from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from swarmboard.app import create_app
from swarmboard.auth import AuthSettings, BasicAuthMiddleware
from swarmboard.models import Event

from .test_engine_acceptance import ScriptedGateway


TEST_USERS = {"researcher": "test-password", "observer": "different-test-password"}


@pytest.fixture(autouse=True)
def isolated_auth_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("swarmboard.config.load_dotenv", lambda **kwargs: None)
    for name in ("SWARMBOARD_AUTH_USERS", "SWARMBOARD_REQUIRE_AUTH", "RENDER"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def authenticated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", json.dumps(TEST_USERS))
    monkeypatch.setenv("SWARMBOARD_REQUIRE_AUTH", "1")
    persona_dir = tmp_path / "test-persona"
    persona_dir.mkdir()
    (persona_dir / "AGENTS.md").write_text("Test-only persona instructions.")
    (persona_dir / "memory.md").write_text("Test-only persona memory.")
    monkeypatch.setenv("SWARMBOARD_PERSONA_DIR", str(persona_dir))
    return create_app(database_url=f"sqlite:///{tmp_path / 'auth.db'}", gateway=ScriptedGateway())


@pytest.mark.asyncio
async def test_every_surface_requires_login_and_health_is_minimal(authenticated_app):
    async with authenticated_app.router.lifespan_context(authenticated_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=authenticated_app), base_url="https://board.test") as client:
            for method, path in (
                ("GET", "/"), ("GET", "/sessions"), ("GET", "/experiments"),
                ("GET", "/static/app.js"), ("GET", "/docs"), ("GET", "/redoc"),
                ("GET", "/openapi.json"), ("GET", "/api/state"),
                ("GET", "/api/events?once=true"), ("GET", "/api/runs/missing/replay"),
                ("GET", "/api/sessions/missing/export"), ("POST", "/api/emergency-stop"),
                ("POST", "/health"), ("GET", "/health/"), ("GET", "/missing"),
            ):
                response = await client.request(method, path)
                assert response.status_code == 401, (method, path, response.text)
                assert response.json() == {"detail": "Authentication required"}
                assert response.headers["www-authenticate"].startswith('Basic realm="Swarmboard"')
                assert response.headers["cache-control"] == "private, no-store"
                assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
                assert response.headers["x-frame-options"] == "DENY"
            response = await client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            assert (await client.head("/health")).status_code == 200
            assert authenticated_app.state.engine.gateway.calls == []


@pytest.mark.asyncio
async def test_credentials_and_malformed_headers(authenticated_app):
    async with authenticated_app.router.lifespan_context(authenticated_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=authenticated_app), base_url="https://board.test") as client:
            for authorization in (
                "Basic", "Basic !!!", "Bearer token", "Basic " + "a" * 9000,
                "Basic " + base64.b64encode(b"no-colon").decode(),
                "Basic " + base64.b64encode(b"\xff:password").decode(),
                "Basic " + base64.b64encode(b"researcher:wrong").decode(),
                "Basic " + base64.b64encode(b"unknown:test-password").decode(),
            ):
                response = await client.get("/api/state", headers={"Authorization": authorization})
                assert response.status_code == 401
                assert "test-password" not in response.text
            encoded = base64.b64encode(b"researcher:test-password").decode()
            duplicate = await client.get("/api/state", headers=[("Authorization", f"Basic {encoded}")] * 2)
            assert duplicate.status_code == 401
            for username, password in TEST_USERS.items():
                response = await client.get("/api/state", auth=(username, password))
                assert response.status_code == 200
                assert response.headers["cache-control"] == "private, no-store"
                assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
                assert response.headers["x-frame-options"] == "DENY"
                assert "Authorization" in response.headers["vary"]
            for path in ("/", "/static/app.js", "/docs", "/openapi.json", "/api/events?once=true"):
                response = await client.get(path, auth=("researcher", TEST_USERS["researcher"]))
                assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]


