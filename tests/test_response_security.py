"""Authenticated response boundaries hide planted credentials, retaining storage."""
from __future__ import annotations

import io
import json
from zipfile import ZipFile

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from swarmboard import findings, interventions, research, sessions
from swarmboard.app import create_app
from swarmboard.models import Event, Turn
from swarmboard.repository import InvalidStateError, NotFoundError, Repository, RepositoryError
from .test_engine_acceptance import ScriptedGateway
from .auth_helpers import login


SERVER_KEY = "planted-server-key-9ba129e64a"
LITERAL_KEY = "planted-literal-credential-b9dc3105"
LOGIN_PASSWORD = "planted-collaborator-password-03a4"


@pytest.fixture
async def secured_client(tmp_path, monkeypatch):
    monkeypatch.setattr("swarmboard.config.load_dotenv", lambda **kwargs: None)
    for name in ("SWARMBOARD_HOSTED", "RENDER", "SWARMBOARD_LOCAL_OPERATOR",
                 "SWARMBOARD_ALLOWED_PROVIDER_HOSTS", "SWARMBOARD_ALLOWED_CREDENTIAL_ENV_VARS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", SERVER_KEY)
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", json.dumps({"collaborator": LOGIN_PASSWORD}))
    monkeypatch.setenv("SWARMBOARD_REQUIRE_AUTH", "1")
    persona_dir = tmp_path / "persona"
    persona_dir.mkdir()
    (persona_dir / "AGENTS.md").write_text("A test-only persona.")
    (persona_dir / "memory.md").write_text("A test-only memory.")
    monkeypatch.setenv("SWARMBOARD_PERSONA_DIR", str(persona_dir))
    app = create_app(database_url=f"sqlite:///{tmp_path / 'responses.db'}", gateway=ScriptedGateway(),
                     recover_on_start=False)

    @app.get("/test-errors/{kind}")
    async def error_route(kind: str):
        if kind == "http":
            raise HTTPException(422, {"message": SERVER_KEY, "api_key": LITERAL_KEY})
        errors = {"missing": NotFoundError, "state": InvalidStateError, "repository": RepositoryError}
        raise errors[kind]("Operation failed: " + SERVER_KEY)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://board.test") as client:
            await login(client, "collaborator", LOGIN_PASSWORD)
            yield app, client


def assert_no_credentials(text):
    assert all(secret not in text for secret in (SERVER_KEY, LITERAL_KEY, LOGIN_PASSWORD))


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/agents", {"handle": "rejected", "model": "qwen/test", "persona": "Test",
                             "settings": {"api_key": LITERAL_KEY, "label": SERVER_KEY}}),
    ("POST", "/api/sessions", {"agent_ids": [], "title": SERVER_KEY,
                               "idempotency_key": "invalid", "password": LITERAL_KEY}),
    ("POST", "/api/runs", {"thread_id": "unused", "policy": {SERVER_KEY: LITERAL_KEY}}),
    ("GET", "/api/events?once=true&after_id=" + SERVER_KEY, None),
])
async def test_validation_errors_do_not_echo_inputs_or_exception_context(secured_client, method, path, body):
    app, client = secured_client
    response = await client.request(method, path, json=body)
    assert response.status_code == 422
    assert_no_credentials(response.text)
    errors = response.json()["detail"]
    assert errors and all(set(error) <= {"type", "loc", "msg"} for error in errors)
    assert (await client.get("/api/state")).json()["threads"] == []
    assert app.state.engine.gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type,content", [
    ("application/json", ('{"api_key": "' + LITERAL_KEY + '", invalid}').encode()),
    ("application/octet-stream", b"\xff" + SERVER_KEY.encode()),
])
async def test_unparseable_bodies_return_safe_validation_json(secured_client, content_type, content):
    _, client = secured_client
    response = await client.post("/api/agents", content=content, headers={"content-type": content_type})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert_no_credentials(response.text)
    assert all(set(error) <= {"type", "loc", "msg"} for error in response.json()["detail"])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,status", [("missing", 404), ("state", 409), ("repository", 400), ("http", 422)])
async def test_exception_details_are_redacted_and_status_is_preserved(secured_client, kind, status):
    _, client = secured_client
    response = await client.get("/test-errors/" + kind)
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    assert_no_credentials(response.text)
    assert "[REDACTED]" in response.text


