from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from swarmboard.database import init_db, make_engine, make_session_factory
from swarmboard.engine import EngineConfig, SwarmEngine
from swarmboard.gateways import AgentAction, GatewayError, GatewayResult, TokenUsage
from swarmboard.models import (
    Agent,
    AuthorType,
    Event,
    Post,
    Run,
    RunState,
    Stimulus,
    StimulusKind,
    StimulusState,
    Thread,
    ThreadStatus,
    Turn,
    TurnState,
    utc_now,
)
from swarmboard.repository import InvalidStateError, Repository


ScriptItem = AgentAction | BaseException | Callable[[Any, list[Any]], AgentAction]


class ScriptedGateway:
    """A deterministic provider boundary used only by orchestration tests."""

    def __init__(self, *script: ScriptItem) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        agent: Any,
        messages: list[Any],
        *,
        seed: int | None = None,
        sampling: dict[str, Any] | None = None,
    ) -> GatewayResult:
        self.calls.append(
            {
                "agent_id": agent.id,
                "messages": messages,
                "seed": seed,
                "sampling": sampling,
            }
        )
        if not self.script:
            raise AssertionError("scripted gateway received an unexpected model call")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        action = item(agent, messages) if callable(item) else item
        return GatewayResult(
            action=action,
            raw_output=action.model_dump_json(),
            provider="scripted-test-gateway",
            model="deterministic-test-model",
            latency_ms=1,
            usage=TokenUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )


class BlockingGateway(ScriptedGateway):
    def __init__(self, *script: ScriptItem) -> None:
        super().__init__(*script)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, *args: Any, **kwargs: Any) -> GatewayResult:
        self.entered.set()
        await self.release.wait()
        return await super().complete(*args, **kwargs)


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def make_database(url: str = "sqlite+pysqlite:///:memory:"):
    engine = make_engine(url)
    init_db(engine)
    return engine, make_session_factory(engine)


def seed_conversation(
    factory,
    *,
    continuous: bool = False,
    state: RunState = RunState.CREATED,
    max_rounds: int = 8,
    max_cascade_depth: int = 3,
) -> tuple[str, str, str, str]:
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(
            handle="tester",
            persona="Offer one useful proposal, then pass when the script says to pass.",
            role="proposer",
            provider="ollama",
            model="unused-in-tests",
            cooldown_seconds=0,
        )
        run = repo.create_run(
            seed=41,
            continuous=continuous,
            state=state,
            max_rounds=max_rounds,
            max_posts=12,
            max_tokens=2_000,
            max_duration_seconds=300,
            per_agent_quota=8,
            per_thread_quota=10,
            max_cascade_depth=max_cascade_depth,
        )
        thread = repo.create_thread(title="Bounded exchange", run_id=run.id)
        opening = repo.create_human_post(
            thread.id,
            "What is the smallest safe first step?",
            idempotency_key="opening-post",
        )
        stimulus = repo.add_stimulus(
            run_id=run.id,
            thread_id=thread.id,
            kind=StimulusKind.HUMAN_POST,
            triggering_event_id=opening.event.id,
            source_post_id=opening.post.id,
            dedupe_key=f"opening:{opening.post.id}",
        )
        return run.id, thread.id, opening.post.id, stimulus.id