@pytest.mark.asyncio
async def test_authenticated_human_posts_and_session_openings_cannot_be_spoofed(authenticated_app):
    async with authenticated_app.router.lifespan_context(authenticated_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=authenticated_app), base_url="https://board.test",
                                    auth=("researcher", TEST_USERS["researcher"])) as client:
            payload = {"title": "Shared investigation", "body": "Opening question.", "author_handle": "SYSTEM", "idempotency_key": "same-browser-key"}
            first = await client.post("/api/threads", json=payload)
            assert first.status_code == 201, first.text
            assert first.json()["post"]["author_handle"] == "researcher"
            assert first.json()["post"]["author_type"] == "human"
            retry = await client.post("/api/threads", json=payload)
            assert retry.json()["thread_id"] == first.json()["thread_id"]
            assert retry.json()["created"] is False
            second = await client.post("/api/threads", json=payload, auth=("observer", TEST_USERS["observer"]))
            assert second.status_code == 201
            assert second.json()["thread_id"] != first.json()["thread_id"]
            assert second.json()["post"]["author_handle"] == "observer"
            reply = await client.post(f"/api/threads/{first.json()['thread_id']}/posts",
                                      json={"body": "Intervention", "author_handle": "ada"},
                                      auth=("observer", TEST_USERS["observer"]))
            assert reply.status_code == 201
            assert reply.json()["post"]["author_handle"] == "observer"
            assert reply.json()["post"]["author_type"] == "human"
            agents = (await client.get("/api/state")).json()["agents"]
            payload = {"agent_ids": [agents[0]["id"]], "title": "Recorded intervention", "body": "Authored opening",
                       "continuous": False, "idempotency_key": "shared-session-key"}
            created = await client.post("/api/sessions", json=payload)
            assert created.status_code == 201, created.text
            opening = (await client.get(f"/api/threads/{created.json()['thread_id']}")).json()["posts"][0]
            assert opening["author_handle"] == "researcher"
            assert opening["author_type"] == "human"
            retry = await client.post("/api/sessions", json=payload)
            assert retry.json()["run_id"] == created.json()["run_id"]
            second = await client.post("/api/sessions", json=payload, auth=("observer", TEST_USERS["observer"]))
            assert second.status_code == 201
            assert second.json()["run_id"] != created.json()["run_id"]
            exported = await client.get(f"/api/sessions/{created.json()['run_id']}/export")
            assert exported.status_code == 200
            assert exported.json()["setup"]["opening"]["author_handle"] == "researcher"
            assert authenticated_app.state.engine.gateway.calls == []


@pytest.mark.asyncio
async def test_authenticated_ada_session_opening_is_human(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    persona_dir = tmp_path / "persona"
    persona_dir.mkdir()
    (persona_dir / "AGENTS.md").write_text("Test persona instructions.")
    (persona_dir / "memory.md").write_text("Test persona memory.")
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", json.dumps(TEST_USERS))
    monkeypatch.setenv("SWARMBOARD_PERSONA_DIR", str(persona_dir))
    authenticated_app = create_app(database_url=f"sqlite:///{tmp_path / 'ada-auth.db'}", gateway=ScriptedGateway())
    async with authenticated_app.router.lifespan_context(authenticated_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=authenticated_app), base_url="https://board.test",
                                    auth=("observer", TEST_USERS["observer"])) as client:
            agents = (await client.get("/api/state")).json()["agents"]
            response = await client.post("/api/personas/ada/sessions", json={
                "title": "Ada opening", "body": "Consider this question", "peer_ids": [agents[0]["id"]],
                "continuous": False, "limits": {}, "idempotency_key": "ada-opening-key",
            })
            assert response.status_code == 201, response.text
            opening = (await client.get(f"/api/threads/{response.json()['thread_id']}")).json()["posts"][0]
            assert opening["author_handle"] == "observer"
            assert opening["author_type"] == "human"
            assert authenticated_app.state.engine.gateway.calls == []


