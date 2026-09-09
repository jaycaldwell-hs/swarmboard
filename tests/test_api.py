from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from swarmboard.app import create_app
from swarmboard.database import init_db, make_engine, make_session_factory
from swarmboard.gateways import AgentAction
from swarmboard.models import Agent, Event, RunState, Stimulus, StimulusKind, Thread, ThreadStatus
from swarmboard.repository import Repository

from .test_engine_acceptance import ScriptedGateway


@pytest.mark.asyncio
async def test_terminal_thread_can_be_closed_from_browser_api(tmp_path: Path) -> None:
    app = create_app(database_url=f"sqlite:///{tmp_path / 'close-thread.db'}", gateway=ScriptedGateway())

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            page = await client.get("/")
            assert page.status_code == 200
            assert 'id="close-thread-button"' in page.text

            created = await client.post(
                "/api/threads",
                json={"title": "Ready to close", "body": "This discussion is complete."},
            )
            thread_id = created.json()["thread_id"]
            run = await client.post(
                "/api/runs",
                json={"thread_id": thread_id, "continuous": False},
            )
            assert run.status_code == 201
            assert run.json()["limits"]["tokens"] == 200_000
            stopped = await client.post(f"/api/runs/{run.json()['id']}/stop")
            assert stopped.status_code == 200

            closed = await client.post(
                f"/api/threads/{thread_id}/status",
                json={"status": "closed", "reason": "closed by human"},
            )
            assert closed.status_code == 200
            assert closed.json()["status"] == ThreadStatus.CLOSED.value
            assert closed.json()["closed_at"] is not None

            rejected_post = await client.post(
                f"/api/threads/{thread_id}/posts",
                json={"body": "This should be rejected."},
            )
            assert rejected_post.status_code == 409
            assert "rerun or create a fresh thread" in rejected_post.json()["detail"]