@pytest.mark.asyncio
async def test_human_post_produces_a_bounded_threaded_exchange() -> None:
    """The first acceptance test proves the board can stop without invention."""

    sql_engine, factory = make_database()
    run_id, thread_id, opening_id, _ = seed_conversation(factory, max_rounds=2)
    with factory.begin() as session:
        Repository(session).create_agent(
            handle="reviewer",
            persona="Review once, and pass when no distinct contribution is needed.",
            role="critic",
            provider="ollama",
            model="unused-in-tests",
            cooldown_seconds=0,
        )
    gateway = ScriptedGateway(
        AgentAction(
            action="reply",
            parent_post_id=opening_id,
            title=None,
            body="Write down the constraint, then test one reversible step.",
            intent="support",
        ),
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None),
    )
    swarm = SwarmEngine(
        factory,
        gateway=gateway,
        config=EngineConfig(
            max_agents_per_stimulus=1,
            retry_backoff_seconds=0,
            idle_seconds=999,
            dormant_seconds=999,
        ),
    )

    first = await swarm.step(run_id)
    second = await swarm.step(run_id)
    third = await swarm.step(run_id)

    with factory() as session:
        repo = Repository(session)
        posts = repo.list_posts(thread_id)
        run = repo.get_run(run_id)
        turns = repo.list_turns(run_id=run_id)
        post_event = next(
            event
            for event in repo.list_events(run_id=run_id)
            if event.event_type == "post.created" and event.post_id == posts[-1].id
        )
    assert first.status == "processed"
    assert second.status == "processed"
    assert third.status in {"terminal", "budget_exhausted"}
    assert [post.author_type for post in posts] == [AuthorType.HUMAN.value, AuthorType.AGENT.value]
    assert posts[1].parent_post_id == opening_id
    assert len(gateway.calls) == 2
    assert {turn.state for turn in turns} == {"completed", "passed"}
    assert next(turn for turn in turns if turn.state == "passed").triggering_event_id == post_event.id
    assert run.state == RunState.COMPLETED.value
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_paused_run_never_calls_a_model() -> None:
    sql_engine, factory = make_database()
    run_id, _, _, _ = seed_conversation(
        factory,
        continuous=True,
        state=RunState.PAUSED,
    )
    gateway = ScriptedGateway(
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None)
    )
    swarm = SwarmEngine(
        factory,
        gateway=gateway,
        config=EngineConfig(poll_seconds=0.01, idle_seconds=999, dormant_seconds=999),
    )

    await swarm.recover()
    swarm.notify(run_id)
    await asyncio.sleep(0.04)

    assert gateway.calls == []
    with factory() as session:
        assert Repository(session).get_run(run_id).state == RunState.PAUSED.value
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_retry_commits_exactly_one_post() -> None:
    sql_engine, factory = make_database()
    run_id, thread_id, opening_id, _ = seed_conversation(factory, max_cascade_depth=0)
    gateway = ScriptedGateway(
        GatewayError("temporary provider failure", retryable=True),
        AgentAction(
            action="reply",
            parent_post_id=opening_id,
            title=None,
            body="One durable reply after a retry.",
            intent="clarify",
        ),
    )
    swarm = SwarmEngine(
        factory,
        gateway=gateway,
        config=EngineConfig(model_retries=1, retry_backoff_seconds=0),
    )

    result = await swarm.step(run_id)
    no_more_work = await swarm.step(run_id)

    with factory() as session:
        repo = Repository(session)
        posts = repo.list_posts(thread_id)
        turns = repo.list_turns(run_id=run_id)
        run = repo.get_run(run_id)
    assert result.status == "processed"
    assert no_more_work.status == "no_work"
    assert len([post for post in posts if post.author_type == AuthorType.AGENT.value]) == 1
    assert len(gateway.calls) == 2
    assert len(turns) == 1
    assert len(turns[0].retry_history) == 1
    assert run.model_calls == 2
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_selection_respects_single_remaining_round() -> None:
    sql_engine, factory = make_database()
    run_id, _, _, _ = seed_conversation(factory, max_rounds=1)
    with factory.begin() as session:
        Repository(session).create_agent(
            handle="second-round-candidate",
            persona="Pass if selected.",
            role="critic",
            provider="ollama",
            model="unused-in-tests",
            cooldown_seconds=0,
        )
    gateway = ScriptedGateway(
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None),
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None),
    )
    swarm = SwarmEngine(
        factory,
        gateway=gateway,
        config=EngineConfig(max_agents_per_stimulus=2),
    )

    result = await swarm.step(run_id)
    with factory() as session:
        repo = Repository(session)
        run = repo.get_run(run_id)
        turns = repo.list_turns(run_id=run_id)
    assert result.status == "processed"
    assert len(gateway.calls) == 1
    assert len(turns) == 1
    assert run.rounds_used == 1
    assert run.model_calls == 1
    assert run.state == RunState.COMPLETED.value
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_selection_respects_single_remaining_thread_slot() -> None:
    sql_engine, factory = make_database()
    run_id, thread_id, opening_id, _ = seed_conversation(factory, max_cascade_depth=0)
    with factory.begin() as session:
        repo = Repository(session)
        repo.get_run(run_id).per_thread_quota = 1
        repo.create_agent(
            handle="second-thread-candidate",
            persona="Offer a distinct reply.",
            role="critic",
            provider="ollama",
            model="unused-in-tests",
            cooldown_seconds=0,
        )
    gateway = ScriptedGateway(
        AgentAction(
            action="reply",
            parent_post_id=opening_id,
            title=None,
            body="First bounded thread reply.",
            intent="support",
        ),
        AgentAction(
            action="reply",
            parent_post_id=opening_id,
            title=None,
            body="Second reply must not be called.",
            intent="challenge",
        ),
    )
    swarm = SwarmEngine(
        factory,
        gateway=gateway,
        config=EngineConfig(max_agents_per_stimulus=2),
    )

    result = await swarm.step(run_id)
    with factory() as session:
        repo = Repository(session)
        run = repo.get_run(run_id)
        agent_posts = [
            post
            for post in repo.list_posts(thread_id)
            if post.author_type == AuthorType.AGENT.value
        ]
        turns = repo.list_turns(run_id=run_id)
    assert result.status == "processed"
    assert len(gateway.calls) == 1
    assert len(agent_posts) == 1
    assert len(turns) == 1
    assert run.rounds_used == 1
    assert run.model_calls == 1
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_pause_during_retry_yields_and_resume_finishes_once() -> None:
    sql_engine, factory = make_database()
    run_id, thread_id, opening_id, stimulus_id = seed_conversation(factory, max_cascade_depth=0)
    gateway = ScriptedGateway(
        GatewayError("retry after pause", retryable=True),
        AgentAction(
            action="reply",
            parent_post_id=opening_id,
            title=None,
            body="The resumed retry commits exactly once.",
            intent="clarify",
        ),
    )
    swarm = SwarmEngine(
        factory,
        gateway=gateway,
        config=EngineConfig(model_retries=1, retry_backoff_seconds=0.15),
    )

    first_step = asyncio.create_task(swarm.step(run_id))
    await wait_until(lambda: len(gateway.calls) == 1)
    await swarm.pause(run_id)
    paused_result = await first_step
    with factory() as session:
        repo = Repository(session)
        stimulus = repo.get_stimulus(stimulus_id)
        turn = repo.list_turns(run_id=run_id)[0]
    assert paused_result.status == "paused"
    assert stimulus.state == StimulusState.PENDING.value
    assert stimulus.attempts == 0
    assert turn.state == TurnState.SELECTED.value
    assert len(turn.retry_history) == 1
    assert len(gateway.calls) == 1

    assert await swarm.resume(run_id) == RunState.RUNNING.value
    resumed_result = await swarm.step(run_id)
    with factory() as session:
        repo = Repository(session)
        run = repo.get_run(run_id)
        posts = repo.list_posts(thread_id)
        turn = repo.list_turns(run_id=run_id)[0]
    assert resumed_result.status == "processed"
    assert len([post for post in posts if post.author_type == AuthorType.AGENT.value]) == 1
    assert turn.state == TurnState.COMPLETED.value
    assert len(gateway.calls) == 2
    assert run.rounds_used == 1
    assert run.model_calls == 2
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_internal_turn_error_requeues_then_succeeds_without_duplicate() -> None:
    sql_engine, factory = make_database()
    run_id, thread_id, opening_id, stimulus_id = seed_conversation(factory, max_cascade_depth=0)
    gateway = ScriptedGateway(
        AgentAction(
            action="reply",
            parent_post_id=opening_id,
            title=None,
            body="Recovered after one internal execution failure.",
            intent="support",
        )
    )
    swarm = SwarmEngine(
        factory,
        gateway=gateway,
        config=EngineConfig(retry_backoff_seconds=0),
    )
    real_execute = swarm._execute_turn
    executions = 0

    async def fail_once(turn_id: str, *, allow_manual_pause: bool):
        nonlocal executions
        executions += 1
        if executions == 1:
            raise RuntimeError("injected internal execution failure")
        return await real_execute(turn_id, allow_manual_pause=allow_manual_pause)

    swarm._execute_turn = fail_once  # type: ignore[method-assign]
    first = await swarm.step(run_id)
    with factory() as session:
        repo = Repository(session)
        stimulus = repo.get_stimulus(stimulus_id)
        turn = repo.list_turns(run_id=run_id)[0]
        requeues = [
            event
            for event in repo.list_events(run_id=run_id)
            if event.event_type == "turn.requeued"
        ]
    assert first.status == "failed"
    assert stimulus.state == StimulusState.PENDING.value
    assert turn.state == TurnState.SELECTED.value
    assert len(requeues) == 1
    assert gateway.calls == []

    second = await swarm.step(run_id)
    with factory() as session:
        repo = Repository(session)
        posts = repo.list_posts(thread_id)
        turns = repo.list_turns(run_id=run_id)
    assert second.status == "processed"
    assert len([post for post in posts if post.author_type == AuthorType.AGENT.value]) == 1
    assert len(turns) == 1
    assert turns[0].state == TurnState.COMPLETED.value
    assert len(gateway.calls) == 1
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_permission_revocation_fences_inflight_success() -> None:
    sql_engine, factory = make_database()
    run_id, thread_id, opening_id, _ = seed_conversation(factory, max_cascade_depth=0)
    gateway = BlockingGateway(
        AgentAction(
            action="reply",
            parent_post_id=opening_id,
            title=None,
            body="Revoked agent output must not commit.",
            intent="support",
        )
    )
    swarm = SwarmEngine(factory, gateway=gateway, config=EngineConfig(model_retries=2))
    in_flight = asyncio.create_task(swarm.step(run_id))
    await gateway.entered.wait()
    with factory.begin() as session:
        agent = Repository(session).list_agents()[0]
        agent.enabled = False
        agent.permissions = {"speak": False}
    gateway.release.set()
    result = await in_flight

    with factory() as session:
        repo = Repository(session)
        posts = repo.list_posts(thread_id)
        turn = repo.list_turns(run_id=run_id)[0]
    assert result.post_ids == []
    assert len(gateway.calls) == 1
    assert not [post for post in posts if post.author_type == AuthorType.AGENT.value]
    assert turn.state == TurnState.FAILED.value
    assert len(turn.retry_history) == 1
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_permission_revocation_prevents_a_retry_call() -> None:
    sql_engine, factory = make_database()
    run_id, thread_id, opening_id, _ = seed_conversation(factory, max_cascade_depth=0)
    gateway = BlockingGateway(
        GatewayError("first call is retryable", retryable=True),
        AgentAction(
            action="reply",
            parent_post_id=opening_id,
            title=None,
            body="A revoked second call must never happen.",
            intent="support",
        ),
    )
    swarm = SwarmEngine(
        factory,
        gateway=gateway,
        config=EngineConfig(model_retries=1, retry_backoff_seconds=0),
    )
    in_flight = asyncio.create_task(swarm.step(run_id))
    await gateway.entered.wait()
    with factory.begin() as session:
        agent = Repository(session).list_agents()[0]
        agent.permissions = {"speak": False}
    gateway.release.set()
    result = await in_flight

    with factory() as session:
        repo = Repository(session)
        posts = repo.list_posts(thread_id)
        turn = repo.list_turns(run_id=run_id)[0]
        run = repo.get_run(run_id)
    assert result.post_ids == []
    assert len(gateway.calls) == 1
    assert not [post for post in posts if post.author_type == AuthorType.AGENT.value]
    assert turn.state == TurnState.FAILED.value
    assert run.rounds_used == 1
    assert run.model_calls == 1
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_closed_source_fences_inflight_new_thread_without_retry() -> None:
    sql_engine, factory = make_database()
    run_id, thread_id, _, _ = seed_conversation(factory, max_cascade_depth=0)
    gateway = BlockingGateway(
        AgentAction(
            action="new_thread",
            parent_post_id=None,
            title="Must not be created",
            body="The source closed while this was in flight.",
            intent="support",
        )
    )
    swarm = SwarmEngine(factory, gateway=gateway, config=EngineConfig(model_retries=2))
    in_flight = asyncio.create_task(swarm.step(run_id))
    await gateway.entered.wait()
    with factory.begin() as session:
        Repository(session).set_thread_status(
            thread_id,
            ThreadStatus.CLOSED,
            reason="closed by human during call",
            actor_type=AuthorType.HUMAN.value,
            actor_id="human",
        )
    gateway.release.set()
    result = await in_flight

    with factory() as session:
        repo = Repository(session)
        threads = repo.list_threads(run_id=run_id)
        turn = repo.list_turns(run_id=run_id)[0]
    assert result.post_ids == []
    assert len(gateway.calls) == 1
    assert len(threads) == 1
    assert threads[0].id == thread_id
    assert turn.state == TurnState.FAILED.value
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_restart_preserves_pending_conversation(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'restart.db'}"
    first_engine, first_factory = make_database(database_url)
    run_id, thread_id, opening_id, stimulus_id = seed_conversation(first_factory)
    first_engine.dispose()

    second_engine, second_factory = make_database(database_url)
    gateway = ScriptedGateway(
        AgentAction(
            action="reply",
            parent_post_id=opening_id,
            title=None,
            body="The pending conversation survived the restart.",
            intent="support",
        )
    )
    swarm = SwarmEngine(second_factory, gateway=gateway)

    recovery = await swarm.recover()
    result = await swarm.step(run_id)

    with second_factory() as session:
        repo = Repository(session)
        stimulus = repo.get_stimulus(stimulus_id)
        posts = repo.list_posts(thread_id)
    assert recovery["stimuli_requeued"] == 0  # It was already durably pending.
    assert result.status == "processed"
    assert stimulus.state == StimulusState.COMPLETED.value
    assert len(posts) == 2
    assert len(gateway.calls) == 1
    await swarm.shutdown()
    second_engine.dispose()


