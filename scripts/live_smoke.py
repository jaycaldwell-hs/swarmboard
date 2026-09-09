#!/usr/bin/env python3
"""Hosted access checks; only authentication state changes, never board content."""
from __future__ import annotations

import argparse
from contextlib import closing
from http.cookies import SimpleCookie
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

import httpx


COOKIE_NAME = "swarmboard_session"
NAVIGATION = {"Accept": "text/html", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
PROTECTED_PAGES = ("/", "/sessions", "/docs", "/redoc")
PROTECTED_RESOURCES = ("/api/state", "/openapi.json", "/static/app.js", "/api/events?once=true")


def check(response: httpx.Response, expected: int) -> None:
    if response.status_code != expected:
        raise RuntimeError(f"Unexpected HTTP status for {response.request.url.path}: {response.status_code}")
    assert "www-authenticate" not in response.headers, "HTTP Basic challenges must be absent"


def private_headers(response: httpx.Response) -> None:
    assert response.headers.get("cache-control") == "private, no-store", "Private responses must not be cached"
    assert response.headers.get("content-security-policy") == "frame-ancestors 'none'"
    assert response.headers.get("x-frame-options") == "DENY"


def login(client: httpx.Client, username: str, password: str) -> str:
    """Authenticate explicitly; return the cookie only for in-memory replay checks."""
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    check(response, 200)
    private_headers(response)
    status = response.json()
    assert status["enabled"] and status["authenticated"] and status["user"] == username
    cookies = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        cookies.load(header)
    assert COOKIE_NAME in cookies, "Login must set the session cookie"
    cookie = cookies[COOKIE_NAME]
    assert cookie["httponly"] and cookie["samesite"].lower() == "strict"
    assert cookie["path"] == "/" and not cookie["domain"], "Session cookies must remain host-only"
    if client.base_url.scheme == "https":
        assert cookie["secure"], "HTTPS session cookies must be Secure"
    assert cookie.value and cookie.value not in response.text, "Session token must not appear in JSON"
    assert password not in response.text, "Login must not echo a password"
    assert 0 < status["idle_expires_at"] - status["server_time"] <= 1800
    assert 0 < status["absolute_expires_at"] - status["server_time"] <= 28800
    return cookie.value


def denied_boundaries(client: httpx.Client, *, headers: dict[str, str] | None = None,
                      auth: tuple[str, str] | None = None) -> dict[str, int]:
    statuses = {}
    for path in (*PROTECTED_PAGES, *PROTECTED_RESOURCES):
        navigation = path in PROTECTED_PAGES
        response = client.get(path, headers={**(NAVIGATION if navigation else {}), **(headers or {})}, auth=auth)
        check(response, 303 if navigation else 401)
        private_headers(response)
        if navigation:
            destination = urlsplit(response.headers.get("location", ""))
            assert not destination.netloc and destination.path == "/login", "Login redirects must remain local"
        statuses[path] = response.status_code
    return statuses


def verify_snapshot(client: httpx.Client, snapshot_path: Path) -> None:
    """Compare migration evidence without displaying conversation or persona data."""
    def remote_json(path: str) -> dict:
        response = client.get(path)
        if response.status_code != 200:
            raise RuntimeError(f"Snapshot verification HTTP status: {response.status_code}")
        return response.json()

    with closing(sqlite3.connect(snapshot_path.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as local:
        local.row_factory = sqlite3.Row
        local.execute("PRAGMA query_only=ON")
        local.execute("BEGIN")
        agents = list(local.execute("SELECT id, handle, settings FROM agents"))
        run_ids = {row["id"] for row in local.execute("SELECT id FROM runs")}
        threads = list(local.execute("SELECT id, run_id FROM threads"))
        state = remote_json("/api/state")
        remote_agents = {agent["id"]: agent for agent in state["agents"]}
        assert {agent["id"] for agent in agents} <= remote_agents.keys(), "Some migrated agent IDs are missing"

        # Board listings intentionally hide discarded setups and have page caps.
        # Check any omitted run directly through the complete read-only replay.
        found_runs = {run["id"] for run in state["runs"]}
        replays = {}
        for run_id in run_ids - found_runs:
            replay = remote_json(f"/api/runs/{run_id}/replay")
            assert replay["run"]["id"] == run_id, "A migrated run ID differs"
            replays[run_id] = replay
            found_runs.add(run_id)
        assert run_ids <= found_runs, "Some migrated run IDs are missing"

        compared_posts = 0
        for thread in threads:
            remote_thread = remote_json(f"/api/threads/{thread['id']}")
            assert remote_thread["id"] == thread["id"], "A migrated thread ID differs"
            assert remote_thread["run_id"] == thread["run_id"], "A migrated thread's run association differs"
            posts = list(local.execute(
                "SELECT id, body, author_type, author_handle, author_agent_id FROM posts WHERE thread_id=? ORDER BY sequence",
                (thread["id"],),
            ))
            remote_posts = remote_thread["posts"]
            # The thread endpoint caps its transcript at 2,000 posts. Attached
            # threads can use replay when a larger local transcript needs it.
            if len(posts) > len(remote_posts) and thread["run_id"] is not None:
                replay = replays.get(thread["run_id"])
                if replay is None:
                    replay = remote_json(f"/api/runs/{thread['run_id']}/replay")
                    replays[thread["run_id"]] = replay
                remote_posts = [post for post in replay["posts"] if post["thread_id"] == thread["id"]]
            by_id = {post["id"]: post for post in remote_posts}
            for post in posts:
                assert post["id"] in by_id, "A migrated post ID is missing"
                persisted = by_id[post["id"]]
                assert all(persisted[field] == post[field] for field in
                           ("body", "author_type", "author_handle", "author_agent_id")), "A migrated post's body or author differs"
                compared_posts += 1

        local_ada = next((agent for agent in agents if agent["handle"] == "ada"), None)
        if local_ada is None:
            local_ada = next((agent for agent in agents if agent["handle"] == "persona1"), None)
        assert local_ada is not None, "The snapshot has no Ada persona agent"
        saved_snapshot = json.loads(local_ada["settings"])["persona_harness"]
        deployed_snapshot = remote_agents[local_ada["id"]]["settings"]["persona_harness"]
        matches = {
            field + "_sha256_matches": hashlib.sha256(saved_snapshot[field].encode("utf-8")).digest()
            == hashlib.sha256(deployed_snapshot[field].encode("utf-8")).digest()
            for field in ("instructions", "memory")
        }
        assert all(matches.values()), "Ada's migrated persona file byte digests differ"
        print(json.dumps({"migration_verified": {
            "agent_ids": len(agents), "run_ids": len(run_ids), "thread_ids": len(threads),
            "post_ids_bodies_authors": compared_posts, **matches,
        }}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--accounts", type=Path, default=Path("private/accounts.json"))
    parser.add_argument("--snapshot", type=Path, help="Read-only comparison against a local migration SQLite snapshot")
    parser.add_argument("--expect-empty", action="store_true", help="Require an empty hosted board after a deliberate reset")
    args = parser.parse_args()
    users = json.loads(args.accounts.read_text())
    if "SWARMBOARD_AUTH_USERS" in users:
        users = json.loads(users["SWARMBOARD_AUTH_USERS"])
    if not isinstance(users, dict) or not users:
        raise SystemExit("Account file must contain a username/password object")

    with httpx.Client(base_url=args.url.rstrip("/"), timeout=30, follow_redirects=False) as client:
        response = client.get("/health")
        check(response, 200)
        assert response.json() == {"status": "ok"}, "Health must expose only status"
        print(json.dumps({"public_health": 200}))

        check(client.get("/login", headers=NAVIGATION), 200)
        response = client.get("/api/auth/session")
        check(response, 200)
        assert response.json()["enabled"] and not response.json()["authenticated"]
        print(json.dumps({"authentication": "missing", "statuses": denied_boundaries(client)}))
        response = client.post("/api/auth/login", json={"username": "invalid-smoke-account", "password": "invalid-smoke-password"})
        check(response, 401)
        assert not client.cookies.get(COOKIE_NAME), "Rejected login must not create a session"
        # Even previously cached, valid Basic credentials must never create a login.
        print(json.dumps({"authentication": "basic-rejected", "statuses": denied_boundaries(client, auth=next(iter(users.items())))}))

        snapshot_verified = False
        for username, password in users.items():
            token = login(client, username, password)
            initial = client.get("/api/auth/session").json()
            statuses = {}
            for path in (*PROTECTED_PAGES, *PROTECTED_RESOURCES):
                with client.stream("GET", path) as response:
                    check(response, 200)
                    private_headers(response)
                    statuses[path] = response.status_code
                    if path == "/api/state":
                        state = json.loads(response.read())
                    if path.startswith("/api/events"):
                        assert "text/event-stream" in response.headers.get("content-type", "")
            polled = client.get("/api/auth/session").json()
            assert initial["idle_expires_at"] == polled["idle_expires_at"], "Background reads must not refresh idle expiry"
            response = client.post("/api/auth/activity", headers={"X-Swarmboard-Activity": "1"})
            check(response, 200)
            assert response.json()["authenticated"] and response.json()["absolute_expires_at"] == initial["absolute_expires_at"]
            ada = next((agent for agent in state["agents"] if agent["handle"] == "ada"), None)
            assert ada is not None, "Ada must be registered"
            snapshot = ada["settings"].get("persona_harness", {})
            assert snapshot.get("instructions") and snapshot.get("memory"), "Ada must have both complete persona files"
            assert ada["provider"] == "codex" and ada["model"] == "gpt-6-astra", "Unexpected Ada runtime"
            if args.expect_empty:
                assert not state["runs"] and not state["threads"] and not state["events"], "Hosted board still contains history"
            if args.snapshot is not None and not snapshot_verified:
                verify_snapshot(client, args.snapshot)
                snapshot_verified = True
            response = client.post("/api/auth/logout")
            check(response, 200)
            assert not response.json()["authenticated"] and not client.cookies.get(COOKIE_NAME)
            denied_boundaries(client, headers={"Cookie": f"{COOKIE_NAME}={token}"})
            # A cached Basic header cannot silently restore the logged-out session.
            check(client.get("/api/state", auth=(username, password)), 401)
            check(client.post("/api/auth/activity", headers={"Cookie": f"{COOKIE_NAME}={token}", "X-Swarmboard-Activity": "1"}), 401)
            print(json.dumps({
                "username": username, "statuses": statuses,
                "ada": {"provider": ada["provider"], "model": ada["model"],
                        "reasoning_effort": ada["settings"].get("sampling", {}).get("reasoning_effort"),
                        "persona_files_present": True},
                "agent_count": len(state["agents"]), "run_count": len(state["runs"]),
                "thread_count": len(state["threads"]),
                "empty_board_verified": args.expect_empty,
                "cookie_flags_verified": True, "logout_replay_denied": True,
                "polling_did_not_refresh_idle": True,
            }))


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        raise SystemExit(f"Read-only HTTP verification failed: {type(exc).__name__}") from None
