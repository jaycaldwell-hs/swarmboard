"""Cookie authentication fences browsers, replayed tokens, and live streams."""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import sys
from datetime import timedelta
from zipfile import ZipFile

import httpx
import pytest
from sqlalchemy import func, select

from swarmboard.app import create_app
from swarmboard.auth_sessions import AuthSession
from swarmboard.models import Event, Post, Run, Thread, Turn, utc_now
from swarmboard.repository import Repository
from tests.test_engine_acceptance import ScriptedGateway


USER, PASSWORD = "researcher", "planted-login-credential"
COOKIE = "swarmboard_session"


@pytest.fixture
async def session_client(tmp_path, monkeypatch):
    monkeypatch.setattr("swarmboard.config.load_dotenv", lambda **kwargs: None)
    for key in ("RENDER", "SWARMBOARD_HOSTED", "SWARMBOARD_LOCAL_OPERATOR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SWARMBOARD_REQUIRE_AUTH", "1")
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", json.dumps({USER: PASSWORD}))
    app = create_app(database_url=f"sqlite:///{tmp_path / 'session-security.db'}", gateway=ScriptedGateway())
    now = [utc_now()]
    app.state.auth_sessions.clock = lambda: now[0]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://board.test", follow_redirects=False) as client:
            yield app, client, now


async def login(client):
    response = await client.post("/api/auth/login", json={"username": USER, "password": PASSWORD})
    assert response.status_code == 200
    assert "www-authenticate" not in response.headers
    token = client.cookies.get(COOKIE)
    assert token and token not in response.text and PASSWORD not in response.text
    return token, response.json()


@pytest.mark.asyncio
async def test_auth_mutations_reject_cross_origin_without_issuing_revoking_or_refreshing(session_client):
    app, client, now = session_client
    response = await client.post("/api/auth/login", json={"username": USER, "password": PASSWORD},
                                headers={"Origin": "https://attacker.test"})
    assert response.status_code == 403 and "set-cookie" not in response.headers
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AuthSession)) == 0
    _, original = await login(client)
    now[0] += timedelta(minutes=1)
    response = await client.post("/api/auth/activity")
    assert response.status_code == 403
    for path in ("/api/auth/logout", "/api/auth/activity"):
        for headers in ({"Origin": "https://attacker.test"}, {"Origin": "null"}, {"Sec-Fetch-Site": "same-site"}):
            response = await client.post(path, headers={**headers, "X-Swarmboard-Activity": "1"})
            assert response.status_code == 403
            assert "set-cookie" not in response.headers
    current = (await client.get("/api/auth/session")).json()
    assert current["authenticated"] and current["idle_expires_at"] == original["idle_expires_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_headers", [False, True])
async def test_duplicate_session_cookie_values_cannot_select_an_identity(session_client, duplicate_headers):
    _, client, _ = session_client
    token, _ = await login(client)
    client.cookies.clear()
    if duplicate_headers:
        headers = [("Cookie", f"{COOKIE}={token}"), ("Cookie", f"{COOKIE}=invalid")]
    else:
        headers = [("Cookie", f"{COOKIE}={token}; {COOKIE}=invalid")]
    response = await client.get("/api/state", headers=headers)
    assert response.status_code == 401 and "www-authenticate" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["logout", "idle_expiry", "absolute_expiry"])
async def test_expired_or_logged_out_session_cannot_reauthenticate_with_cached_basic(session_client, end):
    _, client, now = session_client
    token, _ = await login(client)
    if end == "logout":
        response = await client.post("/api/auth/logout")
        assert response.status_code == 200 and not response.json()["authenticated"]
        assert not client.cookies.get(COOKIE)
    else:
        now[0] += timedelta(minutes=30) if end == "idle_expiry" else timedelta(hours=8)
    client.cookies.clear()
    basic = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    headers = {"Cookie": f"{COOKIE}={token}", "Authorization": basic}
    for path in ("/api/state", "/api/events?once=true", "/openapi.json"):
        response = await client.get(path, headers=headers)
        assert response.status_code == 401 and "www-authenticate" not in response.headers
    response = await client.post("/api/auth/activity", headers={**headers, "X-Swarmboard-Activity": "1"})
    assert response.status_code == 401
    status = (await client.get("/api/auth/session", headers=headers)).json()
    assert not status["authenticated"] and status["user"] is None
    response = await client.get("/sessions", headers={**headers, "Accept": "text/html"})
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert "www-authenticate" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["logout", "idle_expiry", "absolute_expiry"])
async def test_ending_a_login_does_not_change_the_running_ai_run_or_its_history(session_client, end):
    app, client, now = session_client
    token, _ = await login(client)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        agent = repo.list_agents(enabled_only=True)[0]
        run = repo.create_run(continuous=True, config={"agent_ids": [agent.id]})
        thread = repo.create_thread(title="Independent AI run", run_id=run.id)
        repo.create_human_post(thread.id, "An earlier opening.")
        contribution = repo.create_agent_post(thread.id, agent.id, "An earlier contribution.").post
        turn = repo.create_turn(run_id=run.id, thread_id=thread.id, agent_id=agent.id,
                               prompt='[{"role":"user","content":"Earlier captured context."}]')
        repo.finish_turn(turn.id, state="completed", resulting_post_id=contribution.id,
                         raw_output='{"action":"reply","body":"An earlier contribution."}', input_tokens=2, output_tokens=3)
        repo.increment_run_counters(run.id, posts=1)
        repo.control_run(run.id, "start")
        run_id = run.id
        assert run.state == "running" and (run.rounds_used, run.tokens_used, run.model_calls) == (1, 5, 1)
    def board_state():
        with app.state.session_factory() as session:
            return {model.__tablename__: copy.deepcopy([dict(row) for row in session.execute(
                select(model.__table__).order_by(model.__table__.c.id)).mappings()])
                    for model in (Run, Thread, Post, Turn, Event)}
    before = board_state()
    if end == "logout":
        response = await client.post("/api/auth/logout")
        assert response.status_code == 200
    else:
        # Advance the authentication clock only; the AI runner has its own clock.
        now[0] += timedelta(minutes=30) if end == "idle_expiry" else timedelta(hours=8)
    response = await client.get("/api/auth/session", headers={"Cookie": f"{COOKIE}={token}"})
    assert not response.json()["authenticated"]
    assert (await client.get("/api/state", headers={"Cookie": f"{COOKIE}={token}"})).status_code == 401
    assert board_state() == before
    with app.state.session_factory() as session:
        assert session.get(Run, run_id).state == "running"
    assert app.state.engine.gateway.calls == []