@pytest.mark.asyncio
async def test_continuous_runner_failure_requeues_claim_pauses_and_resumes() -> None:
    sql_engine, factory = make_database()
    run_id, _, _, stimulus_id = seed_conversation(
        factory,
        continuous=True,
        state=RunState.CREATED,
    )
    gateway = ScriptedGateway(
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None)
    )
    swarm = SwarmEngine(
        factory,
        gateway=gateway,
        config=EngineConfig(poll_seconds=0.01, retry_backoff_seconds=0),
    )
    real_step = swarm._step_locked

    async def crash_after_claim(crash_run_id: str, *, explicit: bool):
        with factory.begin() as session:
            claimed = Repository(session).claim_stimuli(run_id=crash_run_id, limit=1)
            assert len(claimed) == 1
        raise RuntimeError("deliberate worker crash")

    swarm._step_locked = crash_after_claim  # type: ignore[method-assign]
    await swarm.start(run_id)

    def paused_and_requeued() -> bool:
        with factory() as session:
            return (
                Repository(session).get_run(run_id).state == RunState.PAUSED.value
                and Repository(session).get_stimulus(stimulus_id).state
                == StimulusState.PENDING.value
            )

    await wait_until(paused_and_requeued)
    with factory() as session:
        event_types = [event.event_type for event in Repository(session).list_events(run_id=run_id)]
    assert "stimulus.recovered" in event_types
    assert "run.worker_failed" in event_types
    assert "run.paused" in event_types

    swarm._step_locked = real_step  # type: ignore[method-assign]
    assert await swarm.resume(run_id) == RunState.RUNNING.value

    def completed() -> bool:
        with factory() as session:
            return (
                Repository(session).get_stimulus(stimulus_id).state
                == StimulusState.COMPLETED.value
            )

    await wait_until(completed)
    assert len(gateway.calls) == 1
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_notify_recreates_a_missing_running_worker() -> None:
    sql_engine, factory = make_database()
    run_id, _, _, stimulus_id = seed_conversation(
        factory,
        continuous=True,
        state=RunState.RUNNING,
    )
    gateway = ScriptedGateway(
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None)
    )
    swarm = SwarmEngine(factory, gateway=gateway, config=EngineConfig(poll_seconds=0.01))

    assert run_id not in swarm._tasks
    swarm.notify(run_id)

    def completed() -> bool:
        with factory() as session:
            return (
                Repository(session).get_stimulus(stimulus_id).state
                == StimulusState.COMPLETED.value
            )

    await wait_until(completed)
    assert len(gateway.calls) == 1
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_recovered_delivery_token_rejects_a_late_model_result() -> None:
    """Delivery A cannot commit while recovered delivery B owns the same turn."""

    sql_engine, factory = make_database()
    run_id, thread_id, opening_id, _ = seed_conversation(factory, max_cascade_depth=0)
    action = AgentAction(
        action="reply",
        parent_post_id=opening_id,
        title=None,
        body="Only the current lease may commit this reply.",
        intent="clarify",
    )
    gateway_a = BlockingGateway(action)
    gateway_b = BlockingGateway(action)
    swarm_a = SwarmEngine(factory, gateway=gateway_a)
    swarm_b = SwarmEngine(factory, gateway=gateway_b)

    delivery_a = asyncio.create_task(swarm_a.step(run_id))
    await gateway_a.entered.wait()
    recovery = await swarm_b._recover_inflight_work(
        run_id=run_id,
        stale_before=utc_now() + timedelta(seconds=1),
        reason="staged lease recovery test",
    )
    assert recovery["stimuli_requeued"] == 1
    assert recovery["turns_reopened"] == 1

    delivery_b = asyncio.create_task(swarm_b.step(run_id))
    await gateway_b.entered.wait()
    with factory() as session:
        current_turn = Repository(session).list_turns(run_id=run_id)[0]
        token_b = current_turn.claim_token
        assert current_turn.state == TurnState.CALLING.value
        assert token_b is not None

    gateway_a.release.set()
    result_a = await delivery_a
    with factory() as session:
        assert not [
            post
            for post in Repository(session).list_posts(thread_id)
            if post.author_type == AuthorType.AGENT.value
        ]
        current_turn = Repository(session).list_turns(run_id=run_id)[0]
        assert current_turn.state == TurnState.CALLING.value
        assert current_turn.claim_token == token_b
    assert result_a.post_ids == []

    gateway_b.release.set()
    result_b = await delivery_b
    with factory() as session:
        agent_posts = [
            post
            for post in Repository(session).list_posts(thread_id)
            if post.author_type == AuthorType.AGENT.value
        ]
    assert len(agent_posts) == 1
    assert result_b.post_ids == [agent_posts[0].id]
    await swarm_a.shutdown()
    await swarm_b.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["stop", "emergency_stop"])
