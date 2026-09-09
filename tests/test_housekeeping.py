from datetime import timedelta

import pytest

from swarmboard.engine import SwarmEngine
from swarmboard.gateways import AgentAction
from swarmboard.models import RunState, StimulusKind, StimulusState, TurnState, utc_now
from swarmboard.repository import Repository

from .test_engine_acceptance import ScriptedGateway, make_database, seed_conversation


@pytest.mark.asyncio
@pytest.mark.parametrize("exhausted_before_step", [False, True])
async def test_budget_completion_cancels_queued_work(exhausted_before_step):
    sql_engine, factory = make_database()
    run_id, thread_id, opening_id, _ = seed_conversation(factory, max_rounds=1)
    with factory.begin() as session:
        repo = Repository(session)
        queued_id = repo.add_stimulus(
            run_id=run_id, thread_id=thread_id, source_post_id=opening_id,
            kind=StimulusKind.NEW_EVIDENCE, priority=-1,
        ).id
        if exhausted_before_step:
            repo.increment_run_counters(run_id, rounds=1)
    gateway = ScriptedGateway(AgentAction(action="pass"))
    swarm = SwarmEngine(factory, gateway=gateway)
    await swarm.step(run_id)
    with factory() as session:
        repo = Repository(session)
        assert repo.get_run(run_id).state == RunState.COMPLETED.value
        assert repo.get_stimulus(queued_id).state == StimulusState.CANCELLED.value
        assert all(item.state in {"completed", "cancelled"} for item in repo.list_stimuli(run_id=run_id))
        cancellations = [event for event in repo.list_events(run_id=run_id) if event.event_type == "stimulus.cancelled"]
        assert len(cancellations) == (2 if exhausted_before_step else 1)
    assert len(gateway.calls) == (0 if exhausted_before_step else 1)
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_state", [RunState.COMPLETED, RunState.STOPPED, RunState.FAILED, RunState.EMERGENCY_STOPPED])
async def test_startup_cleans_legacy_terminal_work_once_without_touching_live_queues(terminal_state):
    sql_engine, factory = make_database()
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="tester", persona="Test", model="test")
        run = repo.create_run(seed=41)
        thread = repo.create_thread(title="Legacy", run_id=run.id)
        post = repo.create_human_post(thread.id, "Keep this history.").post
        stale_ids, turn_ids = [], []
        for state in ("pending", "claimed", "processing"):
            stimulus = repo.add_stimulus(run_id=run.id, thread_id=thread.id, kind=StimulusKind.NEW_EVIDENCE)
            stimulus.state = state
            stimulus.claim_token = f"old-{state}"
            stimulus.claimed_at = utc_now() - timedelta(hours=1)
            stimulus.updated_at = stimulus.claimed_at
            turn = repo.create_turn(
                run_id=run.id, thread_id=thread.id, agent_id=agent.id,
                stimulus_id=stimulus.id, claim_token=stimulus.claim_token,
            )
            if state != "pending":
                repo.mark_turn_calling(turn.id)
            stale_ids.append(stimulus.id)
            turn_ids.append(turn.id)
        finished = repo.add_stimulus(run_id=run.id, thread_id=thread.id, kind=StimulusKind.HUMAN_POST)
        finished.state = "completed"
        finished.completed_at = utc_now()
        finished_id = finished.id
        # Simulate a database written by the old completion path.
        run.state = terminal_state.value
        run.finished_at = utc_now()
        old_finished_at = run.finished_at
        run_id, thread_id, post_id = run.id, thread.id, post.id
        live_ids = []
        for state in (RunState.CREATED, RunState.PAUSED, RunState.RUNNING):
            live = repo.create_run(seed=1, state=state)
            live_thread = repo.create_thread(title="Live", run_id=live.id)
            live_ids.append(repo.add_stimulus(
                run_id=live.id, thread_id=live_thread.id, kind=StimulusKind.HUMAN_POST,
            ).id)
        old_events = [(event.id, event.event_type, dict(event.payload)) for event in repo.list_events()]
    gateway = ScriptedGateway()
    swarm = SwarmEngine(factory, gateway=gateway)
    await swarm.recover()
    with factory() as session:
        repo = Repository(session)
        for stimulus_id in stale_ids:
            stimulus = repo.get_stimulus(stimulus_id)
            assert stimulus.state == "cancelled"
            assert stimulus.claim_token is stimulus.claimed_at is None
            assert stimulus.completed_at is not None
        assert all(repo.get_turn(turn_id).state == TurnState.FAILED.value for turn_id in turn_ids)
        assert all(repo.get_turn(turn_id).claim_token is None for turn_id in turn_ids)
        assert all(repo.get_stimulus(stimulus_id).state == "pending" for stimulus_id in live_ids)
        assert repo.get_stimulus(finished_id).state == "completed"
        assert repo.get_run(run_id).finished_at == old_finished_at
        assert repo.get_run(run_id).rounds_used == repo.get_run(run_id).tokens_used == 0
        assert [item.id for item in repo.list_posts(thread_id)] == [post_id]
        events = [(event.id, event.event_type, dict(event.payload)) for event in repo.list_events()]
        assert events[:len(old_events)] == old_events
        assert len(events) == len(old_events) + 6
    await swarm.recover()
    with factory() as session:
        assert len(Repository(session).list_events()) == len(events)
    assert gateway.calls == []
    await swarm.shutdown()
    sql_engine.dispose()
