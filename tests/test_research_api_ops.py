from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from swarmboard.app import create_app
from swarmboard.models import Event, Run, Thread
from .test_engine_acceptance import ScriptedGateway


@pytest.mark.asyncio
async def test_authenticated_fork_is_idempotent_source_unchanged_and_sse_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", '{"researcher":"fixture-password"}')
    monkeypatch.setattr("swarmboard.config.load_dotenv", lambda **kwargs: None)
    app = create_app(database_url=f"sqlite:///{tmp_path / 'fork-api.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://board.test",
                                     auth=("researcher", "fixture-password")) as client:
            peers = (await client.get("/api/state")).json()["agents"]
            created = (await client.post("/api/sessions", json={"agent_ids": [peers[0]["id"]],
                "body": "Original opening", "continuous": False, "idempotency_key": "source"})).json()
            rid, tid = created["run_id"], created["thread_id"]
            await client.post(f"/api/runs/{rid}/stop")
            source = (await client.get(f"/api/threads/{tid}")).json()
            post_id = source["posts"][0]["id"]
            with app.state.session_factory() as session:
                before = [(e.id, e.payload) for e in session.scalars(select(Event).where(Event.run_id == rid).order_by(Event.id))]
            body = {"at_post_id": post_id, "idempotency_key": "fork", "policy": "permissive"}
            response = await client.post(f"/api/threads/{tid}/fork", json=body)
            assert response.status_code == 201, response.text
            child = response.json()
            assert child["session_type"] == "research"
            assert (await client.post(f"/api/threads/{tid}/fork", json=body)).json() == child
            assert (await client.post(f"/api/threads/{tid}/fork", json={**body, "policy": "production"})).status_code == 409
            assert (await client.get(f"/api/threads/{tid}")).json() == source
            with app.state.session_factory() as session:
                assert [(e.id, e.payload) for e in session.scalars(select(Event).where(Event.run_id == rid).order_by(Event.id))] == before
                assert session.get(Run, child["run_id"]).state == "created"
                assert session.get(Thread, child["thread_id"]).current_sequence == 1
                event = session.scalar(select(Event).where(Event.event_type == "research.forked", Event.run_id == child["run_id"]))
                assert event.actor_id == "researcher"
            stream = await client.get("/api/events", params={"after_id": before[-1][0], "once": "true"})
            assert "research.forked" in stream.text and child["run_id"] in stream.text
            assert (await client.get(f"/api/threads/{tid}/forks")).json()["forks"][0]["run_id"] == child["run_id"]