@pytest.mark.asyncio
async def test_auth_tokens_and_fingerprints_are_absent_from_events_and_exports(session_client):
    app, client, _ = session_client
    token, status = await login(client)
    digest = hashlib.sha256(token.encode()).hexdigest()
    with app.state.session_factory() as session:
        credential_fingerprint = session.get(AuthSession, digest).credential_fingerprint
    peer = (await client.get("/api/state")).json()["agents"][0]
    response = await client.post("/api/sessions", json={"agent_ids": [peer["id"]], "continuous": False,
        "title": "Export boundary", "idempotency_key": "auth-export"})
    assert response.status_code == 201
    run_id = response.json()["run_id"]
    documents = [json.dumps(status)]
    for path in ("/api/auth/session", "/api/state", "/api/events?once=true",
                 f"/api/sessions/{run_id}/export", f"/api/runs/{run_id}/export.jsonl", f"/api/runs/{run_id}/events.jsonl"):
        response = await client.get(path)
        assert response.status_code == 200
        documents.append(response.text)
    response = await client.get(f"/api/runs/{run_id}/export.zip")
    assert response.status_code == 200
    with ZipFile(io.BytesIO(response.content)) as archive:
        documents.extend(archive.read(name).decode() for name in archive.namelist() if not name.endswith("/"))
    with app.state.session_factory() as session:
        documents.append(json.dumps([event.payload for event in session.scalars(select(Event))]))
    for document in documents:
        assert all(secret not in document for secret in (token, digest, credential_fingerprint, PASSWORD))
        assert "token_hash" not in document and "credential_fingerprint" not in document
    assert app.state.engine.gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["logout", "idle_expiry"])
@pytest.mark.parametrize("timing", ["after_backlog_read", "after_first_yield"])
async def test_open_sse_stops_before_delivering_after_revocation_or_expiry(session_client, monkeypatch, end, timing):
    app, client, now = session_client
    token, _ = await login(client)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        for number in range(4):
            repo.add_event("test.stream", payload={"number": number})
    ended = False
    def invalidate():
        nonlocal ended
        if ended:
            return
        ended = True
        if end == "logout":
            app.state.auth_sessions.logout(token)
        else:
            now[0] += timedelta(minutes=30)
    if timing == "after_backlog_read":
        original = Repository.list_events
        def read_then_end(repo, *args, **kwargs):
            result = original(repo, *args, **kwargs)
            invalidate()
            return result
        monkeypatch.setattr(Repository, "list_events", read_then_end)

    messages = []
    request_sent = False
    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()
    async def send(message):
        messages.append(message)
        if timing == "after_first_yield" and b"event: update" in message.get("body", b""):
            invalidate()
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "GET", "scheme": "https", "path": "/api/events",
        "raw_path": b"/api/events", "query_string": b"", "root_path": "",
        "headers": [(b"host", b"board.test"), (b"cookie", f"{COOKIE}={token}".encode())],
        "client": ("127.0.0.1", 12345), "server": ("board.test", 443)}
    await asyncio.wait_for(app(scope, receive, send), timeout=3)
    assert messages[0]["status"] == 200 and ended
    updates = [message for message in messages if b"event: update" in message.get("body", b"")]
    assert len(updates) == (0 if timing == "after_backlog_read" else 1)
    assert messages[-1]["type"] == "http.response.body" and not messages[-1].get("more_body", False)
    assert not app.state.broker._subscribers


@pytest.mark.asyncio
async def test_hosted_smoke_script_checks_cookie_login_without_live_calls(session_client, tmp_path, monkeypatch, capsys):
    from starlette.testclient import TestClient
    from scripts import live_smoke

    app, _, _ = session_client
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        repo.create_agent(handle="ada", persona="Test persona", provider="codex", model="gpt-6-astra",
            settings={"persona_harness": {"version": 1, "source": "fixture", "instructions": "Fixture instructions.", "memory": "Fixture memory."}})
        event_count = session.scalar(select(func.count()).select_from(Event))
    accounts = tmp_path / "fake-accounts.json"
    accounts.write_text(json.dumps({USER: PASSWORD}))
    class AlreadyStartedClient(TestClient):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
    monkeypatch.setattr(live_smoke.httpx, "Client", lambda **kwargs: AlreadyStartedClient(
        app, base_url=kwargs["base_url"], follow_redirects=kwargs["follow_redirects"]))
    monkeypatch.setattr(sys, "argv", ["live_smoke.py", "https://board.test", "--accounts", str(accounts)])
    await asyncio.to_thread(live_smoke.main)
    output = capsys.readouterr().out
    assert PASSWORD not in output and '"cookie_flags_verified": true' in output and '"logout_replay_denied": true' in output
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Event)) == event_count
        assert all(row.revoked_at is not None for row in session.scalars(select(AuthSession)))
    assert app.state.engine.gateway.calls == []
