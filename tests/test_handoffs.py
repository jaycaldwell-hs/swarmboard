from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from swarmboard.app import _run_activities, create_app
from swarmboard.config import DEFAULT_AGENT_OUTPUT_TOKENS
from swarmboard.engine import EngineConfig, SwarmEngine
from swarmboard.gateways import AgentAction, GatewayError
from swarmboard.models import Agent, Run, RunState, Stimulus, Thread, Turn, utc_now
from swarmboard.personas import DEFAULT_AGENTS
from swarmboard.repository import Repository

from .test_engine_acceptance import ScriptedGateway, make_database, seed_conversation


def seed_participants(factory, *, kind="human_post", max_rounds=10):
    run_id, thread_id, opening_id, stimulus_id = seed_conversation(factory, max_rounds=max_rounds)
    with factory.begin() as session:
        repo = Repository(session)
        first = repo.list_agents()[0]
        peers = [repo.create_agent(handle=name, persona="A distinct voice", provider="openai_compatible",
                                  model="unused", cooldown_seconds=0) for name in ("second", "third", "excluded")]
        ids = [first.id, peers[0].id, peers[1].id]
        repo.get_run(run_id).config = {"agent_ids": ids, "max_agents_per_stimulus": 1, "model_retries": 0}
        stimulus = repo.get_stimulus(stimulus_id)
        stimulus.kind, stimulus.target_agent_id = kind, first.id
    return run_id, thread_id, opening_id, stimulus_id, ids


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["human_post", "idle_revisit"])
async def test_passes_try_each_scoped_participant_once_then_go_dormant(kind):
    db, factory = make_database()
    run_id, thread_id, _, _, ids = seed_participants(factory, kind=kind)
    gateway = ScriptedGateway(*(AgentAction(action="pass") for _ in ids))
    swarm = SwarmEngine(factory, gateway=gateway)
    for index in range(3):
        await swarm.step(run_id)
        with factory() as session:
            assert session.get(Thread, thread_id).status == ("dormant" if index == 2 else "active")
    assert {call["agent_id"] for call in gateway.calls} == set(ids)
    assert len(gateway.calls) == 3
    assert (await swarm.step(run_id)).status == "no_work"
    with factory() as session:
        stimuli = list(session.scalars(select(Stimulus).where(Stimulus.run_id == run_id)))
        assert len(stimuli) == 3
        assert all(stimulus.state == "completed" for stimulus in stimuli)
        assert len({stimulus.payload.get("fallback_root_id", stimulus.id) for stimulus in stimuli}) == 1
        assert session.get(Run, run_id).rounds_used == 3
    await swarm.shutdown()
    db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_result", [AgentAction(action="pass"), GatewayError("unavailable", retryable=False)])
