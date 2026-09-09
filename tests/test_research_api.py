"""Research creation is opt-in and validation leaves no partial ledger writes."""
from __future__ import annotations

import httpx
import pytest
from sqlalchemy import func, select

from swarmboard.app import create_app
from swarmboard.models import Event, Post, Run, Thread
from swarmboard.run_policy import PERMISSIVE, PRODUCTION
from swarmboard.schemas import AgentAction
from .test_engine_acceptance import ScriptedGateway
from .test_harness import persona_directory


@pytest.fixture
async def research_client(tmp_path, monkeypatch):
    for name in ("SWARMBOARD_AUTH_USERS", "SWARMBOARD_REQUIRE_AUTH", "SWARMBOARD_HOSTED", "RENDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SWARMBOARD_PERSONA_DIR", str(persona_directory(tmp_path)))
    app = create_app(database_url=f"sqlite:///{tmp_path / 'research.db'}",
                     gateway=ScriptedGateway(AgentAction(action="pass")))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            peer = (await client.get("/api/state")).json()["agents"][0]["id"]
            yield app, client, peer


def counts(app):
    with app.state.session_factory() as session:
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in (Run, Thread, Post, Event))


async def creation_payload(client, peer, path):
    if path == "/api/runs":
        thread = (await client.post("/api/threads", json={"title": "Research", "body": "Opening"})).json()
        return {"thread_id": thread["thread_id"], "agent_ids": [peer], "continuous": False}
    if path == "/api/personas/ada/sessions":
        return {"title": "Ada research", "body": "Opening", "peer_ids": [peer], "continuous": False,
                "limits": {}, "idempotency_key": "ada-research"}
    return {"agent_ids": [peer], "continuous": False, "idempotency_key": "research"}


@pytest.mark.parametrize("path", ["/api/runs", "/api/sessions", "/api/personas/ada/sessions"])
async def test_creation_defaults_and_rejects_collaboration_policy_without_side_effects(research_client, path):
    app, client, peer = research_client
    payload = await creation_payload(client, peer, path)
    before = counts(app)
    for policy in ("permissive", {"dedup": False}, {"profile": "production", "schema_mode": "capture"}):
        response = await client.post(path, json={**payload, "policy": policy})
        assert response.status_code == 422, response.text
        assert counts(app) == before
    created = await client.post(path, json=payload)
    assert created.status_code == 201, created.text
    data = created.json()
    run_id = data.get("run_id") or data.get("id") or data["run"]["id"]
    with app.state.session_factory() as session:
        run = session.get(Run, run_id)
        assert run.config["session_type"] == "collaboration"
        assert run.config["policy"] == PRODUCTION
    assert (await client.patch(f"/api/runs/{run_id}", json={"session_type": "research", "policy": "permissive"})).status_code in (404, 405)


@pytest.mark.parametrize("path", ["/api/runs", "/api/sessions", "/api/personas/ada/sessions"])
async def test_creation_accepts_research_policy(research_client, path):
    app, client, peer = research_client
    payload = await creation_payload(client, peer, path)
    response = await client.post(path, json={**payload, "session_type": "research", "policy": "permissive"})
    assert response.status_code == 201, response.text
    data = response.json()
    run_id = data.get("run_id") or data.get("id") or data["run"]["id"]
    run = next(run for run in (await client.get("/api/state")).json()["runs"] if run["id"] == run_id)
    assert run["session_type"] == "research" and run["policy"] == PERMISSIVE


async def test_research_turn_and_export_include_policy_and_outcome(research_client):
    _, client, peer = research_client
    response = await client.post("/api/sessions", json={"agent_ids": [peer], "continuous": False,
        "session_type": "research", "policy": "permissive", "idempotency_key": "trace"})
    assert response.status_code == 201, response.text
    run_id = response.json()["run_id"]
    step = await client.post(f"/api/runs/{run_id}/step")
    assert step.status_code == 200, step.text
    export = (await client.get(f"/api/sessions/{run_id}/export")).json()
    assert export["session_type"] == "research" and export["policy"] == PERMISSIVE
    assert len(export["turns"]) == 1
    turn = export["turns"][0]
    assert turn["session_type"] == "research" and turn["policy_snapshot"] == PERMISSIVE
    assert turn["outcome"] == "passed" and turn["rejection_reason"] is None
    assert turn["raw_output"]
    trace = (await client.get(f'/api/turns/{turn["id"]}')).json()
    assert trace["outcome"] == "passed" and trace["policy_snapshot"] == PERMISSIVE


@pytest.mark.parametrize("forbidden", [{"as_handle": "ada"}, {"author_type": "system"}, {"author_agent_id": "ada"}])
async def test_collaboration_rejects_impersonation_and_system_fields_without_writes(research_client, forbidden):
    app, client, peer = research_client
    result = await client.post("/api/sessions", json={"agent_ids": [peer], "continuous": False, "idempotency_key": "plain"})
    thread_id = result.json()["thread_id"]
    before = counts(app)
    response = await client.post(f"/api/threads/{thread_id}/posts", json={"body": "Spoof", **forbidden})
    assert response.status_code == 422, response.text
    assert counts(app) == before


async def test_only_current_provider_choices_are_public(research_client):
    app, client, _ = research_client
    before = counts(app)
    for provider in ("ollama", "vertex_gemini"):
        response = await client.post("/api/agents", json={"handle": "unsupported", "persona": "Test", "model": "test", "provider": provider})
        assert response.status_code == 422
    assert (await client.get("/api/providers/ollama")).status_code == 404
    assert counts(app) == before


async def test_ordinary_human_api_rejects_reserved_author_aliases_before_writing(research_client):
    app, client, peer = research_client
    result = await client.post("/api/sessions", json={"agent_ids": [peer], "continuous": False, "idempotency_key": "aliases"})
    thread_id = result.json()["thread_id"]
    peer_handle = next(agent["handle"] for agent in (await client.get("/api/state")).json()["agents"] if agent["id"] == peer)
    before = counts(app)
    for alias in ("SYSTEM", "system", peer_handle, "@" + peer_handle.upper()):
        for path in ("/api/threads", f"/api/threads/{thread_id}/posts"):
            payload = {"body": "Spoof", "author_handle": alias}
            if path == "/api/threads":
                payload["title"] = "Spoof"
            response = await client.post(path, json=payload)
            assert response.status_code == 422, response.text
            assert counts(app) == before
