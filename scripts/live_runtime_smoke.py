"""Create one labeled hosted Ada test session and verify shared API inference.

This makes one billable model turn. Credentials stay in private/accounts.json;
only IDs, statuses, counts and runtime metadata are printed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import uuid

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--verify-existing", action="store_true")
    args = parser.parse_args()
    users = json.loads(Path("private/accounts.json").read_text())
    saved = Path("private/live-runtime-smoke.json")
    with httpx.Client(base_url=args.url.rstrip("/"), auth=("admin", users["admin"]), timeout=210) as client:
        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            if not response.is_success:
                raise RuntimeError(f"{method} {path}: HTTP {response.status_code}")
            return response.json()

        if args.verify_existing:
            record = json.loads(saved.read_text())
        else:
            if saved.exists():
                raise SystemExit("A saved smoke session exists; use --verify-existing to avoid another model call")
            session = request("POST", "/api/sessions", json={
                "include_ada": True, "continuous": False, "cadence": "free",
                "title": "Render deployment smoke test",
                "body": "Deployment smoke test: please post one brief acknowledgment that you can read this shared board. No other task is requested.",
                "max_rounds": 1, "max_tokens": 20000, "max_duration_seconds": 300,
                "idempotency_key": "render-smoke-" + uuid.uuid4().hex,
            })
            record = {key: session[key] for key in ("run_id", "thread_id")}
            descriptor = os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump(record, stream)
            print(json.dumps({"created_smoke_session": record}), flush=True)
            try:
                result = request("POST", f"/api/runs/{record['run_id']}/step")
                print(json.dumps({"step_result": result}), flush=True)
            finally:
                stop = client.post(f"/api/runs/{record['run_id']}/stop")
                if stop.status_code not in (200, 409):
                    raise RuntimeError(f"Stopping smoke session: HTTP {stop.status_code}")
            request("POST", f"/api/threads/{record['thread_id']}/posts",
                    auth=("cat", users["cat"]), json={
                        "body": "Deployment smoke test: cat account can contribute to the shared session.",
                        "idempotency_key": "render-smoke-cat-" + record["run_id"],
                    })

        exported = request("GET", f"/api/sessions/{record['run_id']}/export")
        responses = [event for event in exported["events"] if event["event_type"] == "provider.response"]
        turns = exported["turns"]
        print(json.dumps({"turns": [{key: turn.get(key) for key in ("state", "provider", "model", "error", "total_tokens")}
                                    for turn in turns],
                          "provider_response_count": len(responses)}), flush=True)
        assert len(turns) == 1 and responses, "Expected exactly one successful hosted provider turn"
        turn = turns[0]
        metadata = responses[-1]["payload"]["metadata"]
        assert turn["provider"] == "codex" and turn["model"] == "gpt-6-astra"
        assert metadata["auth_mode"] == "api_key" and metadata["configured_cli_version"] == "0.153.4"
        assert turn["validated_action"], "Hosted turn must have a valid action"
        handles = {post["author_handle"] for post in exported["posts"]}
        assert {"admin", "cat"}.issubset(handles), "Export must preserve both authenticated human authors"
        state = request("GET", "/api/state")
        ada = next(agent for agent in state["agents"] if agent["handle"] == "ada")
        snapshot = ada["settings"]["persona_harness"]
        assert all(turn["prompt"].count(snapshot[key]) == 1 for key in ("instructions", "memory"))
        print(json.dumps({"ok": True, **record, "model": turn["model"], "turn_state": turn["state"],
                          "action": turn["validated_action"].get("action"), "total_tokens": turn["total_tokens"],
                          "auth_mode": metadata["auth_mode"], "codex_version": metadata["configured_cli_version"],
                          "both_human_authors_exported": True, "raw_persona_in_prompt_exactly_once": True,
                          "exported_posts": len(exported["posts"])}))


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPError as exc:
        raise SystemExit(f"Hosted runtime HTTP check failed: {type(exc).__name__}") from None
