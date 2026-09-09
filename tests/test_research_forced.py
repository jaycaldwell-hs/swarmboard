"""Forced invitations and exact-prompt resampling retain the normal ledger fences."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from swarmboard import autonomy
from swarmboard.app import create_app
from swarmboard.gateways import AgentAction
from swarmboard.models import Event, Post, Run, Stimulus, Thread, Turn, utc_now
from swarmboard.repository import Repository
from swarmboard.scheduler import WeightedFairScheduler
from tests.test_engine_acceptance import ScriptedGateway, wait_until


@pytest.fixture
async def force_client(tmp_path, monkeypatch):
    for name in ("SWARMBOARD_AUTH_USERS", "SWARMBOARD_REQUIRE_AUTH", "SWARMBOARD_HOSTED", "RENDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SWARMBOARD_LOCAL_OPERATOR", "researcher")
    gateway = ScriptedGateway()
    app = create_app(database_url=f"sqlite:///{tmp_path / 'forced.db'}", gateway=gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield app, client, gateway


def prepared_session(app, *, session_type="collaboration", policy="production", continuous=False):
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        agents = repo.list_agents(enabled_only=True)[:2]
        run = autonomy.create_session(repo, agents=agents, body="An opening for the participants.",
                                      session_type=session_type, policy=policy, continuous=continuous,
                                      author_handle="researcher", limits={"max_rounds": 20})
        thread = session.scalar(select(Thread).where(Thread.run_id == run.id))
        # Consume setup invitations so these tests isolate one forced stimulus.
        for stimulus in repo.claim_stimuli(run_id=run.id, limit=100):
            repo.complete_stimulus(stimulus.id, claim_token=stimulus.claim_token)
        return run.id, thread.id, [agent.id for agent in agents]


async def force(client, rid, aid, **kwargs):
    response = await client.post(f"/api/runs/{rid}/force-turn", json={
        "agent_id": aid, "idempotency_key": "force-test", **kwargs})
    assert response.status_code in (200, 201), response.text
    return response.json()


def ledger_counts(app):
    with app.state.session_factory() as session:
        # Rejected HTTP actions can have a request audit; no domain write is allowed.
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in (Run, Thread, Post, Stimulus, Turn))


@pytest.mark.asyncio
@pytest.mark.parametrize("session_type", ["collaboration", "research"])
async def test_forced_turn_bypasses_candidate_selection_and_records_human_without_pass_fallback(force_client, monkeypatch, session_type):
    app, client, gateway = force_client
    rid, tid, ids = prepared_session(app, session_type=session_type)
    gateway.script.append(AgentAction(action="pass"))
    def unexpected_selection(*args, **kwargs):
        raise AssertionError("forced turns must bypass weighted candidate selection")
    monkeypatch.setattr(WeightedFairScheduler, "select", unexpected_selection)
    queued = await force(client, rid, ids[1], thread_id=tid)
    replay = await force(client, rid, ids[1], thread_id=tid)
    assert replay == queued
    await app.state.engine.step(rid)
    with app.state.session_factory() as session:
        turn = session.scalar(select(Turn).where(Turn.stimulus_id == queued["stimulus_id"]))
        assert turn.agent_id == ids[1] and turn.outcome == "passed"
        assert turn.scheduler_scores["forced"] is True
        assert turn.scheduler_scores["forced_by"] == "researcher"
        pending = list(session.scalars(select(Stimulus).where(Stimulus.run_id == rid, Stimulus.state == "pending")))
        assert pending == []
        events = list(session.scalars(select(Event).where(Event.run_id == rid, Event.actor_type == "human")))
        assert any(event.actor_id == "researcher" and event.agent_id == ids[1] for event in events)
    assert [call["agent_id"] for call in gateway.calls] == [ids[1]]


def prior_generated_turn(app, rid, tid, aid):
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        participant = repo.get_agent(aid)
        participant.cooldown_seconds = 3600
        generated = repo.create_agent_post(tid, aid, "The prior generated contribution.").post
        turn = repo.create_turn(run_id=rid, thread_id=tid, agent_id=aid)
        repo.finish_turn(turn.id, state="completed", resulting_post_id=generated.id)
        human = repo.create_human_post(tid, "A human follow-up resets the speaking streak.", author_handle="researcher").post
        return human.id


@pytest.mark.asyncio
@pytest.mark.parametrize("session_type,profile,override,commits", [
    ("collaboration", "production", False, False),
    ("collaboration", "production", True, True),
    ("research", "production", False, False),
    ("research", "production", True, True),
    ("research", "permissive", False, True),
])
async def test_forced_cooldown_respects_profile_and_explicit_override(force_client, session_type, profile, override, commits):
    app, client, gateway = force_client
    rid, tid, ids = prepared_session(app, session_type=session_type, policy=profile)
    post_id = prior_generated_turn(app, rid, tid, ids[0])
    gateway.script.append(AgentAction(action="reply", body="The forced response.", intent="clarify"))
    queued = await force(client, rid, ids[0], stimulus_post_id=post_id, override_cooldown=override)
    await app.state.engine.step(rid)
    with app.state.session_factory() as session:
        stimulus = session.get(Stimulus, queued["stimulus_id"])
        turns = list(session.scalars(select(Turn).where(Turn.stimulus_id == stimulus.id)))
        if commits:
            assert len(turns) == 1 and turns[0].outcome == "executed"
            assert turns[0].scheduler_scores["override_cooldown"] is override
        else:
            assert turns == [] and stimulus.state == "pending" and stimulus.attempts == 0
            assert stimulus.not_before > utc_now() + timedelta(minutes=50)
    assert len(gateway.calls) == int(commits)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["closed", "stopped", "completed", "emergency_stopped"])
async def test_forced_turn_rejects_frozen_conversations_without_domain_side_effects(force_client, state):
    app, client, _ = force_client
    rid, tid, ids = prepared_session(app)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        if state == "closed":
            repo.set_thread_status(tid, "closed")
        else:
            repo.set_run_state(rid, state)
    before = ledger_counts(app)
    response = await client.post(f"/api/runs/{rid}/force-turn", json={
        "agent_id": ids[0], "thread_id": tid, "idempotency_key": "frozen"})
    assert response.status_code == 409, response.text
    assert ledger_counts(app) == before


@pytest.mark.asyncio
async def test_forced_turn_rejects_outside_roster_and_wrong_thread_source(force_client):
    app, client, _ = force_client
    rid, tid, ids = prepared_session(app)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        outside_agent = next(a for a in repo.list_agents() if a.id not in ids)
        unrelated = repo.create_thread(title="Not in the session")
        unrelated_post = repo.create_human_post(unrelated.id, "Other discussion").post
        outsider, bad_post = outside_agent.id, unrelated_post.id
    before = ledger_counts(app)
    for fields in ({"agent_id": outsider}, {"agent_id": ids[0], "stimulus_post_id": bad_post}):
        response = await client.post(f"/api/runs/{rid}/force-turn", json={
            "thread_id": tid, "idempotency_key": "outside", **fields})
        assert response.status_code == 409, response.text
        assert ledger_counts(app) == before


@pytest.mark.asyncio
async def test_continuous_worker_consumes_forced_stimulus(force_client):
    app, client, gateway = force_client
    rid, tid, ids = prepared_session(app, continuous=True)
    gateway.script.append(AgentAction(action="pass"))
    await app.state.engine.start(rid)
    queued = await force(client, rid, ids[0], thread_id=tid)
    def completed():
        with app.state.session_factory() as session:
            turn = session.scalar(select(Turn).where(Turn.stimulus_id == queued["stimulus_id"]))
            return turn is not None and turn.state == "passed"
    await wait_until(completed, timeout=3)
    assert len(gateway.calls) == 1


@pytest.mark.asyncio
async def test_forced_stimulus_queued_before_thread_closure_never_calls_provider(force_client):
    app, client, gateway = force_client
    rid, tid, ids = prepared_session(app)
    queued = await force(client, rid, ids[0], thread_id=tid)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        # Keep the run viable while its specifically selected thread closes.
        repo.create_thread(run_id=rid, title="Another open thread")
        repo.set_thread_status(tid, "closed")
    await app.state.engine.step(rid)
    assert gateway.calls == []
    with app.state.session_factory() as session:
        assert session.scalar(select(Turn).where(Turn.stimulus_id == queued["stimulus_id"])) is None


@pytest.mark.asyncio
async def test_resample_reuses_prompt_exactly_and_maps_reply_parent_without_changing_raw(force_client):
    app, client, gateway = force_client
    rid, tid, ids = prepared_session(app, session_type="research", policy="permissive")
    with app.state.session_factory() as session:
        original_parent = session.scalar(select(Post).where(Post.thread_id == tid)).id
    action = AgentAction(action="reply", parent_post_id=original_parent,
                         body="A sampled response to the opening.", intent="clarify")
    gateway.script.append(action)
    initial = await force(client, rid, ids[0], thread_id=tid)
    await app.state.engine.step(rid)
    with app.state.session_factory() as session:
        original_turn = session.scalar(select(Turn).where(Turn.stimulus_id == initial["stimulus_id"]))
        turn_id, captured_prompt = original_turn.id, original_turn.prompt
        original_post = original_turn.resulting_post_id
        source_events = [event.id for event in session.scalars(select(Event).where(Event.run_id == rid))]
    response = await client.post(f"/api/turns/{turn_id}/resample", json={
        "n": 2, "reuse_prompt": True, "idempotency_key": "resample-pair"})
    assert response.status_code in (200, 201), response.text
    siblings = response.json()["forks"]
    assert len(siblings) == 2
    for sibling in siblings:
        gateway.script.append(action)
        await app.state.engine.step(sibling["run_id"])
        with app.state.session_factory() as session:
            turn = session.scalar(select(Turn).where(Turn.run_id == sibling["run_id"]))
            result = session.get(Post, turn.resulting_post_id)
            inherited = session.scalar(select(Post).where(Post.thread_id == sibling["thread_id"], Post.sequence == 1))
            assert turn.prompt == captured_prompt
            assert hashlib.sha256(turn.prompt.encode()).hexdigest() == hashlib.sha256(captured_prompt.encode()).hexdigest()
            assert turn.raw_output == action.model_dump_json()
            assert turn.parsed_action["parent_post_id"] == original_parent
            assert result.parent_post_id == inherited.id and inherited.id != original_parent
            assert result.id != original_post and not result.metadata_json.get("is_inherited")
            assert session.get(Run, sibling["run_id"]).config["session_type"] == "research"
        assert [message.model_dump() for message in gateway.calls[-1]["messages"]] == json.loads(captured_prompt)
    with app.state.session_factory() as session:
        assert [event.id for event in session.scalars(select(Event).where(Event.run_id == rid))] == source_events
        assert session.get(Post, original_post) is not None
    replay = await client.post(f"/api/turns/{turn_id}/resample", json={
        "n": 2, "reuse_prompt": True, "idempotency_key": "resample-pair"})
    assert replay.json() == response.json()
    assert len(gateway.calls) == 3

    # A reused prompt can still reference an ancestor's post IDs when its
    # generated variant is sampled again; map those aliases through the chain.
    with app.state.session_factory() as session:
        variant = session.scalar(select(Turn).where(Turn.run_id == siblings[0]["run_id"]))
        variant_id = variant.id
    nested_response = await client.post(f"/api/turns/{variant_id}/resample", json={
        "n": 1, "reuse_prompt": True, "idempotency_key": "resample-the-resample"})
    assert nested_response.status_code == 201, nested_response.text
    nested = nested_response.json()["forks"][0]
    gateway.script.append(action)
    await app.state.engine.step(nested["run_id"])
    with app.state.session_factory() as session:
        nested_turn = session.scalar(select(Turn).where(Turn.run_id == nested["run_id"]))
        assert nested_turn.outcome == "executed" and nested_turn.prompt == captured_prompt
        assert session.get(Post, nested_turn.resulting_post_id).parent_post_id == nested["post_id_map"][original_parent]
        assert len(nested["lineage"]["ancestors"]) == 2
    assert len(gateway.calls) == 4


@pytest.mark.asyncio
async def test_resample_without_prompt_reuse_rebuilds_current_persona_and_child_history(force_client):
    app, client, gateway = force_client
    rid, tid, ids = prepared_session(app)
    gateway.script.append(AgentAction(action="pass"))
    queued = await force(client, rid, ids[0], thread_id=tid)
    await app.state.engine.step(rid)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        original = session.scalar(select(Turn).where(Turn.stimulus_id == queued["stimulus_id"]))
        original_id, original_prompt = original.id, original.prompt
        repo.get_agent(ids[0]).persona = "CURRENT_PERSONA_ONLY_FOR_THE_NEW_SAMPLE"
    response = await client.post(f"/api/turns/{original_id}/resample", json={
        "n": 1, "reuse_prompt": False, "idempotency_key": "rebuilt-sample"})
    assert response.status_code == 201, response.text
    sibling = response.json()["forks"][0]
    gateway.script.append(AgentAction(action="pass"))
    await app.state.engine.step(sibling["run_id"])
    with app.state.session_factory() as session:
        turn = session.scalar(select(Turn).where(Turn.run_id == sibling["run_id"]))
        assert turn.outcome == "passed" and turn.prompt != original_prompt
        assert "CURRENT_PERSONA_ONLY_FOR_THE_NEW_SAMPLE" in turn.prompt
        assert all(post["thread_id"] == sibling["thread_id"] for post in turn.context_snapshot["posts"])
        assert session.get(Run, sibling["run_id"]).config["session_type"] == "research"