async def test_noncontributing_peer_hands_off_to_another_participant(first_result):
    db, factory = make_database()
    run_id, thread_id, opening_id, _, ids = seed_participants(factory, max_rounds=2)
    gateway = ScriptedGateway(first_result, AgentAction(action="reply", parent_post_id=opening_id,
                                                       body="I have a different idea.", intent="support"))
    swarm = SwarmEngine(factory, gateway=gateway)
    await swarm.step(run_id)
    await swarm.step(run_id)
    assert gateway.calls[0]["agent_id"] == ids[0]
    assert gateway.calls[1]["agent_id"] in ids[1:]
    assert (await swarm.step(run_id)).status in {"terminal", "budget_exhausted"}
    with factory() as session:
        assert len(Repository(session).list_posts(thread_id)) == 2
        assert session.get(Run, run_id).rounds_used == 2
    await swarm.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_completed_pass_is_recovered_without_recalling_agent_or_duplicating_fallback():
    db, factory = make_database()
    run_id, thread_id, _, stimulus_id, ids = seed_participants(factory)
    gateway = ScriptedGateway(AgentAction(action="pass"))
    swarm = SwarmEngine(factory, gateway=gateway, config=EngineConfig(claim_lease_seconds=1))
    with factory.begin() as session:
        session.get(Run, run_id).state = "running"
        claim = Repository(session).claim_stimuli(run_id=run_id, limit=1)[0]
        token = claim.claim_token
    turn_ids, _ = await swarm._prepare_stimulus(run_id, stimulus_id, token)
    await swarm._execute_turn(turn_ids[0], allow_manual_pause=False)
    # Simulate process loss after the pass is committed, before queue completion.
    with factory.begin() as session:
        stimulus = session.get(Stimulus, stimulus_id)
        stimulus.updated_at = stimulus.claimed_at = utc_now() - timedelta(seconds=3)
    await swarm.step(run_id)
    await swarm._complete_claim(stimulus_id, token, made_posts=False)  # Stale worker is harmless.
    assert len(gateway.calls) == 1
    with factory() as session:
        pending = list(session.scalars(select(Stimulus).where(Stimulus.run_id == run_id, Stimulus.state == "pending")))
        assert len(pending) == 1
        assert pending[0].payload["attempted_agent_ids"] == [ids[0]]
        assert session.get(Thread, thread_id).status == "active"
    await swarm.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_ineligible_direct_recipient_does_not_strand_other_participants():
    db, factory = make_database()
    run_id, _, opening_id, _, ids = seed_participants(factory)
    with factory.begin() as session:
        session.get(Agent, ids[0]).permissions = {"speak": False}
    gateway = ScriptedGateway(AgentAction(action="reply", parent_post_id=opening_id,
                                          body="I can take this question.", intent="support"))
    swarm = SwarmEngine(factory, gateway=gateway)
    await swarm.step(run_id)
    assert not gateway.calls
    await swarm.step(run_id)
    assert len(gateway.calls) == 1 and gateway.calls[0]["agent_id"] in ids[1:]
    await swarm.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_fallback_waits_for_cooldown_without_recalling_passed_participant():
    db, factory = make_database()
    run_id, thread_id, _, _, ids = seed_participants(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.config = {**run.config, "agent_ids": ids[:2]}
        peer = session.get(Agent, ids[1])
        peer.cooldown_seconds, peer.last_spoke_at = 30, utc_now()
    gateway = ScriptedGateway(AgentAction(action="pass"))
    swarm = SwarmEngine(factory, gateway=gateway)
    await swarm.step(run_id)
    await swarm.step(run_id)
    assert len(gateway.calls) == 1
    with factory() as session:
        pending = list(session.scalars(select(Stimulus).where(Stimulus.run_id == run_id, Stimulus.state == "pending")))
        assert len(pending) == 1
        assert pending[0].not_before > utc_now()
        assert pending[0].payload["attempted_agent_ids"] == [ids[0]]
        assert session.get(Thread, thread_id).status == "active"
    await swarm.shutdown()
    db.dispose()


def test_activity_distinguishes_thinking_queue_cooldown_idle_dormant_and_pause():
    db, factory = make_database()
    run_id, thread_id, _, stimulus_id, ids = seed_participants(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.state = "running"
        session.flush()
        assert _run_activities(session, [run])[run_id]["state"] == "queued"
        stimulus = session.get(Stimulus, stimulus_id)
        stimulus.not_before = utc_now() + timedelta(seconds=60)
        session.flush()
        assert _run_activities(session, [run])[run_id]["state"] == "cooldown"
        turn = Repository(session).create_turn(run_id=run_id, thread_id=thread_id, agent_id=ids[0], stimulus_id=stimulus_id)
        Repository(session).mark_turn_calling(turn.id)
        activity = _run_activities(session, [run])[run_id]
        assert activity["state"] == "thinking" and activity["calling_agents"] == ["tester"]
        turn.state, stimulus.state = "passed", "completed"
        session.flush()
        assert _run_activities(session, [run])[run_id]["state"] == "idle"
        session.get(Thread, thread_id).status = "dormant"
        session.flush()
        assert _run_activities(session, [run])[run_id]["state"] == "dormant"
        run.state = RunState.PAUSED.value
        assert _run_activities(session, [run])[run_id]["state"] == "paused"
    db.dispose()


@pytest.mark.asyncio
async def test_board_api_reports_activity_and_routes_human_reply_to_parent_author():
    db, factory = make_database()
    run_id, thread_id, opening_id, stimulus_id, ids = seed_participants(factory)
    with factory.begin() as session:
        repo = Repository(session)
        repo.get_run(run_id).state = "running"
        parent_id = repo.create_agent_post(thread_id, ids[0], "My suggestion.", parent_post_id=opening_id).post.id
        repo.get_stimulus(stimulus_id).state = "completed"
        repo.get_thread(thread_id).status = "dormant"
    app = create_app(session_factory=factory, gateway=ScriptedGateway(), recover_on_start=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            before = (await client.get("/api/state", params={"thread_id": thread_id})).json()
            assert before["runs"][0]["state"] == "running"
            assert before["runs"][0]["activity"]["state"] == "dormant"
            reply = await client.post(f"/api/threads/{thread_id}/posts", json={
                "body": "Can you explain that suggestion?", "parent_post_id": parent_id,
            })
            assert reply.status_code == 201, reply.text
            assert len(reply.json()["stimulus_ids"]) == 1
            after = (await client.get("/api/state", params={"thread_id": thread_id})).json()
            assert after["runs"][0]["activity"]["state"] == "queued"
            assert after["runs"][0]["activity"]["pending_stimuli"] == 1
            with factory() as session:
                stimulus = session.get(Stimulus, reply.json()["stimulus_ids"][0])
                assert stimulus.target_agent_id == ids[0]
                assert stimulus.payload["reason"] == "reply_to_author"
    db.dispose()


def test_output_defaults_are_4096_but_respect_explicit_caps_and_run_budget():
    provider, key = "openai_compatible", "max_tokens"
    swarm = SwarmEngine(lambda: None)
    run = SimpleNamespace(config={}, max_tokens=10000, tokens_used=0)
    agent = SimpleNamespace(provider=provider, settings={})
    assert swarm._sampling_settings(agent, run)[key] == DEFAULT_AGENT_OUTPUT_TOKENS == 4096
    agent.settings = {"sampling": {key: 8000}}
    assert swarm._sampling_settings(agent, run)[key] == 8000
    run.tokens_used = 9900
    assert swarm._sampling_settings(agent, run)[key] == 100
    for seeded in DEFAULT_AGENTS:
        assert seeded["provider"] == "openai_compatible"
        assert seeded["settings"]["sampling"]["max_tokens"] == 4096