def seed_captured_credentials(factory):
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="capture_peer", persona="Test persona: " + SERVER_KEY,
                                  model="qwen/test")
        run = sessions.create_session(repo, agents=[agent], title="Test title: " + SERVER_KEY,
                                       body="Test opening: " + SERVER_KEY,
                                       continuous=False, author_handle="collaborator", session_type="research")
        thread = repo.list_threads(run_id=run.id)[0]
        opening = repo.list_posts(thread.id)[0]
        run.config = {**run.config, "diagnostic": {"value": SERVER_KEY, "api_key": LITERAL_KEY}}
        prompt = json.dumps([{"role": "system", "content": "Test secret: " + SERVER_KEY}])
        turn = repo.create_turn(thread_id=thread.id, agent_id=agent.id, prompt=prompt,
                               context_post_ids=[opening.id], context_snapshot={"label": SERVER_KEY},
                               sampling_settings={"api_key": LITERAL_KEY})
        repo.finish_turn(turn.id, state="passed", raw_output="Raw capture: " + SERVER_KEY,
                         parsed_action={"action": "pass", "api_key": LITERAL_KEY})
        response = repo.add_event("provider.response", run_id=run.id, thread_id=thread.id, agent_id=agent.id,
                                  payload={"turn_id": turn.id, "metadata": {"diagnostic": SERVER_KEY,
                                           "api_key": LITERAL_KEY, "message": LOGIN_PASSWORD}})
        findings.note(repo, run_id=run.id, author="collaborator", body=SERVER_KEY, idempotency_key="test-note")
        interventions.create_instruction(repo, run_id=run.id, agent_id=agent.id, body=SERVER_KEY,
                                         author="collaborator", idempotency_key="test-instruction")
        branch = research.fork(repo, thread_id=thread.id, at_post_id=opening.id, author="collaborator")
        child = repo.get_run(branch["run_id"])
        child.config = {**child.config, "lineage": {**child.config["lineage"], "diagnostic": SERVER_KEY}}
        return run.id, thread.id, agent.id, turn.id, response.id, prompt


@pytest.mark.asyncio
async def test_authenticated_history_stream_and_exports_hide_stored_credentials(secured_client):
    app, client = secured_client
    run_id, thread_id, agent_id, turn_id, response_event_id, prompt = seed_captured_credentials(app.state.session_factory)
    paths = ["/api/state", f"/api/threads/{thread_id}",
             f"/api/turns/{turn_id}", f"/api/runs/{run_id}/replay", f"/api/sessions/{run_id}",
             f"/api/sessions/{run_id}/export", f"/api/runs/{run_id}/findings", f"/api/runs/{run_id}/interventions",
             f"/api/threads/{thread_id}/forks", f"/api/threads/{thread_id}/participant-view?agent_id={agent_id}",
             "/api/events?once=true", f"/api/runs/{run_id}/export.jsonl?include_prompts=true",
             f"/api/runs/{run_id}/events.jsonl", f"/api/runs/{run_id}/export.zip?include_prompts=true"]
    for path in paths:
        response = await client.get(path)
        assert response.status_code == 200, path
        if "export.zip" in path:
            with ZipFile(io.BytesIO(response.content)) as archive:
                documents = [archive.read(name).decode() for name in archive.namelist()]
        else:
            documents = [response.text]
        for document in documents:
            assert_no_credentials(document)
            assert "[REDACTED]" in document, path
        assert response.headers["cache-control"] == "private, no-store"
    streamed = await client.get("/api/events?once=true")
    assert streamed.headers["content-type"].startswith("text/event-stream")
    payloads = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith("data: ")]
    assert next(event for event in payloads if event["id"] == response_event_id)["payload"]["metadata"]["diagnostic"] == "[REDACTED]"
    trace = (await client.get(f"/api/turns/{turn_id}")).json()
    assert trace["provider_responses"][0]["metadata"]["api_key"] == "[REDACTED]"
    with app.state.session_factory() as session:
        assert session.get(Turn, turn_id).prompt == prompt
        assert session.get(Turn, turn_id).raw_output == "Raw capture: " + SERVER_KEY
        assert session.get(Event, response_event_id).payload["metadata"]["api_key"] == LITERAL_KEY
    assert app.state.engine.gateway.calls == []


@pytest.mark.asyncio
async def test_session_retry_redacts_saved_response_without_mutating_event(secured_client, monkeypatch):
    app, client = secured_client
    peer = (await client.get("/api/state")).json()["agents"][0]
    payload = {"agent_ids": [peer["id"]], "title": "Test session", "body": "Test opening",
               "continuous": False, "idempotency_key": "test-session-retry"}
    original_add_event = Repository.add_event

    def add_captured_event(repo, event_type, **kwargs):
        if event_type == "session.created":
            kwargs["payload"] = {**kwargs["payload"], "diagnostic": SERVER_KEY, "api_key": LITERAL_KEY}
        return original_add_event(repo, event_type, **kwargs)

    with monkeypatch.context() as capture:
        capture.setattr(Repository, "add_event", add_captured_event)
        created = await client.post("/api/sessions", json=payload)
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    with app.state.session_factory() as session:
        event = session.scalar(select(Event).where(Event.run_id == run_id, Event.event_type == "session.created"))
        event_id = event.id
    retried = await client.post("/api/sessions", json=payload)
    assert retried.status_code == 201
    assert retried.json()["run_id"] == run_id
    assert_no_credentials(retried.text)
    assert retried.json()["diagnostic"] == "[REDACTED]"
    with app.state.session_factory() as session:
        assert session.get(Event, event_id).payload["api_key"] == LITERAL_KEY