@pytest.mark.asyncio
async def test_browser_mutations_require_same_origin(authenticated_app):
    async with authenticated_app.router.lifespan_context(authenticated_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=authenticated_app), base_url="https://board.test",
                                    auth=("researcher", TEST_USERS["researcher"])) as client:
            payload = {"title": "Allowed origin", "body": "A human-created thread."}
            for headers in (
                {"Origin": "https://attacker.test"}, {"Origin": "null"}, {"Origin": "http://board.test"},
                {"Origin": "https://board.test:8443"}, {"Sec-Fetch-Site": "cross-site"},
                {"Sec-Fetch-Site": "same-site"},
            ):
                response = await client.post("/api/threads", json=payload, headers=headers)
                assert response.status_code == 403
                assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
                assert response.headers["x-frame-options"] == "DENY"
            assert (await client.get("/api/state")).json()["threads"] == []
            for headers in ({}, {"Origin": "https://board.test", "Sec-Fetch-Site": "same-origin"},
                            {"Origin": "https://board.test:443"}):
                response = await client.post("/api/threads", json=payload, headers=headers)
                assert response.status_code == 201, response.text


@pytest.mark.parametrize("name,value", [("SWARMBOARD_REQUIRE_AUTH", "1"), ("RENDER", "true")])
def test_hosted_app_fails_closed_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match="SWARMBOARD_AUTH_USERS is required"):
        create_app(database_url=f"sqlite:///{tmp_path / 'fail-closed.db'}")
    assert not (tmp_path / "fail-closed.db").exists()


@pytest.mark.parametrize("raw", ["", "bad-json-secret", "[]", "{}", '{"researcher":""}',
                                 '{"researcher":4}', '{"SYSTEM":"password"}', '{"bad:user":"password"}'])
def test_invalid_auth_config_fails_without_exposing_values(monkeypatch: pytest.MonkeyPatch, raw):
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", raw)
    with pytest.raises(ValueError) as exc:
        AuthSettings.from_env()
    assert "bad-json-secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_local_mode_preserves_existing_human_handle(tmp_path: Path):
    app = create_app(database_url=f"sqlite:///{tmp_path / 'local.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/threads", json={"title": "Local", "body": "Local post", "author_handle": "tester"})
            assert response.status_code == 201
            assert response.json()["post"]["author_handle"] == "tester"