@pytest.mark.asyncio
async def test_live_board_api_uses_persisted_state_and_nested_run_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Force replay across multiple pages so a green test proves the response is
    # complete rather than merely smaller than the production page size.
    monkeypatch.setattr("swarmboard.app.REPLAY_EVENT_PAGE_SIZE", 2)
    monkeypatch.setattr("swarmboard.app.EVENT_STREAM_PAGE_SIZE", 2)
    gateway = ScriptedGateway(
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None),
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None),
    )
    app = create_app(database_url=f"sqlite:///{tmp_path / 'api.db'}", gateway=gateway)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).json()["status"] == "ok"
            assert "Swarmboard" in (await client.get("/")).text

            initial = (await client.get("/api/state")).json()
            assert len(initial["agents"]) == 6
            assert all(agent["provider"] and agent["model"] for agent in initial["agents"])
            assert {agent["handle"] for agent in initial["agents"]} == {
                "wintermute",
                "dr_benway",
                "dixie_flatline",
                "mugwump",
                "armitage",
                "bill_lee",
            }
            from swarmboard.peer_models import OPEN_MODELS
            assert {agent["provider"] for agent in initial["agents"]} == {"openai_compatible"}
            assert {agent["model"] for agent in initial["agents"]} == set(OPEN_MODELS)
            for agent in initial["agents"]:
                assert agent["settings"]["api_key_env"] == "OPENROUTER_API_KEY"
                assert agent["settings"]["base_url"] == "https://openrouter.ai/api/v1"
                assert agent["settings"]["sampling"]["provider"] == {"zdr": True, "require_parameters": True}
            assert initial["threads"] == []

            request_id = "browser-request-1"
            created = await client.post(
                "/api/threads",
                json={
                    "title": "Does the board persist?",
                    "body": "Please inspect the durable event path.",
                    "idempotency_key": request_id,
                },
            )
            assert created.status_code == 201
            thread_id = created.json()["thread_id"]

            redelivered = await client.post(
                "/api/threads",
                json={
                    "title": "Does the board persist?",
                    "body": "Please inspect the durable event path.",
                    "idempotency_key": request_id,
                },
            )
            assert redelivered.status_code == 201
            assert redelivered.json()["thread_id"] == thread_id
            assert redelivered.json()["created"] is False

            run_response = await client.post(
                "/api/runs",
                json={
                    "thread_id": thread_id,
                    "continuous": False,
                    "seed": 17,
                    "limits": {
                        "max_rounds": 4,
                        "max_posts": 10,
                        "max_tokens": 1_000,
                        "max_duration_seconds": 60,
                        "per_agent_quota": 4,
                        "per_thread_quota": 8,
                        "max_cascade_depth": 2,
                    },
                },
            )
            assert run_response.status_code == 201, run_response.text
            run = run_response.json()
            assert run["limits"]["rounds"] == 4
            assert run["limits"]["cascade_depth"] == 2

            stepped = await client.post(f"/api/runs/{run['id']}/step")
            assert stepped.status_code == 200, stepped.text
            assert stepped.json()["status"] == "processed"

            state = (await client.get("/api/state", params={"thread_id": thread_id})).json()
            assert state["selected_thread"]["id"] == thread_id
            assert len(state["selected_thread"]["posts"]) == 1
            assert state["runs"][0]["state"] == "paused"
            assert len(gateway.calls) == 2

            replay = await client.get(f"/api/runs/{run['id']}/replay")
            assert replay.status_code == 200
            assert replay.json()["mode"] == "replay"
            assert replay.json()["model_calls"] == 0
            with app.state.session_factory() as session:
                expected_event_ids = list(
                    session.scalars(
                        select(Event.id).where(Event.run_id == run["id"]).order_by(Event.id)
                    )
                )
            assert len(expected_event_ids) > 2
            assert [event["id"] for event in replay.json()["events"]] == expected_event_ids

            with app.state.session_factory() as session:
                all_event_ids = list(session.scalars(select(Event.id).order_by(Event.id)))
            catch_up = await client.get("/api/events", params={"after_id": 0, "once": True})
            assert catch_up.status_code == 200
            streamed_ids = [
                int(line.removeprefix("id: "))
                for line in catch_up.text.splitlines()
                if line.startswith("id: ")
            ]
            assert len(all_event_ids) > 2
            assert streamed_ids == all_event_ids

            stopped = await client.post(f"/api/runs/{run['id']}/stop")
            assert stopped.status_code == 200
            replacement = await client.post(
                "/api/runs",
                json={"thread_id": thread_id, "continuous": False},
            )
            assert replacement.status_code == 409
            assert "use rerun" in replacement.json()["detail"]

            replay_after_rejected_reattach = await client.get(f"/api/runs/{run['id']}/replay")
            assert replay_after_rejected_reattach.status_code == 200
            preserved = replay_after_rejected_reattach.json()
            assert [item["id"] for item in preserved["threads"]] == [thread_id]
            assert len(preserved["posts"]) == 1


@pytest.mark.asyncio
async def test_direct_mention_wakes_dormant_thread_and_creates_targeted_stimulus(
    tmp_path: Path,
) -> None:
    gateway = ScriptedGateway()
    app = create_app(database_url=f"sqlite:///{tmp_path / 'mentions.db'}", gateway=gateway)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            initial = (await client.get("/api/state")).json()
            target_agent = initial["agents"][0]
            created = await client.post(
                "/api/threads",
                json={"title": "Dormant question", "body": "Record the initial premise."},
            )
            assert created.status_code == 201
            thread_id = created.json()["thread_id"]

            run_response = await client.post(
                "/api/runs",
                json={"thread_id": thread_id, "continuous": False},
            )
            assert run_response.status_code == 201
            run_id = run_response.json()["id"]

            dormant = await client.post(
                f"/api/threads/{thread_id}/status",
                json={"status": "dormant", "reason": "waiting for evidence"},
            )
            assert dormant.status_code == 200

            mentioned = await client.post(
                f"/api/threads/{thread_id}/posts",
                json={
                    "body": (
                        f"@{target_agent['handle']}, can you test the highest-risk assumption?"
                    ),
                    "idempotency_key": "wake-mentioned-agent-once",
                },
            )
            assert mentioned.status_code == 201
            source_post_id = mentioned.json()["post"]["id"]
            assert len(mentioned.json()["stimulus_ids"]) == 1

            emergency = await client.post("/api/emergency-stop")
            assert emergency.status_code == 200
            assert run_id in emergency.json()["stopped_run_ids"]

        with app.state.session_factory() as session:
            mentioned_agent = session.get(Agent, target_agent["id"])
            stimuli = list(
                session.scalars(
                    select(Stimulus).where(
                        Stimulus.run_id == run_id,
                        Stimulus.source_post_id == source_post_id,
                    )
                )
            )
            thread = session.get(Thread, thread_id)

        assert mentioned_agent is not None
        assert thread is not None and thread.status == ThreadStatus.ACTIVE.value
        assert len(stimuli) == 1
        assert stimuli[0].kind == StimulusKind.MENTION.value
        assert stimuli[0].target_agent_id == mentioned_agent.id
        with app.state.session_factory() as session:
            assert Repository(session).get_run(run_id).state == RunState.EMERGENCY_STOPPED.value
        assert gateway.calls == []