async def test_stop_controls_terminalize_all_work_and_fence_inflight_result(
    control: str,
) -> None:
    sql_engine, factory = make_database()
    run_id, thread_id, opening_id, _ = seed_conversation(factory, max_cascade_depth=0)
    with factory.begin() as session:
        Repository(session).add_stimulus(
            run_id=run_id,
            thread_id=thread_id,
            kind=StimulusKind.NEW_EVIDENCE,
            source_post_id=opening_id,
            priority=-1,
            dedupe_key=f"pending-during-{control}",
        )
    gateway = BlockingGateway(
        AgentAction(
            action="reply",
            parent_post_id=opening_id,
            title=None,
            body="This late result must be fenced.",
            intent="support",
        )
    )
    swarm = SwarmEngine(factory, gateway=gateway)
    in_flight = asyncio.create_task(swarm.step(run_id))
    await gateway.entered.wait()

    if control == "stop":
        state = await swarm.stop(run_id, reason="test stop")
        assert state == RunState.STOPPED.value
    else:
        state = await swarm.emergency_stop(run_id, reason="test emergency stop")
        assert state == RunState.EMERGENCY_STOPPED.value

    with factory() as session:
        repo = Repository(session)
        stimuli = repo.list_stimuli(run_id=run_id)
        turns = repo.list_turns(run_id=run_id)
        events = repo.list_events(run_id=run_id)
    assert stimuli and all(item.state == StimulusState.CANCELLED.value for item in stimuli)
    assert turns and all(turn.state == TurnState.FAILED.value for turn in turns)
    assert len([event for event in events if event.event_type == "stimulus.cancelled"]) == 2
    assert len([event for event in events if event.event_type == "turn.failed"]) == 1

    gateway.release.set()
    late_result = await in_flight
    with factory() as session:
        posts = Repository(session).list_posts(thread_id)
        stimuli = Repository(session).list_stimuli(run_id=run_id)
        turns = Repository(session).list_turns(run_id=run_id)
    assert late_result.post_ids == []
    assert not [post for post in posts if post.author_type == AuthorType.AGENT.value]
    assert all(item.state == StimulusState.CANCELLED.value for item in stimuli)
    assert all(turn.state == TurnState.FAILED.value for turn in turns)
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_expired_claim_is_recovered_but_fresh_claim_is_not_stolen() -> None:
    sql_engine, factory = make_database()
    old_run, _, _, old_stimulus_id = seed_conversation(factory)
    with factory.begin() as session:
        old_claim = Repository(session).claim_stimuli(run_id=old_run, limit=1)[0]
        old_claim.claimed_at = utc_now() - timedelta(seconds=20)
        old_claim.updated_at = utc_now() - timedelta(seconds=20)
    old_gateway = ScriptedGateway(
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None)
    )
    old_swarm = SwarmEngine(
        factory,
        gateway=old_gateway,
        config=EngineConfig(claim_lease_seconds=5),
    )
    assert (await old_swarm.step(old_run)).status == "processed"
    with factory() as session:
        assert Repository(session).get_stimulus(old_stimulus_id).state == StimulusState.COMPLETED.value
        recovered = [
            event
            for event in Repository(session).list_events(run_id=old_run)
            if event.event_type == "stimulus.recovered"
        ]
    assert len(recovered) == 1

    with factory.begin() as session:
        repo = Repository(session)
        fresh_run_record = repo.create_run(seed=42)
        fresh_thread = repo.create_thread(title="Fresh lease", run_id=fresh_run_record.id)
        fresh_opening = repo.create_human_post(
            fresh_thread.id,
            "Do not steal a fresh claim.",
            idempotency_key="fresh-opening-post",
        )
        fresh_stimulus = repo.add_stimulus(
            run_id=fresh_run_record.id,
            thread_id=fresh_thread.id,
            kind=StimulusKind.HUMAN_POST,
            source_post_id=fresh_opening.post.id,
            dedupe_key="fresh-opening-stimulus",
        )
        fresh_run = fresh_run_record.id
        fresh_stimulus_id = fresh_stimulus.id
        fresh_claim = repo.claim_stimuli(run_id=fresh_run, limit=1)[0]
        fresh_token = fresh_claim.claim_token
    fresh_gateway = ScriptedGateway(
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None)
    )
    fresh_swarm = SwarmEngine(
        factory,
        gateway=fresh_gateway,
        config=EngineConfig(claim_lease_seconds=60),
    )
    assert (await fresh_swarm.step(fresh_run)).status == "no_work"
    with factory() as session:
        fresh = Repository(session).get_stimulus(fresh_stimulus_id)
        assert fresh.state == StimulusState.CLAIMED.value
        assert fresh.claim_token == fresh_token
    assert fresh_gateway.calls == []
    await old_swarm.shutdown()
    await fresh_swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_virtual_time_and_reactive_mentions_are_durable() -> None:
    sql_engine, factory = make_database()
    run_id, _, opening_id, stimulus_id = seed_conversation(factory)
    with factory.begin() as session:
        repo = Repository(session)
        tester = next(agent for agent in repo.list_agents() if agent.handle == "tester")
        tester.cooldown_seconds = 30
        expert = repo.create_agent(
            handle="expert",
            persona="Verify direct questions.",
            role="fact-checker",
            provider="ollama",
            model="unused-in-tests",
            cooldown_seconds=0,
        )
        run = repo.get_run(run_id)
        run.config = {"use_virtual_time": True, "virtual_time_step_seconds": 7}
        stimulus = repo.get_stimulus(stimulus_id)
        stimulus.target_agent_id = tester.id
        expert_id = expert.id
        tester_id = tester.id
    gateway = ScriptedGateway(
        AgentAction(
            action="reply",
            parent_post_id=opening_id,
            title=None,
            body="@expert can you verify this constraint?",
            intent="clarify",
        ),
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None),
    )
    swarm = SwarmEngine(factory, gateway=gateway)

    assert (await swarm.step(run_id)).status == "processed"
    with factory() as session:
        repo = Repository(session)
        run = repo.get_run(run_id)
        tester = repo.get_agent(tester_id)
        pending = [
            item
            for item in repo.list_stimuli(run_id=run_id)
            if item.state == StimulusState.PENDING.value
        ]
        virtual_events = [
            event
            for event in repo.list_events(run_id=run_id)
            if event.event_type == "run.virtual_time_advanced"
        ]
    assert run.virtual_time == 7
    assert tester.last_spoke_at == run.started_at
    assert len(virtual_events) == 1
    assert len(pending) == 1
    assert pending[0].kind == StimulusKind.MENTION.value
    assert pending[0].target_agent_id == expert_id

    mention_id = pending[0].id
    with factory.begin() as session:
        repo = Repository(session)
        expert = repo.get_agent(expert_id)
        run = repo.get_run(run_id)
        expert.cooldown_seconds = 30
        expert.last_spoke_at = run.started_at

    deferred = await swarm.step(run_id)
    with factory() as session:
        repo = Repository(session)
        run = repo.get_run(run_id)
        mention = repo.get_stimulus(mention_id)
        deferred_events = [
            event
            for event in repo.list_events(run_id=run_id)
            if event.event_type == "stimulus.deferred"
        ]
    assert deferred.status == "processed"
    assert len(gateway.calls) == 1
    assert mention.state == StimulusState.PENDING.value
    assert mention.attempts == 0
    assert run.virtual_time == 30
    assert len(deferred_events) == 1

    eventual = await swarm.step(run_id)
    with factory() as session:
        mention = Repository(session).get_stimulus(mention_id)
    assert eventual.status == "processed"
    assert mention.state == StimulusState.COMPLETED.value
    assert len(gateway.calls) == 2
    assert gateway.calls[-1]["agent_id"] == expert_id
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_rerun_clones_only_inputs_once_and_rejects_interleaving() -> None:
    sql_engine, factory = make_database()
    run_id, thread_id, opening_id, _ = seed_conversation(factory)
    with factory.begin() as session:
        repo = Repository(session)
        followup = repo.create_human_post(
            thread_id,
            "A second input supplied before generation.",
            parent_post_id=opening_id,
            idempotency_key="second-source-input",
        )
        agent = repo.list_agents()[0]
        output_thread = repo.create_thread(
            title="Agent-only output",
            run_id=run_id,
            actor_type=AuthorType.AGENT.value,
            actor_id=agent.id,
        )
        repo.create_agent_post(
            output_thread.id,
            agent.id,
            "Generated branch that must not be cloned.",
            idempotency_key="source-agent-branch",
        )
        followup_id = followup.post.id

    gateway = BlockingGateway(
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None)
    )
    swarm = SwarmEngine(factory, gateway=gateway)
    clone_id = await swarm.rerun(run_id, continuous=True)
    await gateway.entered.wait()
    with factory() as session:
        repo = Repository(session)
        clone = repo.get_run(clone_id)
        clone_threads = repo.list_threads(run_id=clone_id)
        clone_posts = repo.list_posts(clone_threads[0].id)
        clone_stimuli = repo.list_stimuli(run_id=clone_id)
    assert clone.state == RunState.RUNNING.value
    assert len(clone_threads) == 1
    assert len(clone_posts) == 2
    assert len(clone_stimuli) == 1
    assert all(clone_id in (post.idempotency_key or "") for post in clone_posts)
    assert clone_stimuli[0].source_post_id == clone_posts[-1].id

    gateway.release.set()
    await wait_until(lambda: len(gateway.calls) == 1)
    with factory.begin() as session:
        Repository(session).create_human_post(
            thread_id,
            "This input arrived after generated output.",
            parent_post_id=followup_id,
            idempotency_key="interleaved-source-input",
        )
    with pytest.raises(InvalidStateError, match="interleaved"):
        await swarm.rerun(run_id, continuous=False)
    await swarm.shutdown()
    sql_engine.dispose()