@pytest.mark.asyncio
async def test_middleware_delivers_stream_chunks_before_completion(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", json.dumps(TEST_USERS))
    messages = asyncio.Queue()
    finish_stream = asyncio.Event()

    async def endpoint(scope, receive, send):
        assert scope["state"]["authenticated_user"] == "researcher"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: first\n\n", "more_body": True})
        await finish_stream.wait()
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def receive():
        return {"type": "http.request", "body": b""}

    middleware = BasicAuthMiddleware(endpoint, AuthSettings.from_env())
    authorization = b"Basic " + base64.b64encode(b"researcher:test-password")
    scope = {"type": "http", "path": "/api/events", "method": "GET", "scheme": "https",
             "headers": [(b"authorization", authorization), (b"host", b"board.test")]}
    task = asyncio.create_task(middleware(scope, receive, messages.put))
    try:
        assert (await asyncio.wait_for(messages.get(), timeout=1))["type"] == "http.response.start"
        first = await asyncio.wait_for(messages.get(), timeout=1)
        assert first["body"] == b"data: first\n\n"
        assert not task.done()
    finally:
        finish_stream.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_successful_mutations_have_separate_attributed_action_events(authenticated_app):
    expected = []
    async with authenticated_app.router.lifespan_context(authenticated_app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=authenticated_app), base_url="https://board.test") as client:
            async def mutate(method, path, route, *, username="researcher", body=None):
                response = await client.request(method, path + "?debug=secret-query-value", json=body,
                                                auth=(username, TEST_USERS[username]))
                assert 200 <= response.status_code < 300, response.text
                expected.append((username, {"method": method, "path": route, "status": response.status_code}))
                return response.json()

            created_agent = await mutate("POST", "/api/agents", "/api/agents", body={
                "handle": "audit_peer", "persona": "Private body marker: do-not-copy-to-action-audit",
                "provider": "openai_compatible", "model": "test-model",
            })
            await mutate("PATCH", f"/api/agents/{created_agent['id']}", "/api/agents/{agent_id}",
                         username="observer", body={"cooldown_seconds": 5})
            await mutate("POST", "/api/personas/ada/reload", "/api/personas/ada/reload", username="observer")
            body = {"agent_ids": [created_agent["id"]], "continuous": False, "idempotency_key": "audit-session"}
            created = await mutate("POST", "/api/sessions", "/api/sessions", body=body)
            retry = await mutate("POST", "/api/sessions", "/api/sessions", body=body)
            assert retry["run_id"] == created["run_id"]
            run_id = created["run_id"]
            for action, username in (("start", "observer"), ("pause", "researcher"),
                                     ("resume", "observer"), ("stop", "researcher")):
                await mutate("POST", f"/api/runs/{run_id}/{action}", f"/api/runs/{{run_id}}/{action}", username=username)
            rerun = await mutate("POST", f"/api/runs/{run_id}/rerun", "/api/runs/{run_id}/rerun", username="observer")
            await mutate("POST", f"/api/runs/{rerun['id']}/emergency-stop", "/api/runs/{run_id}/emergency-stop", username="observer")
            unused = await mutate("POST", "/api/sessions", "/api/sessions", body={**body, "idempotency_key": "discard-audit"})
            await mutate("POST", f"/api/sessions/{unused['run_id']}/discard", "/api/sessions/{run_id}/discard", username="observer")
            await mutate("POST", "/api/emergency-stop", "/api/emergency-stop")

            # Rejections and reads must not look like successful human actions.
            assert (await client.post("/api/emergency-stop")).status_code == 401
            assert (await client.post("/api/agents", json={}, auth=("researcher", TEST_USERS["researcher"]))).status_code == 422
            assert (await client.post("/api/runs/missing/start", auth=("researcher", TEST_USERS["researcher"]))).status_code == 404
            assert (await client.post("/api/emergency-stop", headers={"Origin": "https://other.test"},
                                      auth=("researcher", TEST_USERS["researcher"]))).status_code == 403
            replay = await client.get(f"/api/sessions/{run_id}/export", auth=("researcher", TEST_USERS["researcher"]))
            exported_actions = [event for event in replay.json()["events"] if event["event_type"] == "human.action"]
            assert len(exported_actions) == 5
            assert all(event["actor_id"] in TEST_USERS for event in exported_actions)

        with authenticated_app.state.session_factory() as session:
            actions = list(session.scalars(select(Event).where(Event.event_type == "human.action").order_by(Event.id)))
        assert [(event.actor_id, event.payload) for event in actions] == expected
        assert all(event.actor_type == "human" for event in actions)
        edited_action = next(event for event in actions if event.payload["method"] == "PATCH")
        assert edited_action.agent_id == created_agent["id"]
        serialized = json.dumps([event.payload for event in actions])
        for excluded in ("secret-query-value", "do-not-copy-to-action-audit", *TEST_USERS.values()):
            assert excluded not in serialized
        assert authenticated_app.state.engine.gateway.calls == []


@pytest.mark.asyncio
async def test_audit_failure_preserves_successful_response_without_exposing_error(monkeypatch: pytest.MonkeyPatch, caplog):
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", json.dumps(TEST_USERS))
    messages = []
    audit_calls = []

    async def endpoint(scope, receive, send):
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"created", "more_body": False})

    async def audit(scope, status):
        audit_calls.append(status)
        raise RuntimeError("sensitive-exception-must-not-be-logged")

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        messages.append(message)

    middleware = BasicAuthMiddleware(endpoint, AuthSettings.from_env(), audit=audit)
    authorization = b"Basic " + base64.b64encode(b"researcher:test-password")
    scope = {"type": "http", "path": "/api/threads", "method": "POST", "scheme": "https",
             "headers": [(b"authorization", authorization), (b"host", b"board.test")]}
    await middleware(scope, receive, send)
    assert audit_calls == [201]
    assert [message["type"] for message in messages] == ["http.response.start", "http.response.body"]
    assert messages[0]["status"] == 201
    assert messages[1]["body"] == b"created"
    assert "Could not record authenticated human action" in caplog.text
    assert "sensitive-exception-must-not-be-logged" not in caplog.text