@pytest.mark.asyncio
async def test_agent_api_rejects_literal_credentials_and_accepts_env_references(
    tmp_path: Path,
) -> None:
    app = create_app(database_url=f"sqlite:///{tmp_path / 'credential-inputs.db'}")
    base_agent = {
        "handle": "remote",
        "persona": "Use the configured compatible provider.",
        "provider": "openai_compatible",
        "model": "provider/model",
    }

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            literal = await client.post(
                "/api/agents",
                json={**base_agent, "settings": {"api_key": "must-not-persist"}},
            )
            assert literal.status_code == 422
            assert "use api_key_env" in literal.text

            header_secret = await client.post(
                "/api/agents",
                json={
                    **base_agent,
                    "settings": {"headers": {"Authorization": "Bearer must-not-persist"}},
                },
            )
            assert header_secret.status_code == 422

            invalid_name = await client.post(
                "/api/agents",
                json={**base_agent, "settings": {"api_key_env": "not an env name"}},
            )
            assert invalid_name.status_code == 422

            accepted = await client.post(
                "/api/agents",
                json={
                    **base_agent,
                    "settings": {
                        "api_key_env": "OPENROUTER_API_KEY",
                        "headers": {"HTTP-Referer": "http://127.0.0.1:8000"},
                    },
                },
            )
            assert accepted.status_code == 201, accepted.text
            assert accepted.json()["settings"]["api_key_env"] == "OPENROUTER_API_KEY"


@pytest.mark.asyncio
async def test_startup_scrubs_and_never_serializes_legacy_credentials(tmp_path: Path) -> None:
    sql_engine = make_engine(f"sqlite:///{tmp_path / 'legacy-credentials.db'}")
    init_db(sql_engine)
    factory = make_session_factory(sql_engine)
    with factory.begin() as session:
        legacy = Repository(session).create_agent(
            handle="legacy",
            persona="A legacy provider record.",
            provider="openai_compatible",
            model="provider/model",
            settings={
                "api_key": "top-secret-value",
                "api_key_env": "OPENROUTER_API_KEY",
                "base_url": "https://openrouter.ai/api/v1",
                "headers": {
                    "Authorization": "Bearer second-secret-value",
                    "HTTP-Referer": "http://127.0.0.1:8000",
                },
            },
        )
        legacy_id = legacy.id

    app = create_app(session_factory=factory, gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            state = (await client.get("/api/state")).json()
        serialized = next(agent for agent in state["agents"] if agent["id"] == legacy_id)
        assert "api_key" not in serialized["settings"]
        assert "Authorization" not in serialized["settings"]["headers"]
        assert serialized["settings"]["api_key_env"] == "OPENROUTER_API_KEY"
        assert serialized["settings"]["headers"] == {
            "HTTP-Referer": "http://127.0.0.1:8000"
        }

        with factory() as session:
            persisted = session.get(Agent, legacy_id)
            scrub_events = list(
                session.scalars(
                    select(Event).where(
                        Event.agent_id == legacy_id,
                        Event.event_type == "agent.credentials_scrubbed",
                    )
                )
            )
        assert persisted is not None and persisted.settings == serialized["settings"]
        assert len(scrub_events) == 1
        audit_text = str(scrub_events[0].payload)
        assert "settings.api_key" in audit_text
        assert "settings.headers.Authorization" in audit_text
        assert "top-secret-value" not in audit_text
        assert "second-secret-value" not in audit_text
    sql_engine.dispose()