@pytest.mark.asyncio
async def test_rerun_preserves_unattached_opening_post_budget_baseline() -> None:
    sql_engine, factory = make_database()
    with factory.begin() as session:
        repo = Repository(session)
        repo.create_agent(
            handle="budget-tester",
            persona="Pass after observing the input.",
            role="critic",
            provider="ollama",
            model="unused-in-tests",
            cooldown_seconds=0,
        )
        source = repo.create_run(seed=7, max_posts=1)
        source_thread = repo.create_thread(title="Attached later", run_id=None)
        opening = repo.create_human_post(
            source_thread.id,
            "This opening predates attachment to the run.",
            idempotency_key="pre-run-opening",
        )
        source_thread.run_id = source.id
        repo.add_stimulus(
            run_id=source.id,
            thread_id=source_thread.id,
            kind=StimulusKind.HUMAN_POST,
            source_post_id=opening.post.id,
            dedupe_key="pre-run-opening-stimulus",
        )
        source_id = source.id

    gateway = BlockingGateway(
        AgentAction(action="pass", parent_post_id=None, title=None, body=None, intent=None)
    )
    swarm = SwarmEngine(factory, gateway=gateway)
    clone_id = await swarm.rerun(source_id, continuous=True)
    await gateway.entered.wait()
    with factory() as session:
        clone = Repository(session).get_run(clone_id)
        assert clone.posts_used == 0
        assert clone.max_posts == 1
        # Starting a continuous clone initializes its own step_once=False; the
        # source's ephemeral value must never survive as True.
        assert clone.config.get("step_once") is not True
    gateway.release.set()
    await wait_until(lambda: len(gateway.calls) == 1)
    await swarm.shutdown()
    sql_engine.dispose()
