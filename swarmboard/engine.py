"""Async, event-driven orchestration engine for the local Swarmboard process.

SQLite is the durable queue and audit trail.  The asyncio tasks in this module
are only workers: losing the process cannot lose a pending conversation.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol, Sequence
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import sessions, autonomy, cadence
from . import run_policy
from .config import DEFAULT_AGENT_OUTPUT_TOKENS
from .context_views import agent_snapshot, intervention_snapshot, participant_post, participant_posts, persona_identity, text_hash
from .gateways import (
    AgentAction,
    ChatMessage,
    GatewayError,
    GatewayResult,
    ModelGateway,
    StructuredOutputError,
    TokenUsage,
    action_json_schema,
    captured_json,
    parse_agent_action,
)
from .models import (
    Agent,
    AuthorType,
    Event,
    Memory,
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
from .personas import SYSTEM_PROMPT
from .persona_context import HARNESS_PROMPT_VERSION, harness_prompt, persona_snapshot
from .policies import ActionPolicy
from .repository import ClaimConflictError, InvalidStateError, Repository
from .scheduler import CandidateScore, SchedulingDecision, WeightedFairScheduler
from .stimuli import plan_reactive_stimuli


logger = logging.getLogger(__name__)


class _TurnDeferredForPause(Exception):
    """Internal control flow: retain the turn and yield its stimulus lease."""


PublishCallback = Callable[[dict[str, Any]], Awaitable[None] | None]
SessionFactory = Callable[[], Session]


class GatewayDispatcher(Protocol):
    async def complete(
        self,
        agent: Any,
        messages: Sequence[ChatMessage | Mapping[str, str]],
        *,
        seed: int | None = None,
        sampling: Mapping[str, Any] | None = None,
    ) -> GatewayResult: ...


@dataclass(frozen=True, slots=True)
class EngineConfig:
    poll_seconds: float = 0.35
    context_post_limit: int = 40
    memory_limit: int = 12
    claim_lease_seconds: int = 180
    model_retries: int = 2
    retry_backoff_seconds: float = 0.35
    idle_seconds: float = 45.0
    dormant_seconds: float = 300.0
    max_agents_per_stimulus: int = 2
    prompt_version: str = "swarmboard-v1"
    virtual_time_step_seconds: float = 1.0


StepStatus = Literal[
    "processed",
    "no_work",
    "paused",
    "terminal",
    "budget_exhausted",
    "failed",
]


@dataclass(slots=True)
class StepResult:
    status: StepStatus
    run_id: str
    stimulus_id: str | None = None
    turn_ids: list[str] = field(default_factory=list)
    post_ids: list[str] = field(default_factory=list)
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _event_dict(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "uuid": event.uuid,
        "event_type": event.event_type,
        "run_id": event.run_id,
        "thread_id": event.thread_id,
        "post_id": event.post_id,
        "agent_id": event.agent_id,
        "stimulus_id": event.stimulus_id,
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "payload": dict(event.payload or {}),
        "created_at": event.created_at.isoformat(),
    }


def _stable_int(*parts: Any) -> int:
    # Keep provider seeds in signed 31-bit range for broad compatibility.
    import hashlib

    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFF_FFFF


class SwarmEngine:
    """Coordinate durable stimuli, fair scheduling, model calls, and commits."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        gateway: GatewayDispatcher | None = None,
        scheduler: WeightedFairScheduler | None = None,
        policy: ActionPolicy | None = None,
        publish: PublishCallback | None = None,
        config: EngineConfig | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.gateway: GatewayDispatcher = gateway or ModelGateway()
        self.scheduler = scheduler or WeightedFairScheduler()
        self.policy = policy or ActionPolicy()
        self.publish = publish
        self.config = config or EngineConfig()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._wake_events: dict[str, asyncio.Event] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._closing = False

    # -------------------------------------------------------------- lifecycle
    async def start(self, run_id: str) -> str:
        events: list[dict[str, Any]]
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            sessions.require_runnable(repo.get_run(run_id))
            run = repo.control_run(run_id, "start")
            continuous = run.continuous
            events = self._events_after(session, before)
            state = run.state
        await self._publish_many(events)
        self.notify(run_id)
        if continuous:
            self._ensure_runner(run_id)
        return state

    async def pause(self, run_id: str) -> str:
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            run = Repository(session).control_run(run_id, "pause")
            events = self._events_after(session, before)
            state = run.state
        await self._publish_many(events)
        self.notify(run_id)
        return state

    async def resume(self, run_id: str) -> str:
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            run = repo.get_run(run_id)
            sessions.require_runnable(run)
            # A durable RUNNING row can outlive its process-local worker. Make
            # resume idempotent so it can repair that state instead of returning
            # a conflict that only a full process restart can clear.
            if run.state == RunState.RUNNING.value:
                repo.heartbeat_run(run.id)
                repo.add_event(
                    "run.resume",
                    run_id=run.id,
                    actor_type="human",
                    payload={"state": run.state, "worker_ensure": True},
                )
            else:
                run = repo.control_run(run_id, "resume")
            continuous = run.continuous
            events = self._events_after(session, before)
            state = run.state
        await self._publish_many(events)
        self.notify(run_id)
        if continuous:
            self._ensure_runner(run_id)
        return state

    async def stop(self, run_id: str, reason: str | None = None) -> str:
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            run = repo.control_run(run_id, "stop", reason=reason)
            events = self._events_after(session, before)
            state = run.state
        task = self._tasks.get(run_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._publish_many(events)
        self.notify(run_id)
        return state

    async def emergency_stop(self, run_id: str, reason: str | None = None) -> str:
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            run = repo.control_run(run_id, "emergency_stop", reason=reason)
            events = self._events_after(session, before)
            state = run.state
        task = self._tasks.get(run_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._publish_many(events)
        self.notify(run_id)
        return state

    async def step(self, run_id: str) -> StepResult:
        """Execute at most one durable stimulus, even while manually paused."""

        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            return await self._step_locked(run_id, explicit=True)

    def notify(self, run_id: str) -> None:
        """Wake a continuous worker after a request commits a new stimulus."""

        self._wake_events.setdefault(run_id, asyncio.Event()).set()
        if self._closing:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            should_run = bool(
                run is not None
                and run.state == RunState.RUNNING.value
                and run.continuous
            )
        if should_run:
            self._ensure_runner(run_id)

    async def recover(self) -> dict[str, int]:
        """Recover interrupted leases and restart persisted continuous runs."""

        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            sessions.retire_scripted_runs(Repository(session))
            events = self._events_after(session, before)
        await self._publish_many(events)
        recovery = await self._recover_inflight_work(
            run_id=None,
            stale_before=utc_now(),
            reason="process startup recovery",
        )
        with self.session_factory() as session:
            running = list(
                session.scalars(
                    select(Run.id).where(
                        Run.state == RunState.RUNNING.value,
                        Run.continuous.is_(True),
                    )
                )
            )
        for run_id in running:
            self._ensure_runner(run_id)
        return recovery

    async def shutdown(self) -> None:
        """Stop process-local workers without changing durable run state."""

        self._closing = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------ replay/rerun
    def replay(self, run_id: str, *, after_event_id: int = 0, limit: int = 10_000) -> list[dict[str, Any]]:
        """Return the recorded event stream verbatim; no models are called."""

        with self.session_factory() as session:
            events = Repository(session).list_events(after_id=after_event_id, run_id=run_id, limit=limit)
            return [_event_dict(event) for event in events]

    def _scheduler_for(self, run):
        if run and run.config.get("session_type") == "research":
            base = self.scheduler.config
            if autonomy.enabled(run):
                base = replace(base, role_injection_probability=0, role_injection_weight=0,
                               recent_penalty=0, domination_penalty=0)
            return WeightedFairScheduler(run_policy.effective_scheduler_config(run, base))
        if not autonomy.enabled(run):
            return self.scheduler
        return WeightedFairScheduler(replace(self.scheduler.config,
            ping_pong_limit=0, allow_consecutive_posts=True,
            role_injection_probability=0, role_injection_weight=0,
            recent_penalty=0, domination_penalty=0))

    def _policy_for(self, run):
        if run and run.config.get("session_type") == "research":
            return ActionPolicy(run_policy.effective_action_config(run, self.policy.config))
        if not autonomy.enabled(run):
            return self.policy
        return ActionPolicy(replace(self.policy.config,
            max_agent_ping_pong_posts=0, allow_consecutive_posts=True,
            allow_self_replies=True, allow_repeated_posts=True, max_body_chars=50_000))

    def _turn_agent(self, session, run, agent, stimulus=None):
        view = sessions.session_agent(session, run, agent)
        if stimulus is None or not stimulus.payload.get("forced"):
            return view
        view = SimpleNamespace(**{name: getattr(view, name) for name in (
            "id", "handle", "role", "persona", "provider", "model", "enabled", "settings",
            "permissions", "cooldown_seconds", "last_spoke_at")},
            persona_version=getattr(view, "persona_version", view.settings.get("persona_version", 1)))
        policy = run_policy.policy_for(run)
        view.cooldown_seconds = 0
        view.last_spoke_at = None
        if policy["cooldowns"] and not stimulus.payload.get("override_cooldown"):
            view.cooldown_seconds = (policy["cooldown_seconds"] if policy["cooldown_seconds"] is not None
                                     else agent.cooldown_seconds)
            view.last_spoke_at = session.scalar(select(func.max(Post.created_at)).join(
                Turn, Turn.resulting_post_id == Post.id).where(Turn.run_id == run.id, Turn.agent_id == agent.id))
        return view

    def _turn_policy(self, run, stimulus=None):
        policy = self._policy_for(run)
        if stimulus is not None and stimulus.payload.get("forced"):
            policy = ActionPolicy(replace(policy.config, enforce_cooldown=(
                run_policy.policy_for(run)["cooldowns"] and not stimulus.payload.get("override_cooldown"))))
        return policy

    def _forced_decision(self, run, thread, stimulus, agents, quota_remaining):
        if thread.status == ThreadStatus.CLOSED.value:
            return SchedulingDecision([], [], "thread is closed", _stable_int(run.seed, stimulus.id),
                wake_allowed=False, forced=True, forced_by=stimulus.payload.get("forced_by"),
                override_cooldown=bool(stimulus.payload.get("override_cooldown")))
        candidates = []
        for agent in agents:
            eligible = bool(agent.enabled and agent.permissions.get("speak", True)
                            and quota_remaining.get(agent.id, 1) > 0)
            components, reasons = {"forced": 1.0}, ["researcher selected this speaker"]
            if not eligible:
                reasons.append("agent quota exhausted" if quota_remaining.get(agent.id, 1) <= 0 else "posting permission denied")
            if agent.last_spoke_at and agent.cooldown_seconds:
                remaining = agent.cooldown_seconds - (self._virtual_now(run) - agent.last_spoke_at).total_seconds()
                if remaining > 0:
                    eligible = False
                    components["cooldown"] = -100.0
                    reasons.append("cooldown remains")
            candidates.append(CandidateScore(agent.id, agent.handle, agent.role, 1.0, eligible, components, reasons))
        decision = SchedulingDecision([c.agent_id for c in candidates if c.eligible][:1], candidates,
            "forced by " + str(stimulus.payload.get("forced_by")), _stable_int(run.seed, stimulus.id),
            forced=True, forced_by=stimulus.payload.get("forced_by"),
            override_cooldown=bool(stimulus.payload.get("override_cooldown")))
        return decision

    async def rerun(
        self,
        run_id: str,
        *,
        seed: int | None = None,
        continuous: bool | None = None,
    ) -> str:
        """Clone run configuration and human inputs for fresh model generation."""

        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            original = repo.get_run(run_id)
            sessions.require_runnable(original)
            if original.stop_reason and "safety_block" in original.stop_reason:
                raise InvalidStateError("provider safety block: this workflow cannot be retried")
            if autonomy.enabled(original):
                clone = sessions.restart(repo, original, seed=seed, continuous=continuous)
                clone_id = clone.id
                start_continuous = clone.continuous
            else:
                clone_id = None
                start_continuous = False
            events = self._events_after(session, before)
        if clone_id is not None:
            await self._publish_many(events)
            if start_continuous:
                await self.start(clone_id)
            return clone_id
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            original = repo.get_run(run_id)
            ordered_post_rows = list(
                session.execute(
                    select(Post)
                    .add_columns(Event.run_id)
                    .join(Thread, Post.thread_id == Thread.id)
                    .join(
                        Event,
                        (Event.post_id == Post.id) & (Event.event_type == "post.created"),
                    )
                    .where(Thread.run_id == original.id)
                    .order_by(Event.id)
                )
            )
            saw_agent_post = False
            for source_post, _source_event_run_id in ordered_post_rows:
                if source_post.author_type == AuthorType.AGENT.value:
                    saw_agent_post = True
                elif source_post.author_type == AuthorType.HUMAN.value and saw_agent_post:
                    raise InvalidStateError(
                        "cannot rerun a source with human posts interleaved after agent posts; "
                        "replay preserves that interaction, but moving it up-front would not"
                    )
            clone_config = {
                key: value
                for key, value in original.config.items()
                if key != "step_once"
            }
            clone = repo.create_run(
                seed=original.seed if seed is None else seed,
                continuous=original.continuous if continuous is None else continuous,
                config={**clone_config, "rerun_of": original.id},
                max_rounds=original.max_rounds,
                max_posts=original.max_posts,
                max_tokens=original.max_tokens,
                max_duration_seconds=original.max_duration_seconds,
                per_agent_quota=original.per_agent_quota,
                per_thread_quota=original.per_thread_quota,
                max_cascade_depth=original.max_cascade_depth,
            )
            # Clone human inputs in their global event order. Grouping by thread
            # would reorder A1, B1, A2 into A1, A2, B1 and change which initial
            # stimulus wins shared cooldown and quota decisions.
            thread_map: dict[str, Thread] = {}
            post_map: dict[str, str] = {}
            latest_by_thread: dict[str, tuple[int, Any]] = {}
            counted_source_inputs = 0
            for position, (old_post, source_event_run_id) in enumerate(ordered_post_rows):
                if old_post.author_type != AuthorType.HUMAN.value:
                    continue
                old_thread = repo.get_thread(old_post.thread_id)
                new_thread = thread_map.get(old_thread.id)
                if new_thread is None:
                    new_thread = repo.create_thread(
                        title=old_thread.title,
                        run_id=clone.id,
                        # Summaries may contain generated output and are therefore
                        # never treated as input to a fresh generation.
                        summary=None,
                        actor_type="system",
                        actor_id="rerun",
                    )
                    thread_map[old_thread.id] = new_thread
                parent = post_map.get(old_post.parent_post_id or "")
                write = repo.create_human_post(
                    new_thread.id,
                    old_post.body,
                    parent_post_id=parent,
                    author_handle=old_post.author_handle,
                    idempotency_key=f"rerun:{clone.id}:post:{old_post.id}",
                    metadata={"rerun_source_post_id": old_post.id},
                )
                post_map[old_post.id] = write.post.id
                latest_by_thread[old_thread.id] = (position, write)
                if source_event_run_id == original.id:
                    counted_source_inputs += 1

            for old_thread_id, (_position, latest_write) in sorted(
                latest_by_thread.items(),
                key=lambda item: item[1][0],
            ):
                new_thread = thread_map[old_thread_id]
                repo.add_stimulus(
                    thread_id=new_thread.id,
                    run_id=clone.id,
                    kind=StimulusKind.HUMAN_POST,
                    triggering_event_id=latest_write.event.id,
                    source_post_id=latest_write.post.id,
                    priority=10,
                    payload={"rerun_source_run_id": original.id},
                    dedupe_key=f"rerun:{clone.id}:thread:{old_thread_id}:initial",
                )
            # Creating cloned inputs increments this counter. Restore the exact
            # source-input baseline so rerun retains the source's post budget.
            clone.posts_used = counted_source_inputs
            events = self._events_after(session, before)
            clone_id = clone.id
            start_continuous = clone.continuous
        await self._publish_many(events)
        if start_continuous:
            await self.start(clone_id)
        return clone_id

    # --------------------------------------------------------------- workers
    def _ensure_runner(self, run_id: str) -> None:
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._run_continuous(run_id), name=f"swarmboard-run-{run_id}")
        self._tasks[run_id] = task

        def discard(done: asyncio.Task[None]) -> None:
            if self._tasks.get(run_id) is done:
                self._tasks.pop(run_id, None)
            if done.cancelled() or self._closing:
                return
            error = done.exception()
            if error is not None:
                logger.error(
                    "continuous run %s crashed",
                    run_id,
                    exc_info=(type(error), error, error.__traceback__),
                )
                recovery_task = asyncio.create_task(
                    self._handle_runner_failure(run_id, error),
                    name=f"swarmboard-recover-run-{run_id}",
                )
                recovery_task.add_done_callback(self._log_background_failure)

        task.add_done_callback(discard)

    async def _run_continuous(self, run_id: str) -> None:
        wake = self._wake_events.setdefault(run_id, asyncio.Event())
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        while not self._closing:
            try:
                with self.session_factory() as session:
                    run = session.get(Run, run_id)
                    if run is None or run.state != RunState.RUNNING.value or not run.continuous:
                        return
                async with lock:
                    result = await self._step_locked(run_id, explicit=False)
                if result.status in {"terminal", "budget_exhausted"}:
                    return
                if result.status == "no_work":
                    await self._idle_maintenance(run_id)
                    wake.clear()
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=self.config.poll_seconds)
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("continuous run %s failed", run_id)
                await self._handle_runner_failure(run_id, exc)
                return

    async def _recover_inflight_work(
        self,
        *,
        run_id: str | None,
        stale_before: datetime,
        reason: str,
    ) -> dict[str, int]:
        """Recover expired claims and safely reopen their idempotent turns."""

        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            result = repo.recover_inflight_work(
                run_id=run_id,
                stale_before=stale_before,
            )
            if run_id is not None and (
                result.stimuli_requeued
                or result.stimuli_failed
                or result.turns_failed
            ):
                repo.add_event(
                    "run.inflight_recovered",
                    run_id=run_id,
                    actor_type="system",
                    payload={
                        "reason": reason,
                        "stale_before": stale_before.isoformat(),
                        "stimuli_requeued": result.stimuli_requeued,
                        "stimuli_failed": result.stimuli_failed,
                        "turns_failed": result.turns_failed,
                    },
                )
            interrupted_stmt = (
                select(Turn)
                .join(Stimulus, Turn.stimulus_id == Stimulus.id)
                .where(
                    Turn.state == TurnState.FAILED.value,
                    Turn.error == "model call interrupted by process restart",
                    Turn.resulting_post_id.is_(None),
                    Stimulus.state == StimulusState.PENDING.value,
                )
            )
            if run_id is not None:
                interrupted_stmt = interrupted_stmt.where(Turn.run_id == run_id)
            interrupted = list(session.scalars(interrupted_stmt))
            for turn in interrupted:
                turn.state = TurnState.SELECTED.value
                turn.outcome = None
                turn.rejection_reason = None
                turn.error = None
                turn.completed_at = None
                turn.claim_token = None
                repo.add_event(
                    "turn.reopened",
                    run_id=turn.run_id,
                    thread_id=turn.thread_id,
                    agent_id=turn.agent_id,
                    stimulus_id=turn.stimulus_id,
                    payload={"turn_id": turn.id, "reason": reason},
                )
            events = self._events_after(session, before)
        await self._publish_many(events)
        return {
            "stimuli_requeued": result.stimuli_requeued,
            "stimuli_failed": result.stimuli_failed,
            "turns_reopened": len(interrupted),
        }

    async def _handle_runner_failure(self, run_id: str, error: BaseException) -> None:
        """Make a crashed worker's durable state recoverable, then pause it."""

        message = self._safe_error(error)
        recovery: dict[str, int] = {
            "stimuli_requeued": 0,
            "stimuli_failed": 0,
            "turns_reopened": 0,
        }
        recovery_error: str | None = None
        try:
            # This worker has exited, so even a fresh claim owned by it is now
            # abandoned and can be reclaimed immediately.
            recovery = await self._recover_inflight_work(
                run_id=run_id,
                stale_before=utc_now(),
                reason="continuous worker failure",
            )
        except Exception as exc:
            recovery_error = self._safe_error(exc)
            logger.exception("failed to recover work for crashed run %s", run_id)

        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            run = session.get(Run, run_id)
            if run is None:
                return
            repo.add_event(
                "run.worker_failed",
                run_id=run.id,
                actor_type="system",
                payload={
                    "error": message,
                    "recovery": recovery,
                    "recovery_error": recovery_error,
                },
            )
            if run.state == RunState.RUNNING.value:
                repo.set_run_state(
                    run.id,
                    RunState.PAUSED,
                    reason=f"continuous worker failed: {message}",
                )
            events = self._events_after(session, before)
        await self._publish_many(events)

    @staticmethod
    def _log_background_failure(done: asyncio.Task[Any]) -> None:
        if done.cancelled():
            return
        if (error := done.exception()) is not None:
            logger.error(
                "background recovery failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _step_locked(self, run_id: str, *, explicit: bool) -> StepResult:
        """Claim, prepare, execute, and acknowledge one stimulus."""

        with self.session_factory() as session:
            persisted = Repository(session).get_run(run_id)
            sessions.require_runnable(persisted)
            if persisted.state in {
                RunState.STOPPED.value,
                RunState.COMPLETED.value,
                RunState.EMERGENCY_STOPPED.value,
                RunState.FAILED.value,
            }:
                return StepResult("terminal", run_id, detail=f"run is {persisted.state}")
            if persisted.state == RunState.PAUSED.value and not explicit:
                return StepResult("paused", run_id, detail="run is paused")
            if persisted.state == RunState.CREATED.value and not explicit:
                return StepResult("paused", run_id, detail="run has not been started")
        await self._recover_inflight_work(
            run_id=run_id,
            stale_before=utc_now()
            - timedelta(seconds=max(1, self.config.claim_lease_seconds)),
            reason="claim lease expired",
        )
        step_once = False
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            run = repo.get_run(run_id)
            if run.state in {
                RunState.STOPPED.value,
                RunState.COMPLETED.value,
                RunState.EMERGENCY_STOPPED.value,
                RunState.FAILED.value,
            }:
                return StepResult("terminal", run_id, detail=f"run is {run.state}")
            if run.state == RunState.PAUSED.value and not explicit:
                return StepResult("paused", run_id, detail="run is paused")
            if run.state == RunState.CREATED.value and not explicit:
                return StepResult("paused", run_id, detail="run has not been started")
            if explicit and run.state in {RunState.CREATED.value, RunState.PAUSED.value}:
                step_once = True
                prior = run.state
                run.state = RunState.RUNNING.value
                run.started_at = run.started_at or utc_now()
                run.paused_at = None
                run.config = {**run.config, "step_once": True}
                repo.add_event(
                    "run.step",
                    run_id=run.id,
                    actor_type="human",
                    payload={"from": prior, "state": run.state},
                )

            budget_reason = repo.run_budget_exhaustion(run, now=self._virtual_now(run))
            if not budget_reason:
                sessions.prepare_session(repo, run)
                if run.state in {RunState.COMPLETED.value, RunState.FAILED.value}:
                    budget_reason = run.stop_reason
            if budget_reason:
                if run.state != RunState.FAILED.value:
                    repo.set_run_state(run.id, RunState.COMPLETED, reason=budget_reason)
                events = self._events_after(session, before)
                claimed: list[Stimulus] = []
            else:
                wall_claim_time = utc_now()
                readiness_time = wall_claim_time
                if self._uses_virtual_time(run):
                    readiness_time = max(readiness_time, self._virtual_now(run))
                claimed = repo.claim_stimuli(
                    run_id=run_id,
                    limit=1,
                    now=readiness_time,
                )
                # Queue readiness may use a virtual timestamp, but lease expiry
                # is always wall-clock based so a virtual jump cannot create an
                # unrecoverable claim dated in the future.
                for claimed_stimulus in claimed:
                    claimed_stimulus.claimed_at = wall_claim_time
                    claimed_stimulus.updated_at = wall_claim_time
                if not claimed and step_once:
                    run.config = {**run.config, "step_once": False}
                    repo.set_run_state(run.id, RunState.PAUSED, reason="manual step found no work")
                events = self._events_after(session, before)
            stimulus_id = claimed[0].id if claimed else None
            claim_token = claimed[0].claim_token if claimed else None
        await self._publish_many(events)

        if budget_reason:
            return StepResult("budget_exhausted", run_id, detail=budget_reason)
        if stimulus_id is None or claim_token is None:
            return StepResult("no_work", run_id)

        try:
            turn_ids, prep_detail = await self._prepare_stimulus(
                run_id,
                stimulus_id,
                claim_token,
            )
        except Exception as exc:
            await self._release_claim(stimulus_id, claim_token, f"preparation failed: {self._safe_error(exc)}")
            logger.exception("failed to prepare stimulus %s", stimulus_id)
            await self._finish_step_once(run_id, step_once)
            return StepResult("failed", run_id, stimulus_id=stimulus_id, detail=self._safe_error(exc))

        post_ids: list[str] = []
        for turn_id in turn_ids:
            if not explicit and not self._run_can_call(run_id):
                await self._yield_claim(stimulus_id, claim_token, "run paused before model call")
                return StepResult(
                    "paused",
                    run_id,
                    stimulus_id=stimulus_id,
                    turn_ids=turn_ids,
                    post_ids=post_ids,
                    detail="run paused before model call",
                )
            try:
                post_id = await self._execute_turn(turn_id, allow_manual_pause=explicit)
                if post_id:
                    post_ids.append(post_id)
            except _TurnDeferredForPause as exc:
                await self._yield_claim(stimulus_id, claim_token, str(exc))
                await self._finish_step_once(run_id, step_once)
                return StepResult(
                    "paused",
                    run_id,
                    stimulus_id=stimulus_id,
                    turn_ids=turn_ids,
                    post_ids=post_ids,
                    detail=str(exc),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("turn %s crashed", turn_id)
                error = self._safe_error(exc)
                await self._requeue_turn_after_exception(
                    turn_id,
                    stimulus_id=stimulus_id,
                    claim_token=claim_token,
                    error=error,
                )
                await self._finish_step_once(run_id, step_once)
                return StepResult(
                    "failed",
                    run_id,
                    stimulus_id=stimulus_id,
                    turn_ids=turn_ids,
                    post_ids=post_ids,
                    detail=error,
                )

        await self._complete_claim(stimulus_id, claim_token, made_posts=bool(post_ids))
        await self._advance_virtual_time(run_id, stimulus_id)
        await self._finish_step_once(run_id, step_once)
        await self._finish_run_if_budget_exhausted(run_id)
        return StepResult(
            "processed",
            run_id,
            stimulus_id=stimulus_id,
            turn_ids=turn_ids,
            post_ids=post_ids,
            detail=prep_detail,
        )

    async def _prepare_stimulus(
        self,
        run_id: str,
        stimulus_id: str,
        claim_token: str,
    ) -> tuple[list[str], str]:
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            run = repo.get_run(run_id)
            stimulus = repo.get_stimulus(stimulus_id)
            thread = repo.get_thread(stimulus.thread_id)

            if self._uses_virtual_time(run) and stimulus.source_post_id:
                source_post = session.get(Post, stimulus.source_post_id)
                if source_post is not None and source_post.author_type == AuthorType.HUMAN.value:
                    # Inputs arrive on wall time; map their activity to the
                    # deterministic clock when the corresponding stimulus runs.
                    thread.latest_activity_at = self._virtual_now(run)

            if stimulus.claim_token != claim_token:
                raise ClaimConflictError("stimulus claim was lost before scheduling")
            if stimulus.cascade_depth > run.max_cascade_depth:
                repo.complete_stimulus(stimulus.id, claim_token=claim_token)
                if run_policy.policy_for(run)["dormancy"]:
                    repo.set_thread_status(
                        thread.id,
                        ThreadStatus.DORMANT,
                        reason="maximum reactive cascade depth reached",
                    )
                events = self._events_after(session, before)
                turn_ids: list[str] = []
                detail = "maximum reactive cascade depth reached"
            else:
                existing = list(
                    session.scalars(
                        select(Turn)
                        .where(Turn.stimulus_id == stimulus.id)
                        .order_by(Turn.started_at, Turn.id)
                    )
                )
                resumable = [
                    turn
                    for turn in existing
                    if turn.state in {TurnState.SELECTED.value, TurnState.CALLING.value}
                ]
                if existing:
                    repo.mark_stimulus_processing(stimulus.id, claim_token)
                    for turn in resumable:
                        turn.claim_token = claim_token
                    turn_ids = [turn.id for turn in resumable]
                    detail = "resumed previously selected turns" if turn_ids else "all turns already terminal"
                    # Finalize terminal turns below, including any durable
                    # fallback after a pass that was interrupted by a restart.
                    events = self._events_after(session, before)
                else:
                    posts = list(
                        session.scalars(
                            select(Post)
                            .where(Post.thread_id == thread.id)
                            .order_by(Post.sequence.desc())
                            .limit(max(100, self.config.context_post_limit,
                                (run_policy.policy_for(run)["consecutive_turn_cap"] or 0) + 1))
                        )
                    )
                    posts.reverse()
                    posts = participant_posts(posts)
                    agents = [self._turn_agent(session, run, a, stimulus) for a in
                              repo.list_agents(enabled_only=True, agent_ids=run.config.get("agent_ids"))]
                    attempted = set(stimulus.payload.get("attempted_agent_ids", []))
                    unavailable = set(run.config.get("unavailable_agent_ids", []))
                    agents = [agent for agent in agents if agent.id not in attempted and agent.id not in unavailable]
                    if stimulus.target_agent_id:
                        agents = [agent for agent in agents if agent.id == stimulus.target_agent_id]
                    quota_remaining = self._agent_quota_remaining(session, run, agents)
                    thread_posts = session.scalar(
                        select(func.count(Post.id)).where(
                            Post.thread_id == thread.id,
                            Post.author_type == AuthorType.AGENT.value,
                            Post.metadata_json["is_inherited"].as_boolean().is_not(True),
                        )
                    ) or 0
                    round_slots = max(0, run.max_rounds - run.rounds_used)
                    thread_slots = max(0, min(run.per_thread_quota,
                        run.config.get("inherited_thread_quota_remaining", run.per_thread_quota)) - thread_posts)
                    configured_selection = self._run_config_int(
                        run,
                        "max_agents_per_stimulus",
                        self.config.max_agents_per_stimulus,
                        minimum=1,
                        maximum=2,
                    )
                    selection_limit = min(configured_selection, round_slots, thread_slots)
                    decision = self._forced_decision(run, thread, stimulus, agents, quota_remaining) if stimulus.payload.get("forced") else self._scheduler_for(run).select(
                        agents,
                        thread=thread,
                        posts=posts,
                        stimulus=stimulus,
                        run_seed=run.seed,
                        max_selected=max(1, selection_limit),
                        quota_remaining=quota_remaining,
                        now=self._virtual_now(run),
                    )
                    if thread.status == ThreadStatus.DORMANT.value and decision.wake_allowed:
                        repo.wake_thread(thread.id, reason=stimulus.kind)

                    if selection_limit == 0:
                        decision.selected_agent_ids.clear()
                        if round_slots == 0:
                            decision.reason = "run round budget exhausted"
                        else:
                            decision.reason = "per-thread quota exhausted"
                            if run_policy.policy_for(run)["dormancy"]:
                                repo.set_thread_status(
                                    thread.id,
                                    ThreadStatus.DORMANT,
                                    reason=decision.reason,
                                )
                    elif len(decision.selected_agent_ids) > selection_limit:
                        decision.selected_agent_ids = decision.selected_agent_ids[:selection_limit]
                        decision.reason = (
                            f"{decision.reason}; capped to {selection_limit} available "
                            "round/thread slot(s)"
                        )

                    if not decision.selected_agent_ids:
                        scheduler_now = self._virtual_now(run)
                        defer_until = self._cooldown_deferral_time(
                            stimulus,
                            decision,
                            agents,
                            now=scheduler_now,
                        )
                        if defer_until is not None:
                            stimulus.state = StimulusState.PENDING.value
                            stimulus.claim_token = None
                            stimulus.claimed_at = None
                            stimulus.attempts = max(0, stimulus.attempts - 1)
                            stimulus.not_before = defer_until
                            stimulus.last_error = None
                            payload = {
                                **decision.as_dict(),
                                "reason": "temporarily ineligible due to cooldown",
                                "not_before": defer_until.isoformat(),
                            }
                            repo.add_event(
                                "stimulus.deferred",
                                run_id=run.id,
                                thread_id=thread.id,
                                post_id=stimulus.source_post_id,
                                agent_id=stimulus.target_agent_id,
                                stimulus_id=stimulus.id,
                                payload=payload,
                            )
                            if self._uses_virtual_time(run):
                                jump_seconds = max(
                                    0.0,
                                    (defer_until - scheduler_now).total_seconds(),
                                )
                                if jump_seconds:
                                    repo.increment_run_counters(
                                        run.id,
                                        virtual_time=jump_seconds,
                                    )
                                    repo.add_event(
                                        "run.virtual_time_cooldown_jump",
                                        run_id=run.id,
                                        thread_id=thread.id,
                                        stimulus_id=stimulus.id,
                                        payload={
                                            "seconds": jump_seconds,
                                            "not_before": defer_until.isoformat(),
                                        },
                                    )
                            detail = "deferred until cooldown expires"
                        else:
                            repo.mark_stimulus_processing(stimulus.id, claim_token)
                            repo.add_event(
                                "scheduler.no_selection",
                                run_id=run.id,
                                thread_id=thread.id,
                                stimulus_id=stimulus.id,
                                payload=decision.as_dict(),
                            )
                            detail = decision.reason
                        turn_ids = []
                    else:
                        repo.mark_stimulus_processing(stimulus.id, claim_token)
                        turn_ids = []
                        for agent_id in decision.selected_agent_ids:
                            agent = next(agent for agent in agents if agent.id == agent_id)
                            context, messages, memory_ids = self._build_context(
                                session,
                                run=run,
                                thread=thread,
                                posts=posts,
                                stimulus=stimulus,
                                agent=agent,
                            )
                            captured_prompt = None
                            if stimulus.payload.get("reuse_turn_id"):
                                original = repo.get_turn(stimulus.payload["reuse_turn_id"])
                                captured_prompt = original.prompt
                                messages = self._decode_prompt(captured_prompt)
                                current_configuration = context["agent_snapshot"]
                                context = dict(original.context_snapshot)
                                context["agent_snapshot"] = current_configuration
                                memory_ids = list(original.retrieved_memory_ids)
                            sampling = self._sampling_settings(agent, run)
                            turn_seed = _stable_int(run.seed, stimulus.id, agent.id)
                            turn = repo.create_turn(
                                run_id=run.id,
                                thread_id=thread.id,
                                agent_id=agent.id,
                                stimulus_id=stimulus.id,
                                triggering_event_id=stimulus.triggering_event_id,
                                context_post_ids=[str(post["id"]) for post in context["posts"]],
                                context_snapshot=context,
                                scheduler_scores=decision.as_dict(),
                                selection_reason=decision.reason,
                                prompt=captured_prompt if captured_prompt is not None else json.dumps(
                                    [message.model_dump() for message in messages],
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                                prompt_version=context.get("prompt_version") or context.get("persona_snapshot", {}).get("prompt_version", self.config.prompt_version),
                                provider=agent.provider,
                                model=agent.model,
                                sampling_settings=sampling,
                                seed=turn_seed,
                                retrieved_memory_ids=memory_ids,
                                idempotency_key=f"turn:{stimulus.id}:{agent.id}",
                                claim_token=claim_token,
                            )
                            turn_ids.append(turn.id)
                        detail = decision.reason
                    events = self._events_after(session, before)
        await self._publish_many(events)
        return turn_ids, detail

    def _build_context(
        self,
        session: Session,
        *,
        run: Run,
        thread: Thread,
        posts: Sequence[Post],
        stimulus: Stimulus,
        agent: Agent,
        at_event_id: int | None = None,
    ) -> tuple[dict[str, Any], list[ChatMessage], list[str]]:
        selected_posts = cadence.transcript(Repository(session), run, at_event_id=at_event_id) if cadence.enabled(run) else list(posts)[-self._context_limit(run) :]
        selected_posts = participant_posts(selected_posts)
        from .interventions import inactive_memory_ids
        inactive = inactive_memory_ids(Repository(session), run.id, at_event_id=at_event_id)
        memories = list(
            session.scalars(
                select(Memory)
                .where(
                    Memory.active.is_(True),
                    (Memory.run_id == run.id) | Memory.run_id.is_(None),
                    (Memory.thread_id == thread.id) | Memory.thread_id.is_(None),
                    (Memory.agent_id == agent.id) | Memory.agent_id.is_(None),
                )
                .order_by(Memory.updated_at.desc())
            )
        )
        if autonomy.enabled(run):
            memories = [memory for memory in memories if memory.run_id == run.id]
        memories = [memory for memory in memories if memory.id not in inactive]
        if at_event_id is not None:
            cutoff_time = session.get(Event, at_event_id).created_at
            memories = [memory for memory in memories if memory.created_at <= cutoff_time]
        memories = memories[:self.config.memory_limit]
        post_payload = [
            {
                "id": post.id,
                "thread_id": post.thread_id,
                "parent_post_id": post.parent_post_id,
                "author_type": post.author_type,
                "author_agent_id": post.author_agent_id,
                "author_handle": post.author_handle,
                "body": post.body,
                "sequence": post.sequence,
                "intent": post.intent,
                "created_at": post.created_at.isoformat(),
            }
            for post in selected_posts
        ]
        memory_payload = [
            {
                "id": memory.id,
                "claim": memory.claim,
                "source_post_ids": list(memory.source_post_ids),
                "confidence": memory.confidence,
                "sha256": text_hash(memory.claim),
            }
            for memory in memories
        ]
        context = {
            "captured_at": utc_now().isoformat(),
            "run": {
                "id": run.id,
                "seed": run.seed,
                "rounds_remaining": max(0, run.max_rounds - run.rounds_used),
                "posts_remaining": max(0, run.max_posts - run.posts_used),
                "tokens_remaining": max(0, run.max_tokens - run.tokens_used),
            },
            "thread": {
                "id": thread.id,
                "title": thread.title,
                "status": thread.status,
                "summary": thread.summary,
                "current_sequence": thread.current_sequence,
            },
            "stimulus": {
                "id": stimulus.id,
                "kind": stimulus.kind,
                "source_post_id": stimulus.source_post_id,
                "cascade_depth": stimulus.cascade_depth,
                "payload": dict(stimulus.payload or {}),
            },
            "posts": post_payload,
            "memories": memory_payload,
        }
        if autonomy.enabled(run):
            context["environment"] = autonomy.context(Repository(session), run, agent, at_event_id=at_event_id)
            context["participants"] = context["environment"]["participants"]
            context["prompt_version"] = "autonomous-board-v1"
            if cadence.enabled(run):
                context["cadence"] = cadence.context(Repository(session), run)
                context["prompt_version"] = "ada-cadence-v1"
        snapshot = persona_snapshot(agent.settings or {})
        if snapshot is not None:
            system_prompt = harness_prompt(snapshot, handle=agent.handle)
            participants = [sessions.session_agent(session, run, peer) for peer in Repository(session).list_agents(
                enabled_only=True, agent_ids=run.config.get("agent_ids"),
            )]
            context["participants"] = [
                {"handle": peer.handle, **({"role": peer.role} if peer.id != agent.id else {})}
                for peer in participants
            ]
            context["persona_snapshot"] = {
                **persona_identity(agent), "schema_version": snapshot.version,
                "prompt_version": HARNESS_PROMPT_VERSION, "files": snapshot.file_manifest,
            }
        else:
            system_prompt = (
                f"{SYSTEM_PROMPT}\n\n"
                f"Your handle is @{agent.handle}. People on the board know you as the {agent.role}.\n"
                f"Persona: {agent.persona}"
            )
        if autonomy.enabled(run):
            system_prompt = autonomy.prompt(agent, snapshot, run=run)
        system = (
            f"{system_prompt}\n\n"
            "The exact JSON Schema is:\n"
            f"{json.dumps(action_json_schema(), separators=(',', ':'))}\n"
            "All schema keys must be present. For reply/propose_close, title must be null. "
            "For pass, parent_post_id, title, body, and intent must all be null. Never use "
            "an empty string where null is required."
        )
        intervention_state = intervention_snapshot(Repository(session), run, agent, selected_posts, at_event_id=at_event_id)
        if intervention_state["private_instructions"]:
            system += "\n\nPrivate instructions from the human operator for this participant:\n"
            system += "\n\n".join(item["body"] for item in intervention_state["private_instructions"])
        user = "Captured immutable discussion context:\n" + json.dumps(
            context,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        # These fields belong to the researcher ledger, never to participant
        # messages: they reveal interventions and actual configuration.
        context["agent_snapshot"] = agent_snapshot(agent)
        context.setdefault("persona_snapshot", persona_identity(agent))
        context["interventions"] = intervention_state
        return context, [ChatMessage(role="system", content=system), ChatMessage(role="user", content=user)], [
            memory.id for memory in memories
        ]

    async def _execute_turn(self, turn_id: str, *, allow_manual_pause: bool) -> str | None:
        """Call a provider with bounded retries, then atomically commit its action."""

        del allow_manual_pause  # A new pause always fences retries, including manual steps.
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            turn = repo.get_turn(turn_id)
            if turn.state in {TurnState.COMPLETED.value, TurnState.PASSED.value}:
                return turn.resulting_post_id
            if turn.state == TurnState.FAILED.value:
                return None
            agent = repo.get_agent(turn.agent_id)
            run = repo.get_run(turn.run_id) if turn.run_id else None
            agent = self._turn_agent(session, run, agent, repo.get_stimulus(turn.stimulus_id) if turn.stimulus_id else None)
            captured_configuration = (turn.context_snapshot or {}).get("agent_snapshot", {}).get("configuration")
            if captured_configuration:
                agent = SimpleNamespace(**{name: getattr(agent, name) for name in (
                    "id", "handle", "role", "enabled", "permissions", "cooldown_seconds", "last_spoke_at")},
                    **captured_configuration)
            repo.mark_turn_calling(turn.id)
            messages = self._decode_prompt(turn.prompt)
            seed = turn.seed
            call_claim_token = turn.claim_token
            sampling = dict(turn.sampling_settings or {})
            prior_retries = len(turn.retry_history)
            prior_input = turn.input_tokens
            prior_output = turn.output_tokens
            prior_total = turn.total_tokens
            prior_latency = turn.latency_ms or 0
            configured_retries = self._run_config_int(
                run,
                "model_retries",
                self.config.model_retries,
                minimum=0,
                maximum=10,
            ) if run is not None else self.config.model_retries
            events = self._events_after(session, before)
        await self._publish_many(events)

        max_attempts = configured_retries + 1
        attempt = min(prior_retries, max_attempts - 1)
        retry_history: list[dict[str, Any]] = []
        aggregate_input = prior_input
        aggregate_output = prior_output
        aggregate_total = prior_total
        aggregate_latency = prior_latency
        final_result: GatewayResult | None = None
        final_decision: Any | None = None
        final_error: str | None = None
        last_raw: str | None = None
        parsed_action: dict[str, Any] | None = None
        calls_made = 0
        deferred_for_pause = False
        failure_category = None
        outcome = "provider_failure"
        capture_mode = run_policy.policy_for(run)["schema_mode"] == "capture"

        while attempt < max_attempts:
            if not self._run_can_call(turn.run_id):
                final_error = "run is not permitted to start another model call"
                deferred_for_pause = self._run_is_paused(turn.run_id)
                break
            if not self._agent_can_speak(agent.id):
                final_error = "agent is no longer permitted to call or post"
                break
            call_started = time.perf_counter()
            try:
                calls_made += 1
                raw_result = await self.gateway.complete(
                    agent=agent,
                    messages=messages,
                    seed=_stable_int(seed, attempt),
                    sampling=sampling,
                )
                result = self._coerce_gateway_result(raw_result, agent)
                if run and result.action.parent_post_id in run.config.get("post_id_map", {}):
                    result = replace(result, action=result.action.model_copy(update={
                        "parent_post_id": run.config["post_id_map"][result.action.parent_post_id]}))
                final_result = result
                last_raw = result.raw_output
                parsed_action = result.action.model_dump(mode="json")
                if run and run.config.get("source_turn_id"):
                    parsed_action = json.loads(result.raw_output)
                aggregate_input += result.usage.prompt_tokens
                aggregate_output += result.usage.completion_tokens
                aggregate_total += result.usage.total_tokens
                aggregate_latency += result.latency_ms

                with self.session_factory() as response_session, response_session.begin():
                    response_repo = Repository(response_session)
                    response_turn = response_repo.get_turn(turn_id)
                    if response_turn.state == TurnState.CALLING.value and response_turn.claim_token == call_claim_token:
                        response_repo.add_event("provider.response", run_id=turn.run_id,
                            thread_id=turn.thread_id, agent_id=turn.agent_id,
                            payload={"turn_id": turn_id, "attempt": attempt + 1,
                                     "provider": result.provider, "model": result.model,
                                     "metadata": dict(result.response_metadata)})
                with self.session_factory() as policy_session:
                    current_turn = Repository(policy_session).get_turn(turn_id)
                    current_agent = Repository(policy_session).get_agent(current_turn.agent_id)
                    current_run = Repository(policy_session).get_run(current_turn.run_id) if current_turn.run_id else None
                    current_thread = autonomy.action_thread(Repository(policy_session), current_run, current_turn, result.action)
                    current_posts = (list(policy_session.scalars(select(Post).where(Post.thread_id == current_thread.id)
                                     .order_by(Post.sequence))) if run_policy.is_research(current_run)
                                     else Repository(policy_session).list_posts(current_thread.id, limit=500))
                    current_posts = participant_posts(current_posts)
                    fingerprints = {
                        str(post.metadata_json.get("action_fingerprint"))
                        for post in current_posts
                        if post.metadata_json.get("action_fingerprint")
                    }
                    current_stimulus = policy_session.get(Stimulus, current_turn.stimulus_id)
                    current_agent = self._turn_agent(policy_session, current_run, current_agent, current_stimulus)
                    decision = self._turn_policy(current_run, current_stimulus).validate(
                        result.action,
                        agent=current_agent,
                        thread=current_thread,
                        posts=current_posts,
                        existing_fingerprints=fingerprints,
                        now=self._virtual_now(current_run) if current_run else utc_now(),
                    )
                if decision.accepted:
                    final_decision = decision
                    break
                final_error = f"policy rejected action: {decision.reason}"
                outcome = "rejected_by_policy"
                retry_history.append(
                    {
                        "attempt": attempt + 1,
                        "kind": "policy_rejection",
                        "error": final_error,
                        "raw_output": last_raw,
                        "parsed_action": parsed_action,
                        "latency_ms": result.latency_ms,
                        "token_usage": asdict(result.usage),
                        "at": utc_now().isoformat(),
                    }
                )
                if self._is_permanent_policy_rejection(decision.reason):
                    attempt = max_attempts
                    break
                messages = [
                    *messages,
                    ChatMessage(role="assistant", content=last_raw),
                    ChatMessage(
                        role="user",
                        content=(
                            f"That action was rejected: {decision.reason}. "
                            "Return a materially different valid action, or pass. JSON only."
                        ),
                    ),
                ]
            except (GatewayError, StructuredOutputError) as exc:
                elapsed_ms = round((time.perf_counter() - call_started) * 1000)
                retryable = not isinstance(exc, GatewayError) or exc.retryable
                if isinstance(exc, StructuredOutputError):
                    outcome = "invalid_output"
                    if capture_mode:
                        retryable = False
                else:
                    outcome = "provider_failure"
                final_error = self._safe_error(exc)
                failure_category = getattr(exc, "category", "structured_output")
                rejected_raw = getattr(exc, "raw_output", None)
                if isinstance(rejected_raw, str):
                    last_raw = rejected_raw
                    # JSON artifacts may have unknown fields or invalid actions.
                    # Retain those fields without treating them as executable.
                    parsed_action = captured_json(rejected_raw)
                rejected_usage = getattr(exc, "usage", None)
                if isinstance(rejected_usage, TokenUsage):
                    aggregate_input += rejected_usage.prompt_tokens
                    aggregate_output += rejected_usage.completion_tokens
                    aggregate_total += rejected_usage.total_tokens
                provider_latency = getattr(exc, "latency_ms", None)
                aggregate_latency += int(provider_latency or elapsed_ms)
                retry_history.append(
                    {
                        "attempt": attempt + 1,
                        "kind": "gateway_error",
                        "category": failure_category,
                        "error": final_error,
                        "retryable": retryable,
                        "latency_ms": int(provider_latency or elapsed_ms),
                        "raw_output": rejected_raw,
                        "token_usage": asdict(rejected_usage) if isinstance(rejected_usage, TokenUsage) else None,
                        "at": utc_now().isoformat(),
                    }
                )
                if not retryable:
                    attempt = max_attempts
                    break
                messages = [*messages]
                if isinstance(rejected_raw, str):
                    messages.append(ChatMessage(role="assistant", content=rejected_raw))
                messages.append(
                    ChatMessage(
                        role="user",
                        content=(
                            f"The previous response could not be executed: {str(exc)}. "
                            "Correct that exact violation. Return one JSON object matching the "
                            "action schema, with no markdown or explanation."
                        ),
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                final_error = self._safe_error(exc)
                outcome = "provider_failure"
                retry_history.append(
                    {
                        "attempt": attempt + 1,
                        "kind": "unexpected_gateway_error",
                        "error": final_error,
                        "retryable": False,
                        "latency_ms": round((time.perf_counter() - call_started) * 1000),
                        "at": utc_now().isoformat(),
                    }
                )
                attempt = max_attempts
                break

            attempt += 1
            if attempt < max_attempts:
                await asyncio.sleep(self.config.retry_backoff_seconds * (2 ** (attempt - 1)))

        # Persist retry entries before the terminal event, preserving their order.
        if retry_history:
            with self.session_factory() as session, session.begin():
                before = self._last_event_id(session)
                repo = Repository(session)
                current = repo.get_turn(turn_id)
                if (
                    current.state != TurnState.CALLING.value
                    or current.claim_token != call_claim_token
                ):
                    return None
                for retry in retry_history:
                    repo.append_turn_retry(turn_id, retry)
                    turn_record = repo.get_turn(turn_id)
                    repo.add_event(
                        "turn.retry",
                        run_id=turn_record.run_id,
                        thread_id=turn_record.thread_id,
                        agent_id=turn_record.agent_id,
                        stimulus_id=turn_record.stimulus_id,
                        payload={"turn_id": turn_id, **retry},
                    )
                retry_events = self._events_after(session, before)
            await self._publish_many(retry_events)

        if deferred_for_pause:
            deferred = await self._defer_turn_for_pause(
                turn_id,
                expected_claim_token=call_claim_token,
                latency_ms=aggregate_latency,
                input_tokens=aggregate_input,
                output_tokens=aggregate_output,
                total_tokens=aggregate_total,
            )
            if deferred:
                raise _TurnDeferredForPause("run paused before the next model attempt")
            return None

        if final_result is None or final_decision is None:
            if calls_made == 0:
                await self._fail_turn_without_model_call(
                    turn_id,
                    expected_claim_token=call_claim_token,
                    error=final_error or "model call was not permitted",
                )
                return None
            with self.session_factory() as session, session.begin():
                before = self._last_event_id(session)
                repo = Repository(session)
                current = repo.get_turn(turn_id)
                if (
                    current.state != TurnState.CALLING.value
                    or current.claim_token != call_claim_token
                ):
                    return None
                failed = repo.finish_turn(
                    turn_id,
                    state=TurnState.FAILED,
                    raw_output=last_raw,
                    parsed_action=parsed_action,
                    error=final_error or "model attempts exhausted",
                    outcome=outcome,
                    rejection_reason=final_error,
                    latency_ms=aggregate_latency or None,
                    input_tokens=aggregate_input,
                    output_tokens=aggregate_output,
                    total_tokens=aggregate_total,
                )
                total_calls = prior_retries + calls_made
                if failed.run_id and total_calls > 1:
                    repo.increment_run_counters(failed.run_id, model_calls=total_calls - 1)
                if failed.run_id:
                    failed_run = repo.get_run(failed.run_id)
                    if failure_category == "safety_block":
                        repo.add_event("provider.failure",
                            run_id=failed.run_id, agent_id=failed.agent_id,
                            payload={"turn_id": failed.id, "category": failure_category or "policy_rejection"})
                        repo.set_run_state(failed.run_id, RunState.FAILED,
                            reason=f"{failure_category or 'policy_rejection'}: {final_error}")
                    elif autonomy.enabled(failed_run):
                        # One unavailable endpoint does not stop the other participants.
                        if failure_category and not (capture_mode and outcome == "invalid_output"):
                            unavailable = set(failed_run.config.get("unavailable_agent_ids", []))
                            unavailable.add(failed.agent_id)
                            failed_run.config = {**failed_run.config, "unavailable_agent_ids": sorted(unavailable)}
                        repo.add_event("session.participant_unavailable", run_id=failed.run_id,
                            agent_id=failed.agent_id, payload={"turn_id": failed.id,
                            "category": failure_category or "policy_rejection", "error": final_error})
                events = self._events_after(session, before)
            await self._publish_many(events)
            return None

        action = final_result.action
        if action.action == "pass":
            with self.session_factory() as session, session.begin():
                before = self._last_event_id(session)
                repo = Repository(session)
                current = repo.get_turn(turn_id)
                if (
                    current.state != TurnState.CALLING.value
                    or current.claim_token != call_claim_token
                ):
                    return None
                passed = repo.finish_turn(
                    turn_id,
                    state=TurnState.PASSED,
                    raw_output=final_result.raw_output,
                    parsed_action=action.model_dump(mode="json"),
                    validated_action=action.model_dump(mode="json"),
                    latency_ms=aggregate_latency,
                    input_tokens=aggregate_input,
                    output_tokens=aggregate_output,
                    total_tokens=aggregate_total,
                )
                if passed.run_id and autonomy.enabled(repo.get_run(passed.run_id)):
                    autonomy.record_action(repo, repo.get_run(passed.run_id), passed, action)
                total_calls = prior_retries + calls_made
                if passed.run_id and total_calls > 1:
                    repo.increment_run_counters(passed.run_id, model_calls=total_calls - 1)
                events = self._events_after(session, before)
            await self._publish_many(events)
            return None

        return await self._commit_action(
            turn_id,
            result=final_result,
            decision=final_decision,
            input_tokens=aggregate_input,
            output_tokens=aggregate_output,
            total_tokens=aggregate_total,
            latency_ms=aggregate_latency,
            calls_made=prior_retries + calls_made,
            expected_claim_token=call_claim_token,
        )

    async def _commit_action(
        self,
        turn_id: str,
        *,
        result: GatewayResult,
        decision: Any,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
        latency_ms: int,
        calls_made: int,
        expected_claim_token: str | None,
    ) -> str | None:
        action = result.action
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            turn = repo.get_turn(turn_id)
            if turn.state == TurnState.COMPLETED.value:
                return turn.resulting_post_id
            if turn.state in {TurnState.PASSED.value, TurnState.FAILED.value}:
                return None
            # A recovered turn is reset to SELECTED for a new, claimed delivery.
            # A result arriving from the superseded CALLING lease must not write.
            if (
                turn.state != TurnState.CALLING.value
                or turn.claim_token != expected_claim_token
            ):
                return None
            agent = repo.get_agent(turn.agent_id)
            run = repo.get_run(turn.run_id) if turn.run_id else None
            thread = autonomy.action_thread(repo, run, turn, action)
            posts = (list(session.scalars(select(Post).where(Post.thread_id == thread.id).order_by(Post.sequence)))
                     if run_policy.is_research(run) else repo.list_posts(thread.id, limit=500))
            posts = participant_posts(posts)
            fingerprints = {
                str(post.metadata_json.get("action_fingerprint"))
                for post in posts
                if post.metadata_json.get("action_fingerprint")
            }
            current_stimulus = session.get(Stimulus, turn.stimulus_id)
            current_agent = self._turn_agent(session, run, agent, current_stimulus)
            fresh_decision = self._turn_policy(run, current_stimulus).validate(
                action,
                agent=current_agent,
                thread=thread,
                posts=posts,
                existing_fingerprints=fingerprints,
                now=self._virtual_now(run) if run else utc_now(),
            )
            terminal_states = {
                RunState.STOPPED.value,
                RunState.COMPLETED.value,
                RunState.EMERGENCY_STOPPED.value,
                RunState.FAILED.value,
            }
            if run is not None and run.state in terminal_states:
                failed = repo.finish_turn(
                    turn.id,
                    state=TurnState.FAILED,
                    raw_output=result.raw_output,
                    parsed_action=(json.loads(result.raw_output) if run and run.config.get("source_turn_id")
                                   else action.model_dump(mode="json")),
                    error=f"run became {run.state} before action commit",
                    latency_ms=latency_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                )
                if calls_made > 1:
                    repo.increment_run_counters(run.id, model_calls=calls_made - 1)
                events = self._events_after(session, before)
                post_id = None
            elif not fresh_decision.accepted:
                failed = repo.finish_turn(
                    turn.id,
                    state=TurnState.FAILED,
                    raw_output=result.raw_output,
                    parsed_action=(json.loads(result.raw_output) if run and run.config.get("source_turn_id")
                                   else action.model_dump(mode="json")),
                    error=f"policy changed before commit: {fresh_decision.reason}",
                    latency_ms=latency_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                )
                if failed.run_id and calls_made > 1:
                    repo.increment_run_counters(failed.run_id, model_calls=calls_made - 1)
                events = self._events_after(session, before)
                post_id = None
            elif run is not None and run.posts_used >= run.max_posts:
                failed = repo.finish_turn(
                    turn.id,
                    state=TurnState.FAILED,
                    raw_output=result.raw_output,
                    parsed_action=(json.loads(result.raw_output) if run and run.config.get("source_turn_id")
                                   else action.model_dump(mode="json")),
                    error="post budget exhausted before commit",
                    latency_ms=latency_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                )
                if calls_made > 1:
                    repo.increment_run_counters(run.id, model_calls=calls_made - 1)
                events = self._events_after(session, before)
                post_id = None
            elif (
                run is not None
                and action.action != "new_thread"
                and sum(
                    post.author_type == AuthorType.AGENT.value
                    and not post.metadata_json.get("is_inherited")
                    and not post.metadata_json.get("is_impersonation")
                    for post in posts
                )
                >= min(run.per_thread_quota, run.config.get("inherited_thread_quota_remaining", run.per_thread_quota))
            ):
                failed = repo.finish_turn(
                    turn.id,
                    state=TurnState.FAILED,
                    raw_output=result.raw_output,
                    parsed_action=(json.loads(result.raw_output) if run and run.config.get("source_turn_id")
                                   else action.model_dump(mode="json")),
                    error="per-thread quota exhausted before commit",
                    latency_ms=latency_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                )
                if calls_made > 1:
                    repo.increment_run_counters(run.id, model_calls=calls_made - 1)
                events = self._events_after(session, before)
                post_id = None
            else:
                post_metadata = {
                    "turn_id": turn.id,
                    "action": action.action,
                    "action_fingerprint": fresh_decision.fingerprint,
                    "provider": result.provider,
                    "model": result.model,
                }
                if action.action == "new_thread":
                    write = repo.create_agent_thread(
                        title=action.title or "Untitled",
                        run_id=turn.run_id,
                        agent_id=agent.id,
                        body=action.body or "",
                        intent=action.intent,
                        idempotency_key=turn.idempotency_key,
                        stimulus_id=turn.stimulus_id,
                        metadata=post_metadata,
                    )
                    target_thread = repo.get_thread(write.post.thread_id)
                else:
                    target_thread = thread
                    write = repo.create_agent_post(
                        target_thread.id,
                        agent.id,
                        action.body or "",
                        parent_post_id=action.parent_post_id,
                        intent=action.intent,
                        idempotency_key=turn.idempotency_key,
                        stimulus_id=turn.stimulus_id,
                        metadata=post_metadata,
                    )
                post_id = write.post.id
                if write.created and run is not None and self._uses_virtual_time(run):
                    virtual_at = self._virtual_now(run)
                    agent.last_spoke_at = virtual_at
                    target_thread.latest_activity_at = virtual_at
                repo.finish_turn(
                    turn.id,
                    state=TurnState.COMPLETED,
                    resulting_post_id=post_id,
                    raw_output=result.raw_output,
                    parsed_action=(json.loads(result.raw_output) if run and run.config.get("source_turn_id")
                                   else action.model_dump(mode="json")),
                    validated_action=action.model_dump(mode="json"),
                    latency_ms=latency_ms,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                )
                if turn.run_id and calls_made > 1:
                    repo.increment_run_counters(turn.run_id, model_calls=calls_made - 1)

                if autonomy.enabled(run) and write.created:
                    autonomy.record_action(repo, run, turn, action)
                can_close = self._can_close(current_agent)
                if action.action == "propose_close" and can_close:
                    repo.set_thread_status(
                        thread.id,
                        ThreadStatus.CLOSED,
                        reason=f"closure proposed by @{agent.handle}",
                        actor_type=AuthorType.AGENT.value,
                        actor_id=agent.id,
                    )
                    self._cancel_thread_stimuli(
                        repo,
                        thread.id,
                        except_stimulus_id=turn.stimulus_id,
                        reason="thread closed",
                    )
                elif write.created and run is not None and turn.stimulus_id and not cadence.enabled(run):
                    parent_stimulus = repo.get_stimulus(turn.stimulus_id)
                    if parent_stimulus.cascade_depth < run.max_cascade_depth:
                        reply_parent = session.get(Post, write.post.parent_post_id) if write.post.parent_post_id else None
                        plans = plan_reactive_stimuli(
                            write.post.body,
                            repo.list_agents(enabled_only=True, agent_ids=run.config.get("agent_ids")),
                            default_kind=StimulusKind.AGENT_POST,
                            default_priority=2.0,
                            exclude_agent_id=None if autonomy.enabled(run) else agent.id,
                            reply_to_agent_id=participant_post(reply_parent).author_agent_id if reply_parent is not None else None,
                        )
                        for plan in plans:
                            repo.add_stimulus(
                                thread_id=target_thread.id,
                                run_id=run.id,
                                kind=plan.kind,
                                triggering_event_id=write.event.id,
                                source_post_id=post_id,
                                target_agent_id=plan.target_agent_id,
                                priority=plan.priority,
                                payload={**plan.payload, "origin_turn_id": turn.id},
                                cascade_depth=parent_stimulus.cascade_depth + 1,
                                dedupe_key=f"cascade:{post_id}:{plan.dedupe_label}",
                            )
                events = self._events_after(session, before)
        await self._publish_many(events)
        if post_id and turn.run_id:
            self.notify(turn.run_id)
        return post_id

    # --------------------------------------------------------------- finalizers
    async def _requeue_turn_after_exception(
        self,
        turn_id: str,
        *,
        stimulus_id: str,
        claim_token: str,
        error: str,
    ) -> bool:
        """Bound an internal failure while keeping recoverable work durable."""

        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            turn = repo.get_turn(turn_id)
            stimulus = repo.get_stimulus(stimulus_id)
            if (
                turn.state not in {TurnState.SELECTED.value, TurnState.CALLING.value}
                or stimulus.claim_token != claim_token
                or stimulus.state
                not in {StimulusState.CLAIMED.value, StimulusState.PROCESSING.value}
            ):
                return False
            will_retry = stimulus.attempts < stimulus.max_attempts
            if will_retry:
                turn.state = TurnState.SELECTED.value
                turn.outcome = None
                turn.rejection_reason = None
                turn.claim_token = None
                turn.error = None
                turn.completed_at = None
                repo.add_event(
                    "turn.requeued",
                    run_id=turn.run_id,
                    thread_id=turn.thread_id,
                    agent_id=turn.agent_id,
                    stimulus_id=turn.stimulus_id,
                    payload={"turn_id": turn.id, "error": error},
                )
            else:
                turn.state = TurnState.FAILED.value
                turn.outcome = "provider_failure"
                turn.rejection_reason = error
                turn.claim_token = None
                turn.error = error
                turn.completed_at = utc_now()
                repo.add_event(
                    "turn.failed",
                    run_id=turn.run_id,
                    thread_id=turn.thread_id,
                    agent_id=turn.agent_id,
                    stimulus_id=turn.stimulus_id,
                    payload={"turn_id": turn.id, "error": error},
                )
            repo.release_stimulus(
                stimulus.id,
                claim_token,
                error=error,
                retry_delay_seconds=self.config.retry_backoff_seconds,
            )
            events = self._events_after(session, before)
        await self._publish_many(events)
        return will_retry

    async def _defer_turn_for_pause(
        self,
        turn_id: str,
        *,
        expected_claim_token: str | None,
        latency_ms: int,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> bool:
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            turn = repo.get_turn(turn_id)
            if (
                turn.state != TurnState.CALLING.value
                or turn.claim_token != expected_claim_token
            ):
                return False
            turn.state = TurnState.SELECTED.value
            turn.outcome = None
            turn.rejection_reason = None
            turn.claim_token = None
            turn.error = None
            turn.completed_at = None
            turn.latency_ms = latency_ms or None
            turn.input_tokens = input_tokens
            turn.output_tokens = output_tokens
            turn.total_tokens = total_tokens
            repo.add_event(
                "turn.deferred",
                run_id=turn.run_id,
                thread_id=turn.thread_id,
                agent_id=turn.agent_id,
                stimulus_id=turn.stimulus_id,
                payload={
                    "turn_id": turn.id,
                    "reason": "run paused before the next model attempt",
                    "retry_count": len(turn.retry_history),
                },
            )
            events = self._events_after(session, before)
        await self._publish_many(events)
        return True

    async def _fail_turn_without_model_call(
        self,
        turn_id: str,
        *,
        expected_claim_token: str | None,
        error: str,
    ) -> None:
        """Terminalize an uncalled turn without fabricating usage counters."""

        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            turn = repo.get_turn(turn_id)
            if (
                turn.state != TurnState.CALLING.value
                or turn.claim_token != expected_claim_token
            ):
                return
            turn.state = TurnState.FAILED.value
            turn.outcome = "rejected_by_policy"
            turn.rejection_reason = error
            turn.claim_token = None
            turn.error = error
            turn.completed_at = utc_now()
            repo.add_event(
                "turn.failed",
                run_id=turn.run_id,
                thread_id=turn.thread_id,
                agent_id=turn.agent_id,
                stimulus_id=turn.stimulus_id,
                payload={
                    "turn_id": turn.id,
                    "error": error,
                    "model_calls": 0,
                },
            )
            events = self._events_after(session, before)
        await self._publish_many(events)

    async def _complete_claim(self, stimulus_id: str, claim_token: str, *, made_posts: bool) -> None:
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            stimulus = repo.get_stimulus(stimulus_id)
            if not (
                stimulus.state in {StimulusState.CLAIMED.value, StimulusState.PROCESSING.value}
                and stimulus.claim_token == claim_token
            ):
                return
            turns = list(session.scalars(select(Turn).where(Turn.stimulus_id == stimulus.id)))
            # Recovered terminal turns can have a committed post even when this
            # worker made no new model call. Never hand that work off again.
            made_posts = made_posts or any(turn.resulting_post_id for turn in turns)
            repo.complete_stimulus(stimulus.id, claim_token=claim_token)
            thread = repo.get_thread(stimulus.thread_id)
            noncontributing = bool(turns) and all(
                turn.state in {TurnState.PASSED.value, TurnState.FAILED.value} for turn in turns
            )
            # A permanently ineligible direct recipient (e.g. quota/ping-pong
            # limits) should not strand the other participants either. Cooldown
            # deferrals are still pending and return at the ownership check above.
            unavailable_target = not turns and stimulus.target_agent_id is not None
            noncontributing = noncontributing or unavailable_target
            active_run = repo.get_run(stimulus.run_id) if stimulus.run_id else None
            if stimulus.payload.get("forced"):
                # A forced opportunity belongs only to its named recipient.
                noncontributing = False
                unavailable_target = False
            if cadence.enabled(active_run):
                cadence.complete_slot(repo, active_run, stimulus, turns)
            fixed_cadence = cadence.enabled(active_run)
            if not fixed_cadence and not made_posts and noncontributing and thread.status == ThreadStatus.ACTIVE.value:
                run = repo.get_run(stimulus.run_id)
                attempted = set(stimulus.payload.get("attempted_agent_ids", []))
                attempted.update(turn.agent_id for turn in turns)
                if unavailable_target:
                    attempted.add(stimulus.target_agent_id)
                latest_author = session.scalar(
                    select(Post.author_agent_id).where(Post.thread_id == thread.id)
                    .order_by(Post.sequence.desc()).limit(1)
                )
                remaining = [agent for agent in repo.list_agents(
                    enabled_only=True, agent_ids=run.config.get("agent_ids"),
                ) if agent.id not in attempted
                    and agent.id not in run.config.get("unavailable_agent_ids", [])
                    and (autonomy.enabled(run) or agent.id != latest_author)]
                if remaining and not repo.run_budget_exhaustion(run, now=self._virtual_now(run)):
                    fallback = repo.add_stimulus(
                        thread_id=thread.id, run_id=run.id, kind=stimulus.kind,
                        triggering_event_id=stimulus.triggering_event_id,
                        source_post_id=stimulus.source_post_id, priority=stimulus.priority,
                        # Passing is not a new conversational cascade. Each
                        # participant gets at most one chance for this input.
                        cascade_depth=stimulus.cascade_depth,
                        payload={
                            **stimulus.payload,
                            "fallback_root_id": stimulus.payload.get("fallback_root_id", stimulus.id),
                            "attempted_agent_ids": sorted(attempted),
                        },
                        dedupe_key=f"fallback:{stimulus.id}",
                    )
                    repo.add_event(
                        "stimulus.fallback", run_id=run.id, thread_id=thread.id,
                        stimulus_id=fallback.id,
                        payload={"previous_stimulus_id": stimulus.id, "attempted_agent_ids": sorted(attempted)},
                    )
            if run_policy.policy_for(active_run)["dormancy"] and not fixed_cadence and not made_posts and (
                noncontributing or stimulus.payload.get("fallback_root_id")
                or stimulus.kind == StimulusKind.IDLE_REVISIT.value
            ):
                pending = session.scalar(select(func.count(Stimulus.id)).where(
                    Stimulus.thread_id == thread.id,
                    Stimulus.state.in_([StimulusState.PENDING.value, StimulusState.CLAIMED.value, StimulusState.PROCESSING.value]),
                )) or 0
                if not pending and thread.status == ThreadStatus.ACTIVE.value:
                    repo.set_thread_status(
                        thread.id,
                        ThreadStatus.DORMANT,
                        reason="no further eligible contributions",
                    )
            events = self._events_after(session, before)
        await self._publish_many(events)

    async def _advance_virtual_time(self, run_id: str, stimulus_id: str) -> None:
        """Advance virtual time once for each durably completed stimulus."""

        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            run = repo.get_run(run_id)
            stimulus = repo.get_stimulus(stimulus_id)
            already_advanced = session.scalar(
                select(Event.id).where(
                    Event.run_id == run.id,
                    Event.stimulus_id == stimulus.id,
                    Event.event_type == "run.virtual_time_advanced",
                )
            )
            if (
                not self._uses_virtual_time(run)
                or stimulus.state != StimulusState.COMPLETED.value
                or already_advanced is not None
            ):
                return
            configured = run.config.get(
                "virtual_time_step_seconds",
                self.config.virtual_time_step_seconds,
            )
            try:
                seconds = max(0.001, min(float(configured), 86_400.0))
            except (TypeError, ValueError):
                seconds = self.config.virtual_time_step_seconds
            repo.increment_run_counters(run.id, virtual_time=seconds)
            repo.add_event(
                "run.virtual_time_advanced",
                run_id=run.id,
                thread_id=stimulus.thread_id,
                stimulus_id=stimulus.id,
                payload={"seconds": seconds, "virtual_time": run.virtual_time},
            )
            events = self._events_after(session, before)
        await self._publish_many(events)

    async def _release_claim(self, stimulus_id: str, claim_token: str, error: str) -> None:
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            try:
                repo.release_stimulus(
                    stimulus_id,
                    claim_token,
                    error=error,
                    retry_delay_seconds=self.config.retry_backoff_seconds,
                )
            except (ClaimConflictError, InvalidStateError):
                pass
            events = self._events_after(session, before)
        await self._publish_many(events)

    async def _yield_claim(self, stimulus_id: str, claim_token: str, reason: str) -> None:
        """Return an uncalled claim to pending without consuming an attempt."""

        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            stimulus = repo.get_stimulus(stimulus_id)
            if (
                stimulus.claim_token == claim_token
                and stimulus.state in {StimulusState.CLAIMED.value, StimulusState.PROCESSING.value}
            ):
                stimulus.state = StimulusState.PENDING.value
                stimulus.claim_token = None
                stimulus.claimed_at = None
                stimulus.attempts = max(0, stimulus.attempts - 1)
                stimulus.not_before = utc_now()
                repo.add_event(
                    "stimulus.yielded",
                    run_id=stimulus.run_id,
                    thread_id=stimulus.thread_id,
                    post_id=stimulus.source_post_id,
                    stimulus_id=stimulus.id,
                    payload={"reason": reason},
                )
            events = self._events_after(session, before)
        await self._publish_many(events)

    async def _finish_step_once(self, run_id: str, step_once: bool) -> None:
        if not step_once:
            return
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            run = repo.get_run(run_id)
            if run.state == RunState.RUNNING.value:
                run.config = {**run.config, "step_once": False}
                repo.set_run_state(run.id, RunState.PAUSED, reason="manual step completed")
            events = self._events_after(session, before)
        await self._publish_many(events)

    async def _finish_run_if_budget_exhausted(self, run_id: str) -> None:
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            run = repo.get_run(run_id)
            reason = repo.run_budget_exhaustion(run, now=self._virtual_now(run))
            if reason and run.state not in {
                RunState.COMPLETED.value,
                RunState.STOPPED.value,
                RunState.EMERGENCY_STOPPED.value,
                RunState.FAILED.value,
            }:
                repo.set_run_state(run.id, RunState.COMPLETED, reason=reason)
            events = self._events_after(session, before)
        await self._publish_many(events)

    async def _fail_turn(self, turn_id: str, error: str) -> None:
        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            turn = repo.get_turn(turn_id)
            if turn.state not in {
                TurnState.COMPLETED.value,
                TurnState.PASSED.value,
                TurnState.FAILED.value,
            }:
                repo.finish_turn(turn.id, state=TurnState.FAILED, error=error)
            events = self._events_after(session, before)
        await self._publish_many(events)

    # ------------------------------------------------------------ maintenance
    async def _idle_maintenance(self, run_id: str) -> None:
        """Invite one idle contribution, then let a quiet thread go dormant."""

        with self.session_factory() as session, session.begin():
            before = self._last_event_id(session)
            repo = Repository(session)
            run = repo.get_run(run_id)
            if run.state != RunState.RUNNING.value or cadence.enabled(run) or not run_policy.policy_for(run)["dormancy"]:
                return
            now = self._virtual_now(run)
            threads = repo.list_threads(run_id=run.id, status=ThreadStatus.ACTIVE, limit=500)
            for thread in threads:
                age = (now - thread.latest_activity_at).total_seconds()
                pending = session.scalar(
                    select(func.count(Stimulus.id)).where(
                        Stimulus.thread_id == thread.id,
                        Stimulus.state.in_(
                            [
                                StimulusState.PENDING.value,
                                StimulusState.CLAIMED.value,
                                StimulusState.PROCESSING.value,
                            ]
                        ),
                    )
                ) or 0
                if pending:
                    continue
                if age >= self.config.dormant_seconds:
                    repo.set_thread_status(thread.id, ThreadStatus.DORMANT, reason="idle timeout")
                elif age >= self.config.idle_seconds:
                    repo.add_stimulus(
                        thread_id=thread.id,
                        run_id=run.id,
                        kind=StimulusKind.IDLE_REVISIT,
                        priority=-1.0,
                        payload={"idle_seconds": round(age, 3)},
                        cascade_depth=0,
                        dedupe_key=f"idle:{thread.id}:{thread.current_sequence}",
                    )
            events = self._events_after(session, before)
        await self._publish_many(events)
        if events:
            self.notify(run_id)

    # -------------------------------------------------------------- utilities
    @staticmethod
    def _cooldown_deferral_time(
        stimulus: Stimulus,
        decision: SchedulingDecision,
        agents: Sequence[Agent],
        *,
        now: datetime,
    ) -> datetime | None:
        """Return the earliest temporary eligibility time for durable triggers."""

        if not stimulus.payload.get("forced") and (not (stimulus.kind in {
            StimulusKind.MENTION.value,
            StimulusKind.UNANSWERED_QUESTION.value,
        } or stimulus.payload.get("fallback_root_id")) or decision.reason != "no agent is currently eligible"):
            return None
        permanent_reasons = {
            "disabled",
            "posting permission denied",
            "agent quota exhausted",
            "would immediately self-reply",
            "two-agent ping-pong limit",
        }
        by_id = {agent.id: agent for agent in agents}
        eligible_times: list[datetime] = []
        for candidate in decision.candidates:
            if "cooldown" not in candidate.components:
                continue
            if permanent_reasons.intersection(candidate.reasons):
                continue
            agent = by_id.get(candidate.agent_id)
            last_spoke = getattr(agent, "last_spoke_at", None)
            if agent is None or not isinstance(last_spoke, datetime):
                continue
            if last_spoke.tzinfo is None:
                last_spoke = last_spoke.replace(tzinfo=timezone.utc)
            cooldown_seconds = max(0.0, float(agent.cooldown_seconds or 0))
            eligible_at = last_spoke + timedelta(seconds=cooldown_seconds)
            if eligible_at > now:
                eligible_times.append(eligible_at)
        return min(eligible_times) if eligible_times else None

    def _agent_quota_remaining(
        self,
        session: Session,
        run: Run,
        agents: Sequence[Agent],
    ) -> dict[str, int]:
        if not agents:
            return {}
        rows = session.execute(
            select(Post.author_agent_id, func.count(Post.id))
            .join(Thread, Post.thread_id == Thread.id)
            .where(
                Thread.run_id == run.id,
                Post.author_agent_id.in_([agent.id for agent in agents]),
                Post.metadata_json["is_inherited"].as_boolean().is_not(True),
            )
            .group_by(Post.author_agent_id)
        ).all()
        used = {str(agent_id): int(count) for agent_id, count in rows if agent_id}
        return {agent.id: max(0, min(run.per_agent_quota,
            run.config.get("inherited_agent_quota_remaining", {}).get(agent.id, run.per_agent_quota))
            - used.get(agent.id, 0)) for agent in agents}

    def _cancel_thread_stimuli(
        self,
        repo: Repository,
        thread_id: str,
        *,
        except_stimulus_id: str | None,
        reason: str,
    ) -> None:
        stimuli = repo.list_stimuli(thread_id=thread_id, limit=10_000)
        for stimulus in stimuli:
            if stimulus.id == except_stimulus_id or stimulus.state not in {
                StimulusState.PENDING.value,
                StimulusState.CLAIMED.value,
                StimulusState.PROCESSING.value,
            }:
                continue
            stimulus.state = StimulusState.CANCELLED.value
            stimulus.claim_token = None
            stimulus.completed_at = utc_now()
            repo.add_event(
                "stimulus.cancelled",
                run_id=stimulus.run_id,
                thread_id=stimulus.thread_id,
                post_id=stimulus.source_post_id,
                stimulus_id=stimulus.id,
                payload={"reason": reason},
            )

    def _can_close(self, agent: Agent) -> bool:
        permissions = dict(agent.permissions or {})
        if permissions.get("close_threads", permissions.get("can_close_threads")) is not None:
            return bool(permissions.get("close_threads", permissions.get("can_close_threads")))
        return agent.role.casefold() == "moderator"

    def _context_limit(self, run: Run) -> int:
        configured = run.config.get("context_post_limit") if isinstance(run.config, Mapping) else None
        try:
            return max(1, min(int(configured or self.config.context_post_limit), 500))
        except (TypeError, ValueError):
            return self.config.context_post_limit

    @staticmethod
    def _run_config_int(
        run: Run | None,
        key: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        configured = run.config.get(key) if run is not None and isinstance(run.config, Mapping) else None
        try:
            return max(minimum, min(int(default if configured is None else configured), maximum))
        except (TypeError, ValueError):
            return default

    def _sampling_settings(self, agent: Agent, run: Run) -> dict[str, Any]:
        sampling: dict[str, Any] = {}
        if isinstance(run.config, Mapping) and isinstance(run.config.get("sampling"), Mapping):
            sampling.update(run.config["sampling"])
        if isinstance(agent.settings, Mapping) and isinstance(agent.settings.get("sampling"), Mapping):
            sampling.update(agent.settings["sampling"])
        # Codex CLI does not expose a per-call token cap. Do not record an
        # invented default as if it were sent to the provider.
        if agent.provider == "codex":
            return sampling
        remaining = max(1, run.max_tokens - run.tokens_used)
        configured = int(sampling.get("max_tokens", DEFAULT_AGENT_OUTPUT_TOKENS))
        sampling["max_tokens"] = min(configured, remaining)
        return sampling

    def _virtual_now(self, run: Run) -> datetime:
        if self._uses_virtual_time(run):
            origin = run.started_at or run.created_at
            return origin + timedelta(seconds=run.virtual_time)
        return datetime.now(timezone.utc)

    @staticmethod
    def _uses_virtual_time(run: Run) -> bool:
        return bool(isinstance(run.config, Mapping) and run.config.get("use_virtual_time"))

    def _run_can_call(self, run_id: str | None, *, allow_paused: bool = False) -> bool:
        if run_id is None:
            return True
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                return False
            if run.state == RunState.RUNNING.value:
                return (
                    Repository(session).run_budget_exhaustion(
                        run,
                        now=self._virtual_now(run),
                    )
                    is None
                )
            return allow_paused and run.state == RunState.PAUSED.value

    def _run_is_paused(self, run_id: str | None) -> bool:
        if run_id is None:
            return False
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            return bool(run is not None and run.state == RunState.PAUSED.value)

    def _agent_can_speak(self, agent_id: str) -> bool:
        with self.session_factory() as session:
            agent = session.get(Agent, agent_id)
            if agent is None or not agent.enabled:
                return False
            permissions = dict(agent.permissions or {})
            return bool(permissions.get("speak", permissions.get("can_post", True)))

    @staticmethod
    def _is_permanent_policy_rejection(reason: str) -> bool:
        return reason in {
            "agent is disabled",
            "agent is not permitted to post",
            "agent is not permitted to create threads",
            "thread is closed",
        }

    def _decode_prompt(self, prompt: str | None) -> list[ChatMessage]:
        if not prompt:
            raise ValueError("turn is missing its captured prompt")
        decoded = json.loads(prompt)
        if not isinstance(decoded, list):
            raise ValueError("captured prompt is not a message list")
        return [ChatMessage.model_validate(message) for message in decoded]

    def _coerce_gateway_result(self, value: Any, agent: Agent) -> GatewayResult:
        """Normalize an injected dispatcher result without providing a fake model."""

        if isinstance(value, GatewayResult):
            return value
        if isinstance(value, AgentAction):
            action = value
            raw = action.model_dump_json()
        elif isinstance(value, str):
            raw = value
            action = parse_agent_action(raw)
        elif isinstance(value, Mapping):
            raw = json.dumps(dict(value), separators=(",", ":"))
            action = parse_agent_action(raw)
        else:
            raise GatewayError("gateway returned an unsupported result type")
        return GatewayResult(
            action=action,
            raw_output=raw,
            provider=str(getattr(agent, "provider", "injected")),
            model=str(getattr(agent, "model", "injected")),
            latency_ms=0,
            usage=TokenUsage(),
        )

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        return f"{error.__class__.__name__}: {str(error)[:2_000]}"

    @staticmethod
    def _last_event_id(session: Session) -> int:
        return int(session.scalar(select(func.coalesce(func.max(Event.id), 0))) or 0)

    @staticmethod
    def _events_after(session: Session, event_id: int) -> list[dict[str, Any]]:
        session.flush()
        events = list(session.scalars(select(Event).where(Event.id > event_id).order_by(Event.id)))
        return [_event_dict(event) for event in events]

    async def _publish_many(self, events: Sequence[dict[str, Any]]) -> None:
        if self.publish is None:
            return
        for event in events:
            try:
                result = self.publish(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # The committed SQLite event remains authoritative; a dropped
                # process-local notification is repaired by the UI's next read.
                logger.exception("event publish callback failed for event %s", event.get("id"))


__all__ = [
    "EngineConfig",
    "GatewayDispatcher",
    "StepResult",
    "SwarmEngine",
]
