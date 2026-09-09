"""Actual free-board schedules stay inclusive without manufacturing endless turns."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from swarmboard import autonomy, cadence, research
from swarmboard.app import _add_post_stimuli
from swarmboard.engine import SwarmEngine
from swarmboard.gateways import AgentAction
from swarmboard.models import Agent, Event, Run, Stimulus, Thread, Turn
from swarmboard.repository import Repository
from swarmboard.scheduler import CandidateScore, SchedulerConfig, WeightedFairScheduler, overlooked_participant
from .test_engine_acceptance import ScriptedGateway, make_database, wait_until


def seed(factory, *, participant_count=4, **kwargs):
    with factory.begin() as session:
        repo = Repository(session)
        agents = [repo.create_agent(handle=handle, provider="codex" if handle == "ada" else "openai_compatible",
                                   model="ada-astra" if handle == "ada" else "qwen/qwen3.8-27b",
                                   persona=f"Participant {handle}", permissions={"speak": True})
                  for handle in ("ng", "hiro", "ada", "zed", "bea", "cy", "eli")[:participant_count]]
        run = autonomy.create_session(repo, agents=agents, body="@ng, start us off.",
                                      seed=19, continuous=False, **kwargs)
        return run.id, {a.handle: a.id for a in agents}, repo.list_threads(run_id=run.id)[0].id


def loop(agent, messages):
    target = "hiro" if agent.handle == "ng" else "ng"
    return AgentAction(action="reply", body=f"@{target}, another thought from {agent.handle}.", intent="support")


def pending_fair(session, run_id):
    return [s for s in session.scalars(select(Stimulus).where(Stimulus.run_id == run_id,
            Stimulus.state.in_(["pending", "claimed", "processing"]))) if s.payload.get("free_fairness")]


def test_ada_sampling_doubles_effective_weight_and_wait_counts_pass_opportunities():
    captured = []
    rng = SimpleNamespace(choices=lambda candidates, *, weights, k: captured.append(weights) or [candidates[0]])
    candidates = [CandidateScore(handle, handle, "specialist", 1, True) for handle in ("ada", "ng")]
    WeightedFairScheduler(SchedulerConfig(ada_opportunity_multiplier=2))._weighted_pick(candidates, rng)
    assert captured[0][0] == 2 * captured[0][1]
    assert candidates[0].selection_weight == captured[0][0]
    agents = [SimpleNamespace(id=handle, handle=handle) for handle in ("ada", "ng", "zed")]
    assert overlooked_participant(agents, ["ng", "ng"])[0].id == "ada"
    # A selection that passed resets Ada's wait just like a posted response.
    assert overlooked_participant(agents, ["ng", "ng", "ada"])[0].id == "zed"


@pytest.mark.asyncio
@pytest.mark.parametrize("participant_count", [4, 7])
async def test_sustained_mentions_invite_every_voice_and_preserve_replies(participant_count):
    db, factory = make_database()
    rid, ids, _ = seed(factory, participant_count=participant_count)
    gateway = ScriptedGateway(*([loop] * 20))
    engine = SwarmEngine(factory, gateway=gateway)
    for _ in range(20):
        await engine.step(rid)
        with factory() as session:
            assert len(pending_fair(session, rid)) <= 1
    handles = {value: key for key, value in ids.items()}
    selected = [handles[call["agent_id"]] for call in gateway.calls]
    assert selected[:5] == ["ng", "hiro", "ada", "ng", "zed"]
    assert set(selected) == set(ids)
    assert selected.count("ada") >= 3 and selected.count("zed") >= 2
    with factory() as session:
        turns = list(session.scalars(select(Turn).where(Turn.run_id == rid)))
        assert all(turn.state == "completed" for turn in turns)
        for turn in turns:
            stimulus = session.get(Stimulus, turn.stimulus_id)
            if stimulus.payload.get("free_fairness"):
                assert "overlooked participant" in turn.scheduler_scores["reason"]
                # The participant's @mention is still routed, with its original post.
                assert session.scalar(select(Stimulus.id).where(
                    Stimulus.source_post_id == turn.resulting_post_id,
                    Stimulus.target_agent_id == ids["ng"], Stimulus.kind == "mention"))
        assert session.get(Run, rid).state == "paused"
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_fair_pass_does_not_spawn_more_work_and_all_passes_go_quiet():
    db, factory = make_database()
    rid, ids, tid = seed(factory)
    gateway = ScriptedGateway(loop, loop, *([AgentAction(action="pass")] * 8))
    engine = SwarmEngine(factory, gateway=gateway)
    for _ in range(12):
        await engine.step(rid)
    with factory() as session:
        stimuli = list(session.scalars(select(Stimulus).where(Stimulus.run_id == rid)))
        fair = [s for s in stimuli if s.payload.get("free_fairness")]
        assert len(fair) == 1 and fair[0].target_agent_id == ids["ada"]
        assert not any(s.payload.get("fallback_root_id") == fair[0].id for s in stimuli)
        assert not any(s.state in {"pending", "claimed", "processing"} for s in stimuli)
        assert session.get(Thread, tid).status == "dormant"
        assert 3 <= len(gateway.calls) <= 7
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_pending_invitation_survives_recovery_once_and_stays_paused():
    db, factory = make_database()
    rid, ids, _ = seed(factory)
    engine = SwarmEngine(factory, gateway=ScriptedGateway(loop, loop))
    await engine.step(rid)
    await engine.step(rid)
    with factory() as session:
        invitation_id = pending_fair(session, rid)[0].id
    await engine.shutdown()
    gateway = ScriptedGateway(AgentAction(action="pass"))
    recovered = SwarmEngine(factory, gateway=gateway)
    await recovered.recover()
    with factory() as session:
        assert session.get(Run, rid).state == "paused"
        assert [s.id for s in pending_fair(session, rid)] == [invitation_id]
    assert not gateway.calls
    await recovered.step(rid)
    assert [call["agent_id"] for call in gateway.calls] == [ids["ada"]]
    with factory() as session:
        assert len(list(session.scalars(select(Event).where(Event.run_id == rid,
            Event.event_type == "scheduler.fairness_invited")))) == 1
    await recovered.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_human_target_and_forced_turn_precede_fairness_without_replacing_it():
    db, factory = make_database()
    rid, ids, tid = seed(factory)
    # Contributions avoid the separate human-priority fallback chain after a pass.
    gateway = ScriptedGateway(loop, loop, loop, loop, AgentAction(action="pass"))
    engine = SwarmEngine(factory, gateway=gateway)
    await engine.step(rid)
    await engine.step(rid)
    with factory.begin() as session:
        repo = Repository(session)
        post = repo.create_post(thread_id=tid, body="@zed, please answer this.", author_type="human",
                                author_handle="collaborator").post
        _add_post_stimuli(repo, run_id=rid, thread_id=tid, post=post,
                          triggering_event_id=None, default_kind="human_post", default_priority=5)
        research.force_turn(repo, run_id=rid, thread_id=tid, agent_id=ids["hiro"], author="collaborator",
                            override_cooldown=True)
    await engine.step(rid)
    await engine.step(rid)
    await engine.step(rid)
    assert [c["agent_id"] for c in gateway.calls] == [ids[h] for h in ("ng", "hiro", "hiro", "zed", "ada")]
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_older_pending_human_target_prevents_a_new_fair_invitation():
    db, factory = make_database()
    rid, ids, tid = seed(factory)
    engine = SwarmEngine(factory, gateway=ScriptedGateway(loop, loop))
    await engine.step(rid)
    with factory.begin() as session:
        repo = Repository(session)
        post = repo.create_post(thread_id=tid, body="Please answer this, Zed.", author_type="human",
                                author_handle="collaborator").post
        repo.add_stimulus(run_id=rid, thread_id=tid, source_post_id=post.id,
                          target_agent_id=ids["zed"], kind="mention", priority=9)
    await engine.step(rid)
    with factory() as session:
        assert not pending_fair(session, rid)
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_continuous_fair_schedule_stops_at_its_original_budget():
    db, factory = make_database()
    rid, ids, _ = seed(factory, limits={"max_rounds": 12})
    with factory.begin() as session:
        session.get(Run, rid).continuous = True
    gateway = ScriptedGateway(*([loop] * 12))
    engine = SwarmEngine(factory, gateway=gateway)
    await engine.start(rid)
    def finished():
        with factory() as session:
            return session.get(Run, rid).state == "completed"
    await wait_until(finished, timeout=5)
    with factory() as session:
        run = session.get(Run, rid)
        assert run.rounds_used == 12
        assert {call["agent_id"] for call in gateway.calls} == set(ids.values())
        assert not pending_fair(session, rid)
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("session_type,cadence_mode", [("research", "free"), ("collaboration", cadence.NAME)])
async def test_research_and_cadence_do_not_use_free_fairness(session_type, cadence_mode):
    db, factory = make_database()
    rid, _, _ = seed(factory, session_type=session_type, cadence_mode=cadence_mode,
                     policy="permissive" if session_type == "research" else "production")
    engine = SwarmEngine(factory, gateway=ScriptedGateway(*([loop] * 6)))
    for _ in range(6):
        await engine.step(rid)
    with factory() as session:
        assert not list(session.scalars(select(Event).where(Event.run_id == rid,
                        Event.event_type == "scheduler.fairness_invited")))
        assert engine._scheduler_for(session.get(Run, rid)).config.ada_opportunity_multiplier == 1
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["disabled", "unavailable", "permission", "quota", "budget"])
async def test_fair_invitation_respects_availability_permissions_and_budgets(restriction):
    db, factory = make_database()
    rid, ids, _ = seed(factory)
    gateway = ScriptedGateway(loop, loop)
    engine = SwarmEngine(factory, gateway=gateway)
    await engine.step(rid)
    with factory.begin() as session:
        run = session.get(Run, rid)
        ada = session.get(Agent, ids["ada"])
        if restriction == "disabled":
            ada.enabled = False
        elif restriction == "unavailable":
            run.config = {**run.config, "unavailable_agent_ids": [ada.id]}
        elif restriction == "permission":
            ada.permissions = {"speak": False}
        elif restriction == "quota":
            run.config = {**run.config, "inherited_agent_quota_remaining": {ada.id: 0}}
        else:
            run.max_rounds = 2
    await engine.step(rid)
    with factory() as session:
        assert not pending_fair(session, rid)
        if restriction == "budget":
            assert session.get(Run, rid).state == "completed"
    await engine.shutdown()
    db.dispose()
