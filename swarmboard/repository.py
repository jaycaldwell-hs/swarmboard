"""Transaction-oriented persistence operations for Swarmboard.

Repository methods flush but never commit.  A caller therefore gets one atomic
unit containing a state change and its audit event by wrapping calls in
``SessionLocal.begin()`` (or by committing the request-scoped session).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import Select, and_, desc, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .config import DEFAULT_RUN_MAX_TOKENS
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
    TurnOutcome,
    TurnState,
    infer_turn_outcome,
    new_uuid,
    normalize_agent_handle,
    utc_now,
)
from .run_policy import normalize_config


class RepositoryError(RuntimeError):
    pass


class NotFoundError(RepositoryError):
    pass


class InvalidStateError(RepositoryError):
    pass


class ClaimConflictError(RepositoryError):
    pass


class IdempotencyConflictError(InvalidStateError):
    """A delivery key is already owned by a different logical operation."""


class _RollbackDuplicateThread(RuntimeError):
    """Internal signal used to roll back a newly minted duplicate thread."""


_TERMINAL_RUN_STATES = {
    RunState.STOPPED.value,
    RunState.COMPLETED.value,
    RunState.EMERGENCY_STOPPED.value,
    RunState.FAILED.value,
}


@dataclass(frozen=True, slots=True)
class PostWriteResult:
    post: Post
    event: Event
    created: bool


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    stimuli_requeued: int = 0
    stimuli_failed: int = 0
    turns_failed: int = 0


class Repository:
    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------ events
    def add_event(
        self,
        event_type: str,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        post_id: str | None = None,
        agent_id: str | None = None,
        stimulus_id: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Event:
        event = Event(
            event_type=event_type,
            run_id=run_id,
            thread_id=thread_id,
            post_id=post_id,
            agent_id=agent_id,
            stimulus_id=stimulus_id,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=dict(payload or {}),
        )
        self.session.add(event)
        self.session.flush()
        return event

    def get_event(self, event_id: int) -> Event:
        event = self.session.get(Event, event_id)
        if event is None:
            raise NotFoundError(f"event {event_id} was not found")
        return event

    def list_events(
        self,
        *,
        after_id: int = 0,
        run_id: str | None = None,
        thread_id: str | None = None,
        limit: int = 500,
    ) -> list[Event]:
        stmt = select(Event).where(Event.id > after_id)
        if run_id is not None:
            stmt = stmt.where(Event.run_id == run_id)
        if thread_id is not None:
            stmt = stmt.where(Event.thread_id == thread_id)
        stmt = stmt.order_by(Event.id).limit(limit)
        return list(self.session.scalars(stmt))

    # ------------------------------------------------------------------ agents
    def create_agent(
        self,
        *,
        handle: str,
        persona: str,
        model: str,
        role: str = "specialist",
        provider: str = "openai_compatible",
        settings: Mapping[str, Any] | None = None,
        permissions: Mapping[str, Any] | None = None,
        cooldown_seconds: int = 15,
        enabled: bool = True,
        expertise: Sequence[str] | None = None,
    ) -> Agent:
        agent_settings = dict(settings) if settings is not None else (
            {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"}
            if provider == "openai_compatible" else {}
        )
        if expertise is not None:
            agent_settings.setdefault("expertise", list(expertise))
        normalized_handle = normalize_agent_handle(handle)
        agent = Agent(
            handle=normalized_handle,
            persona=persona.strip(),
            role=role,
            provider=provider,
            model=model,
            settings=agent_settings,
            permissions=dict(permissions or {}),
            cooldown_seconds=cooldown_seconds,
            enabled=enabled,
        )
        try:
            with self.session.begin_nested():
                self.session.add(agent)
                self.session.flush()
                self.add_event(
                    "agent.created",
                    agent_id=agent.id,
                    actor_type="system",
                    payload={
                        "handle": agent.handle,
                        "role": agent.role,
                        "provider": agent.provider,
                    },
                )
        except IntegrityError:
            existing = self.session.scalar(
                select(Agent).where(Agent.handle == normalized_handle)
            )
            if existing is not None:
                raise InvalidStateError(
                    f"agent handle @{normalized_handle} already exists"
                ) from None
            raise
        return agent

    def upsert_agent(self, *, handle: str, **values: Any) -> Agent:
        normalized = normalize_agent_handle(handle)
        agent = self.session.scalar(select(Agent).where(Agent.handle == normalized))
        expertise = values.pop("expertise", None)
        if agent is None:
            return self.create_agent(handle=normalized, expertise=expertise, **values)
        allowed = {
            "persona",
            "model",
            "role",
            "provider",
            "settings",
            "permissions",
            "cooldown_seconds",
            "enabled",
        }
        for name, value in values.items():
            if name not in allowed:
                raise ValueError(f"unknown Agent field: {name}")
            if name in {"settings", "permissions"}:
                value = dict(value)
            setattr(agent, name, value)
        if expertise is not None:
            agent.settings = {**agent.settings, "expertise": list(expertise)}
        self.session.flush()
        self.add_event(
            "agent.updated",
            agent_id=agent.id,
            actor_type="system",
            payload={"handle": agent.handle},
        )
        return agent

    def get_agent(self, agent_id: str) -> Agent:
        agent = self.session.get(Agent, agent_id)
        if agent is None:
            raise NotFoundError(f"agent {agent_id} was not found")
        return agent

    def get_agent_by_handle(self, handle: str) -> Agent:
        normalized = normalize_agent_handle(handle)
        agent = self.session.scalar(select(Agent).where(Agent.handle == normalized))
        if agent is None:
            raise NotFoundError(f"agent @{normalized} was not found")
        return agent

    def list_agents(
        self, *, enabled_only: bool = False, agent_ids: Sequence[str] | None = None,
    ) -> list[Agent]:
        stmt = select(Agent)
        if enabled_only:
            stmt = stmt.where(Agent.enabled.is_(True))
        if agent_ids is not None:
            stmt = stmt.where(Agent.id.in_(agent_ids))
        return list(self.session.scalars(stmt.order_by(Agent.handle)))

    # -------------------------------------------------------------------- runs
    def create_run(
        self,
        *,
        seed: int = 0,
        continuous: bool = False,
        config: Mapping[str, Any] | None = None,
        max_rounds: int = 50,
        max_posts: int = 200,
        max_tokens: int = DEFAULT_RUN_MAX_TOKENS,
        max_duration_seconds: int = 3_600,
        per_agent_quota: int = 50,
        per_thread_quota: int = 100,
        max_cascade_depth: int = 8,
        state: RunState | str = RunState.CREATED,
    ) -> Run:
        try:
            normalized_config = normalize_config(config)
        except ValueError as exc:
            raise InvalidStateError(str(exc)) from None
        run = Run(
            state=str(state),
            seed=seed,
            continuous=continuous,
            config=normalized_config,
            max_rounds=max_rounds,
            max_posts=max_posts,
            max_tokens=max_tokens,
            max_duration_seconds=max_duration_seconds,
            per_agent_quota=per_agent_quota,
            per_thread_quota=per_thread_quota,
            max_cascade_depth=max_cascade_depth,
        )
        self.session.add(run)
        self.session.flush()
        self.add_event(
            "run.created",
            run_id=run.id,
            actor_type="human",
            payload={
                "seed": seed, "continuous": continuous,
                "session_type": run.config["session_type"],
                "policy": deepcopy(run.config["policy"]),
            },
        )
        return run

    def get_run(self, run_id: str) -> Run:
        run = self.session.get(Run, run_id)
        if run is None:
            raise NotFoundError(f"run {run_id} was not found")
        return run

    def list_runs(self, *, limit: int = 100) -> list[Run]:
        return list(self.session.scalars(select(Run).where(Run.config["discarded"].as_boolean().is_not(True))
                                        .order_by(desc(Run.created_at)).limit(limit)))

    def control_run(self, run_id: str, action: str, *, reason: str | None = None) -> Run:
        run = self.get_run(run_id)
        now = utc_now()
        action = action.lower().strip()
        terminal = {
            RunState.STOPPED.value,
            RunState.COMPLETED.value,
            RunState.EMERGENCY_STOPPED.value,
            RunState.FAILED.value,
        }
        if run.state in terminal:
            raise InvalidStateError(f"run {run.id} is terminal ({run.state})")

        if action in {"start", "resume"}:
            if run.state not in {RunState.CREATED.value, RunState.PAUSED.value}:
                raise InvalidStateError(f"cannot {action} a {run.state} run")
            run.state = RunState.RUNNING.value
            run.started_at = run.started_at or now
            run.paused_at = None
            run.config = {**run.config, "step_once": False}
        elif action == "pause":
            if run.state != RunState.RUNNING.value:
                raise InvalidStateError(f"cannot pause a {run.state} run")
            run.state = RunState.PAUSED.value
            run.paused_at = now
        elif action == "step":
            if run.state not in {RunState.CREATED.value, RunState.PAUSED.value}:
                raise InvalidStateError(f"cannot step a {run.state} run")
            run.state = RunState.RUNNING.value
            run.continuous = False
            run.started_at = run.started_at or now
            run.config = {**run.config, "step_once": True}
        elif action == "stop":
            run.state = RunState.STOPPED.value
            run.stopped_at = now
            run.finished_at = now
            run.stop_reason = reason
        elif action == "emergency_stop":
            run.state = RunState.EMERGENCY_STOPPED.value
            run.stopped_at = now
            run.finished_at = now
            run.stop_reason = reason or "emergency stop"
        else:
            raise ValueError(f"unknown run control action: {action}")

        run.heartbeat_at = now
        self.session.flush()
        self.add_event(
            f"run.{action}",
            run_id=run.id,
            actor_type="human",
            payload={"state": run.state, "reason": reason},
        )
        if run.state in _TERMINAL_RUN_STATES:
            self.cleanup_terminal_run_work(run_id=run.id)
        return run

    def set_run_state(
        self,
        run_id: str,
        state: RunState | str,
        *,
        reason: str | None = None,
        actor_type: str = "system",
    ) -> Run:
        run = self.get_run(run_id)
        new_state = str(state)
        run.state = new_state
        now = utc_now()
        if new_state == RunState.PAUSED.value:
            run.paused_at = now
        if new_state in {
            RunState.STOPPED.value,
            RunState.COMPLETED.value,
            RunState.EMERGENCY_STOPPED.value,
            RunState.FAILED.value,
        }:
            run.finished_at = now
            run.stop_reason = reason
        self.session.flush()
        self.add_event(
            f"run.{new_state}",
            run_id=run.id,
            actor_type=actor_type,
            payload={"state": new_state, "reason": reason},
        )
        if new_state in _TERMINAL_RUN_STATES:
            self.cleanup_terminal_run_work(run_id=run.id)
        return run

    def cleanup_terminal_run_work(self, *, run_id: str | None = None) -> dict[str, int]:
        """Cancel leftover deliveries and fence unfinished turns in ended runs.

        Rows and past events are retained for replay. Repeating cleanup is a
        no-op, and active, paused, and not-yet-started runs are never touched.
        """

        terminal_ids = select(Run.id).where(Run.state.in_(_TERMINAL_RUN_STATES))
        if run_id is not None:
            terminal_ids = terminal_ids.where(Run.id == run_id)
        stimuli = list(self.session.scalars(select(Stimulus).where(
            Stimulus.run_id.in_(terminal_ids),
            Stimulus.state.in_({
                StimulusState.PENDING.value,
                StimulusState.CLAIMED.value,
                StimulusState.PROCESSING.value,
            }),
        )))
        turns = list(self.session.scalars(select(Turn).where(
            Turn.run_id.in_(terminal_ids),
            Turn.state.in_({TurnState.SELECTED.value, TurnState.CALLING.value}),
        )))
        now = utc_now()
        for stimulus in stimuli:
            run = self.get_run(stimulus.run_id)
            prior_state = stimulus.state
            reason = run.stop_reason or f"run {run.state}"
            stimulus.state = StimulusState.CANCELLED.value
            stimulus.claim_token = None
            stimulus.claimed_at = None
            stimulus.completed_at = now
            stimulus.updated_at = now
            stimulus.last_error = reason
            self.add_event(
                "stimulus.cancelled", run_id=run.id, thread_id=stimulus.thread_id,
                post_id=stimulus.source_post_id, agent_id=stimulus.target_agent_id,
                stimulus_id=stimulus.id,
                payload={"from": prior_state, "reason": reason, "terminal_state": run.state},
            )
        for turn in turns:
            run = self.get_run(turn.run_id)
            prior_state = turn.state
            reason = run.stop_reason or f"run {run.state}"
            turn.state = TurnState.FAILED.value
            turn.claim_token = None
            turn.error = reason
            turn.outcome = TurnOutcome.REJECTED_BY_POLICY.value
            turn.rejection_reason = reason
            turn.completed_at = now
            self.add_event(
                "turn.failed", run_id=run.id, thread_id=turn.thread_id,
                agent_id=turn.agent_id, stimulus_id=turn.stimulus_id,
                payload={
                    "turn_id": turn.id, "from": prior_state, "error": reason,
                    "outcome": turn.outcome, "rejection_reason": turn.rejection_reason,
                    "session_type": turn.session_type,
                    "policy_snapshot": deepcopy(turn.policy_snapshot),
                    "terminal_state": run.state,
                    "cancelled_by_run_control": run.state in {
                        RunState.STOPPED.value, RunState.EMERGENCY_STOPPED.value,
                    },
                },
            )
        self.session.flush()
        return {"stimuli_cancelled": len(stimuli), "turns_failed": len(turns)}

    def increment_run_counters(
        self,
        run_id: str,
        *,
        rounds: int = 0,
        posts: int = 0,
        tokens: int = 0,
        model_calls: int = 0,
        virtual_time: float = 0.0,
    ) -> Run:
        now = utc_now()
        updated = self.session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                rounds_used=Run.rounds_used + rounds,
                posts_used=Run.posts_used + posts,
                tokens_used=Run.tokens_used + tokens,
                model_calls=Run.model_calls + model_calls,
                virtual_time=Run.virtual_time + virtual_time,
                heartbeat_at=now,
                updated_at=now,
            )
            .returning(Run.id)
        ).scalar_one_or_none()
        if updated is None:
            raise NotFoundError(f"run {run_id} was not found")
        self.session.flush()
        return self.get_run(run_id)

    def heartbeat_run(self, run_id: str, *, at: datetime | None = None) -> Run:
        run = self.get_run(run_id)
        run.heartbeat_at = at or utc_now()
        self.session.flush()
        return run

    def run_budget_exhaustion(self, run: Run | str, *, now: datetime | None = None) -> str | None:
        current = self.get_run(run) if isinstance(run, str) else run
        if current.rounds_used >= current.max_rounds:
            return "maximum rounds reached"
        if current.posts_used >= current.max_posts:
            return "maximum posts reached"
        if current.tokens_used >= current.max_tokens:
            return "maximum tokens reached"
        if current.started_at is not None:
            elapsed = ((now or utc_now()) - current.started_at).total_seconds()
            if elapsed >= current.max_duration_seconds:
                return "maximum duration reached"
        return None

    # ------------------------------------------------------------------ threads
    def create_thread(
        self,
        *,
        title: str,
        run_id: str | None = None,
        summary: str | None = None,
        actor_type: str = "human",
        actor_id: str | None = None,
    ) -> Thread:
        if run_id is not None:
            self.get_run(run_id)
        thread = Thread(run_id=run_id, title=title.strip(), summary=summary)
        self.session.add(thread)
        self.session.flush()
        self.add_event(
            "thread.created",
            run_id=run_id,
            thread_id=thread.id,
            actor_type=actor_type,
            actor_id=actor_id,
            payload={"title": thread.title},
        )
        return thread

    def create_human_thread(
        self,
        *,
        title: str,
        body: str,
        run_id: str | None = None,
        author_handle: str = "human",
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> PostWriteResult:
        author_type = AuthorType.SYSTEM if author_handle == "SYSTEM" else AuthorType.HUMAN
        existing = self._post_result_for_idempotency_key(
            idempotency_key,
            expected_author_type=author_type,
            expected_operation="new_thread",
        )
        if existing is not None:
            return existing
        if run_id is not None:
            run = self.get_run(run_id)
            if run.state in _TERMINAL_RUN_STATES:
                raise InvalidStateError(
                    f"run {run.id} is terminal ({run.state}); rerun or create a fresh thread"
                )

        if idempotency_key is None:
            thread = self.create_thread(
                title=title,
                run_id=run_id,
                actor_type=author_type.value,
                actor_id=author_handle,
            )
            return self.create_post(
                thread_id=thread.id,
                body=body,
                author_type=author_type,
                author_handle=author_handle,
                metadata=metadata,
                operation="new_thread",
                allow_cross_thread_idempotency=True,
            )

        try:
            # Include thread creation in a savepoint.  If another delivery wins
            # the globally unique post key, the losing delivery must not leave
            # behind an empty thread or a misleading thread.created event.
            with self.session.begin_nested():
                thread = self.create_thread(
                    title=title,
                    run_id=run_id,
                    actor_type=author_type.value,
                    actor_id=author_handle,
                )
                result = self.create_post(
                    thread_id=thread.id,
                    body=body,
                    author_type=author_type,
                    author_handle=author_handle,
                    idempotency_key=idempotency_key,
                    metadata=metadata,
                    operation="new_thread",
                    allow_cross_thread_idempotency=True,
                )
                if not result.created:
                    raise _RollbackDuplicateThread
                return result
        except _RollbackDuplicateThread:
            existing = self._post_result_for_idempotency_key(
                idempotency_key,
                expected_author_type=AuthorType.HUMAN,
                expected_operation="new_thread",
            )
            if existing is None:  # pragma: no cover - defensive invariant
                raise RepositoryError("duplicate post disappeared during thread rollback")
            return existing

    def create_agent_thread(
        self,
        *,
        title: str,
        agent_id: str,
        body: str,
        run_id: str | None = None,
        intent: str | None = None,
        idempotency_key: str | None = None,
        stimulus_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> PostWriteResult:
        """Atomically create an agent-started thread and its first post.

        Engine new-thread actions must use this operation rather than composing
        ``create_thread`` and ``create_agent_post``.  The savepoint removes the
        speculative thread when a racing/redelivered action already committed
        the same global delivery key.
        """

        agent = self.get_agent(agent_id)
        if idempotency_key is None and stimulus_id is not None:
            idempotency_key = f"stimulus:{stimulus_id}:agent:{agent_id}"
        existing = self._post_result_for_idempotency_key(
            idempotency_key,
            expected_author_type=AuthorType.AGENT,
            expected_agent_id=agent.id,
            expected_stimulus_id=stimulus_id,
            expected_operation="new_thread",
        )
        if existing is not None:
            return existing
        if run_id is not None:
            run = self.get_run(run_id)
            if run.state in _TERMINAL_RUN_STATES:
                raise InvalidStateError(
                    f"run {run.id} is terminal ({run.state}); rerun or create a fresh thread"
                )

        if idempotency_key is None:
            thread = self.create_thread(
                title=title,
                run_id=run_id,
                actor_type=AuthorType.AGENT.value,
                actor_id=agent.id,
            )
            return self.create_agent_post(
                thread.id,
                agent.id,
                body,
                intent=intent,
                stimulus_id=stimulus_id,
                metadata=metadata,
                _operation="new_thread",
                _allow_cross_thread_idempotency=True,
            )

        try:
            with self.session.begin_nested():
                thread = self.create_thread(
                    title=title,
                    run_id=run_id,
                    actor_type=AuthorType.AGENT.value,
                    actor_id=agent.id,
                )
                result = self.create_agent_post(
                    thread.id,
                    agent.id,
                    body,
                    intent=intent,
                    idempotency_key=idempotency_key,
                    stimulus_id=stimulus_id,
                    metadata=metadata,
                    _operation="new_thread",
                    _allow_cross_thread_idempotency=True,
                )
                if not result.created:
                    raise _RollbackDuplicateThread
                return result
        except _RollbackDuplicateThread:
            existing = self._post_result_for_idempotency_key(
                idempotency_key,
                expected_author_type=AuthorType.AGENT,
                expected_agent_id=agent.id,
                expected_stimulus_id=stimulus_id,
                expected_operation="new_thread",
            )
            if existing is None:  # pragma: no cover - defensive invariant
                raise RepositoryError("duplicate post disappeared during thread rollback")
            return existing

    def get_thread(self, thread_id: str, *, with_posts: bool = False) -> Thread:
        if with_posts:
            thread = self.session.scalar(
                select(Thread).options(selectinload(Thread.posts)).where(Thread.id == thread_id)
            )
        else:
            thread = self.session.get(Thread, thread_id)
        if thread is None:
            raise NotFoundError(f"thread {thread_id} was not found")
        return thread

    def list_threads(
        self,
        *,
        run_id: str | None = None,
        status: ThreadStatus | str | None = None,
        limit: int = 100,
    ) -> list[Thread]:
        discarded = select(Run.id).where(Run.config["discarded"].as_boolean().is_(True))
        stmt = select(Thread).where(Thread.run_id.is_(None) | Thread.run_id.not_in(discarded))
        if run_id is not None:
            stmt = stmt.where(Thread.run_id == run_id)
        if status is not None:
            stmt = stmt.where(Thread.status == str(status))
        stmt = stmt.order_by(desc(Thread.latest_activity_at), desc(Thread.created_at)).limit(limit)
        return list(self.session.scalars(stmt))

    def set_thread_status(
        self,
        thread_id: str,
        status: ThreadStatus | str,
        *,
        reason: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
    ) -> Thread:
        thread = self.get_thread(thread_id)
        new_status = str(status)
        if thread.status == new_status:
            return thread
        if thread.run_id is not None:
            run = self.get_run(thread.run_id)
            if run.state in _TERMINAL_RUN_STATES and new_status != ThreadStatus.CLOSED.value:
                raise InvalidStateError(
                    f"run {run.id} is terminal ({run.state}); rerun or create a fresh thread"
                )
        now = utc_now()
        old_status = thread.status
        thread.status = new_status
        if new_status == ThreadStatus.ACTIVE.value:
            thread.dormant_at = None
            thread.closed_at = None
            thread.wake_reason = reason
        elif new_status == ThreadStatus.DORMANT.value:
            thread.dormant_at = now
        elif new_status == ThreadStatus.CLOSED.value:
            thread.closed_at = now
        else:
            raise ValueError(f"unknown thread status: {new_status}")
        self.session.flush()
        self.add_event(
            f"thread.{new_status}",
            run_id=thread.run_id,
            thread_id=thread.id,
            actor_type=actor_type,
            actor_id=actor_id,
            payload={"from": old_status, "to": new_status, "reason": reason},
        )
        return thread

    def wake_thread(
        self,
        thread_id: str,
        *,
        reason: str,
        actor_type: str = "system",
        actor_id: str | None = None,
    ) -> Thread:
        thread = self.get_thread(thread_id)
        if thread.run_id is not None:
            run = self.get_run(thread.run_id)
            if run.state in _TERMINAL_RUN_STATES:
                raise InvalidStateError(
                    f"run {run.id} is terminal ({run.state}); rerun or create a fresh thread"
                )
        if thread.status == ThreadStatus.CLOSED.value:
            raise InvalidStateError("closed threads cannot be woken")
        return self.set_thread_status(
            thread_id,
            ThreadStatus.ACTIVE,
            reason=reason,
            actor_type=actor_type,
            actor_id=actor_id,
        )

    # -------------------------------------------------------------------- posts
    def _post_result_for_idempotency_key(
        self,
        idempotency_key: str | None,
        *,
        expected_author_type: AuthorType | str | None = None,
        expected_agent_id: str | None = None,
        expected_stimulus_id: str | None = None,
        expected_operation: str | None = None,
        expected_thread_id: str | None = None,
    ) -> PostWriteResult | None:
        if idempotency_key is None:
            return None
        existing = self.session.scalar(
            select(Post).where(Post.idempotency_key == idempotency_key)
        )
        if existing is None:
            return None
        event = self.session.scalar(
            select(Event)
            .where(Event.post_id == existing.id, Event.event_type == "post.created")
            .order_by(Event.id)
        )
        if event is None:
            raise RepositoryError("idempotent post exists without its audit event")

        expected_author = (
            str(expected_author_type) if expected_author_type is not None else None
        )
        conflict = expected_author is not None and existing.author_type != expected_author
        if expected_agent_id is not None:
            conflict = conflict or existing.author_agent_id != expected_agent_id
            conflict = conflict or event.agent_id != expected_agent_id
        if expected_stimulus_id is not None:
            conflict = conflict or event.stimulus_id != expected_stimulus_id
        if expected_thread_id is not None:
            conflict = conflict or existing.thread_id != expected_thread_id

        recorded_operation = event.payload.get("operation")
        if recorded_operation is None:
            recorded_operation = existing.metadata_json.get("_swarmboard_operation")
        if recorded_operation is None:
            legacy_action = existing.metadata_json.get("action")
            if legacy_action is not None:
                recorded_operation = (
                    "new_thread" if legacy_action == "new_thread" else "post"
                )
        if (
            expected_operation is not None
            and recorded_operation is not None
            and recorded_operation != expected_operation
        ):
            conflict = True

        if conflict:
            raise IdempotencyConflictError(
                f"idempotency key {idempotency_key!r} belongs to a different post operation"
            )
        return PostWriteResult(existing, event, False)

    def create_human_post(
        self,
        thread_id: str,
        body: str,
        *,
        parent_post_id: str | None = None,
        author_handle: str = "human",
        idempotency_key: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        _operation: str = "post",
        _allow_cross_thread_idempotency: bool = False,
    ) -> PostWriteResult:
        author_type = AuthorType.SYSTEM if author_handle == "SYSTEM" else AuthorType.HUMAN
        return self.create_post(
            thread_id=thread_id,
            body=body,
            author_type=author_type,
            author_handle=author_handle,
            parent_post_id=parent_post_id,
            idempotency_key=idempotency_key,
            metadata=metadata,
            operation=_operation,
            allow_cross_thread_idempotency=_allow_cross_thread_idempotency,
        )

    def create_agent_post(
        self,
        thread_id: str,
        agent_id: str,
        body: str,
        *,
        parent_post_id: str | None = None,
        intent: str | None = None,
        idempotency_key: str | None = None,
        stimulus_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        _operation: str = "post",
        _allow_cross_thread_idempotency: bool = False,
    ) -> PostWriteResult:
        agent = self.get_agent(agent_id)
        if idempotency_key is None and stimulus_id is not None:
            idempotency_key = f"stimulus:{stimulus_id}:agent:{agent_id}"
        result = self.create_post(
            thread_id=thread_id,
            body=body,
            author_type=AuthorType.AGENT,
            author_handle=agent.handle,
            author_agent_id=agent.id,
            parent_post_id=parent_post_id,
            intent=intent,
            idempotency_key=idempotency_key,
            stimulus_id=stimulus_id,
            metadata=metadata,
            operation=_operation,
            allow_cross_thread_idempotency=_allow_cross_thread_idempotency,
        )
        if result.created:
            agent.last_spoke_at = result.post.created_at
            self.session.flush()
        return result

    def create_post(
        self,
        *,
        thread_id: str,
        body: str,
        author_type: AuthorType | str,
        author_handle: str,
        author_agent_id: str | None = None,
        parent_post_id: str | None = None,
        intent: str | None = None,
        idempotency_key: str | None = None,
        stimulus_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        event_payload: Mapping[str, Any] | None = None,
        operation: str = "post",
        allow_cross_thread_idempotency: bool = False,
    ) -> PostWriteResult:
        clean_body = body.strip()
        if not clean_body:
            raise ValueError("post body cannot be empty")

        author_value = str(author_type)
        existing_result = self._post_result_for_idempotency_key(
            idempotency_key,
            expected_author_type=author_value,
            expected_agent_id=author_agent_id,
            expected_stimulus_id=stimulus_id,
            expected_operation=operation,
            expected_thread_id=None if allow_cross_thread_idempotency else thread_id,
        )
        if existing_result is not None:
            return existing_result

        thread = self.get_thread(thread_id)
        if thread.run_id is not None:
            run = self.get_run(thread.run_id)
            if run.state in _TERMINAL_RUN_STATES:
                raise InvalidStateError(
                    f"run {run.id} is terminal ({run.state}); rerun or create a fresh thread"
                )
        if thread.status == ThreadStatus.CLOSED.value:
            raise InvalidStateError("cannot add a post to a closed thread")
        if thread.status == ThreadStatus.DORMANT.value:
            if author_value != AuthorType.HUMAN.value:
                raise InvalidStateError("dormant threads require an explicit wake trigger")
            self.wake_thread(
                thread_id,
                reason="human_post",
                actor_type=AuthorType.HUMAN.value,
                actor_id=author_handle,
            )

        if parent_post_id is not None:
            parent = self.get_post(parent_post_id)
            if parent.thread_id != thread_id:
                raise ValueError("parent post belongs to a different thread")
        if author_agent_id is not None:
            self.get_agent(author_agent_id)

        now = utc_now()
        try:
            # The savepoint lets a racing delivery lose the unique-key insert
            # without poisoning the caller's larger transaction.  It can then
            # return the already committed post as a successful idempotent write.
            with self.session.begin_nested():
                sequence = self.session.execute(
                    update(Thread)
                    .where(Thread.id == thread_id)
                    .values(
                        current_sequence=Thread.current_sequence + 1,
                        latest_activity_at=now,
                        updated_at=now,
                    )
                    .returning(Thread.current_sequence)
                ).scalar_one()

                post = Post(
                    thread_id=thread_id,
                    parent_post_id=parent_post_id,
                    author_type=author_value,
                    author_agent_id=author_agent_id,
                    author_handle=author_handle.strip().lstrip("@") or "human",
                    body=clean_body,
                    sequence=sequence,
                    intent=intent,
                    idempotency_key=idempotency_key,
                    metadata_json={
                        **dict(metadata or {}),
                        "_swarmboard_operation": operation,
                        "_swarmboard_stimulus_id": stimulus_id,
                    },
                    created_at=now,
                )
                self.session.add(post)
                self.session.flush()
                payload: dict[str, Any] = {
                    "sequence": sequence,
                    "parent_post_id": parent_post_id,
                    "author_type": author_value,
                    "author_handle": post.author_handle,
                    "intent": intent,
                    "operation": operation,
                }
                payload.update(event_payload or {})
                event = self.add_event(
                    "post.created",
                    run_id=thread.run_id,
                    thread_id=thread_id,
                    post_id=post.id,
                    agent_id=author_agent_id,
                    stimulus_id=stimulus_id,
                    actor_type=author_value,
                    actor_id=author_agent_id or post.author_handle,
                    payload=payload,
                )
                if thread.run_id is not None:
                    self.increment_run_counters(thread.run_id, posts=1)
        except IntegrityError:
            if idempotency_key is None:
                raise
            existing_result = self._post_result_for_idempotency_key(
                idempotency_key,
                expected_author_type=author_value,
                expected_agent_id=author_agent_id,
                expected_stimulus_id=stimulus_id,
                expected_operation=operation,
                expected_thread_id=None if allow_cross_thread_idempotency else thread_id,
            )
            if existing_result is None:
                raise
            return existing_result
        return PostWriteResult(post, event, True)

    def get_post(self, post_id: str) -> Post:
        post = self.session.get(Post, post_id)
        if post is None:
            raise NotFoundError(f"post {post_id} was not found")
        return post

    def list_posts(
        self,
        thread_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[Post]:
        self.get_thread(thread_id)
        return list(
            self.session.scalars(
                select(Post)
                .where(Post.thread_id == thread_id, Post.sequence > after_sequence)
                .order_by(Post.sequence)
                .limit(limit)
            )
        )

    def thread_snapshot(self, thread_id: str, *, post_limit: int = 100) -> dict[str, Any]:
        thread = self.get_thread(thread_id)
        posts_desc = list(
            self.session.scalars(
                select(Post)
                .where(Post.thread_id == thread_id)
                .order_by(desc(Post.sequence))
                .limit(post_limit)
            )
        )
        posts_desc.reverse()
        return {
            "thread": {
                "id": thread.id,
                "run_id": thread.run_id,
                "title": thread.title,
                "status": thread.status,
                "current_sequence": thread.current_sequence,
                "summary": thread.summary,
            },
            "posts": [
                {
                    "id": post.id,
                    "parent_post_id": post.parent_post_id,
                    "author_type": post.author_type,
                    "author_agent_id": post.author_agent_id,
                    "author_handle": post.author_handle,
                    "body": post.body,
                    "sequence": post.sequence,
                    "intent": post.intent,
                    "created_at": post.created_at.isoformat(),
                }
                for post in posts_desc
            ],
            "captured_at": utc_now().isoformat(),
        }

    # ----------------------------------------------------------------- stimuli
    def add_stimulus(
        self,
        *,
        thread_id: str,
        kind: StimulusKind | str,
        run_id: str | None = None,
        triggering_event_id: int | None = None,
        source_post_id: str | None = None,
        target_agent_id: str | None = None,
        priority: float = 0.0,
        payload: Mapping[str, Any] | None = None,
        cascade_depth: int = 0,
        not_before: datetime | None = None,
        max_attempts: int = 2,
        dedupe_key: str | None = None,
    ) -> Stimulus:
        if dedupe_key is not None:
            existing = self.session.scalar(
                select(Stimulus).where(Stimulus.dedupe_key == dedupe_key)
            )
            if existing is not None:
                return existing
        thread = self.get_thread(thread_id)
        if run_id is None:
            run_id = thread.run_id
        elif thread.run_id is not None and run_id != thread.run_id:
            raise ValueError("stimulus run does not match thread run")
        if run_id is not None:
            run = self.get_run(run_id)
            if run.state in _TERMINAL_RUN_STATES:
                raise InvalidStateError(
                    f"run {run.id} is terminal ({run.state}); rerun or create a fresh thread"
                )
        if target_agent_id is not None:
            self.get_agent(target_agent_id)
        if source_post_id is not None and self.get_post(source_post_id).thread_id != thread_id:
            raise ValueError("stimulus source post belongs to a different thread")
        if triggering_event_id is not None:
            self.get_event(triggering_event_id)

        stimulus = Stimulus(
            run_id=run_id,
            thread_id=thread_id,
            triggering_event_id=triggering_event_id,
            source_post_id=source_post_id,
            target_agent_id=target_agent_id,
            kind=str(kind),
            priority=priority,
            payload=dict(payload or {}),
            cascade_depth=cascade_depth,
            not_before=not_before or utc_now(),
            max_attempts=max_attempts,
            dedupe_key=dedupe_key,
        )
        self.session.add(stimulus)
        self.session.flush()
        self.add_event(
            "stimulus.created",
            run_id=run_id,
            thread_id=thread_id,
            post_id=source_post_id,
            agent_id=target_agent_id,
            stimulus_id=stimulus.id,
            payload={
                "kind": stimulus.kind,
                "priority": priority,
                "cascade_depth": cascade_depth,
                "triggering_event_id": triggering_event_id,
            },
        )
        return stimulus

    def get_stimulus(self, stimulus_id: str) -> Stimulus:
        stimulus = self.session.get(Stimulus, stimulus_id)
        if stimulus is None:
            raise NotFoundError(f"stimulus {stimulus_id} was not found")
        return stimulus

    def list_stimuli(
        self,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        state: StimulusState | str | None = None,
        limit: int = 500,
    ) -> list[Stimulus]:
        stmt = select(Stimulus)
        if run_id is not None:
            stmt = stmt.where(Stimulus.run_id == run_id)
        if thread_id is not None:
            stmt = stmt.where(Stimulus.thread_id == thread_id)
        if state is not None:
            stmt = stmt.where(Stimulus.state == str(state))
        stmt = stmt.order_by(desc(Stimulus.priority), Stimulus.created_at).limit(limit)
        return list(self.session.scalars(stmt))

    def claim_stimuli(
        self,
        *,
        run_id: str | None = None,
        limit: int = 2,
        now: datetime | None = None,
    ) -> list[Stimulus]:
        """Claim ready work after the engine's audited lease-recovery pass."""

        claim_time = now or utc_now()
        stmt: Select[tuple[Stimulus]] = select(Stimulus).where(
            Stimulus.state == StimulusState.PENDING.value,
            Stimulus.not_before <= claim_time,
        )
        if run_id is not None:
            stmt = stmt.where(Stimulus.run_id == run_id)
        stmt = stmt.order_by(desc(Stimulus.priority), Stimulus.created_at, Stimulus.id).limit(limit)
        candidates = list(self.session.scalars(stmt))
        claimed: list[Stimulus] = []
        for candidate in candidates:
            token = new_uuid()
            won = self.session.execute(
                update(Stimulus)
                .where(
                    Stimulus.id == candidate.id,
                    Stimulus.state == StimulusState.PENDING.value,
                    Stimulus.not_before <= claim_time,
                )
                .values(
                    state=StimulusState.CLAIMED.value,
                    claim_token=token,
                    claimed_at=claim_time,
                    attempts=Stimulus.attempts + 1,
                    updated_at=claim_time,
                )
            ).rowcount
            if not won:
                continue
            self.session.flush()
            self.session.refresh(candidate)
            self.add_event(
                "stimulus.claimed",
                run_id=candidate.run_id,
                thread_id=candidate.thread_id,
                post_id=candidate.source_post_id,
                agent_id=candidate.target_agent_id,
                stimulus_id=candidate.id,
                payload={"attempt": candidate.attempts, "claim_token": token},
            )
            claimed.append(candidate)
        return claimed

    def mark_stimulus_processing(self, stimulus_id: str, claim_token: str) -> Stimulus:
        stimulus = self._claimed_stimulus(stimulus_id, claim_token)
        stimulus.state = StimulusState.PROCESSING.value
        self.session.flush()
        self.add_event(
            "stimulus.processing",
            run_id=stimulus.run_id,
            thread_id=stimulus.thread_id,
            post_id=stimulus.source_post_id,
            agent_id=stimulus.target_agent_id,
            stimulus_id=stimulus.id,
            payload={"attempt": stimulus.attempts},
        )
        return stimulus

    def complete_stimulus(
        self, stimulus_id: str, *, claim_token: str | None = None
    ) -> Stimulus:
        stimulus = self.get_stimulus(stimulus_id)
        if stimulus.state == StimulusState.COMPLETED.value:
            return stimulus
        if claim_token is not None and stimulus.claim_token != claim_token:
            raise ClaimConflictError("stimulus claim token does not match")
        if stimulus.state not in {
            StimulusState.CLAIMED.value,
            StimulusState.PROCESSING.value,
        }:
            raise InvalidStateError(f"cannot complete a {stimulus.state} stimulus")
        stimulus.state = StimulusState.COMPLETED.value
        stimulus.completed_at = utc_now()
        stimulus.claim_token = None
        self.session.flush()
        self.add_event(
            "stimulus.completed",
            run_id=stimulus.run_id,
            thread_id=stimulus.thread_id,
            post_id=stimulus.source_post_id,
            agent_id=stimulus.target_agent_id,
            stimulus_id=stimulus.id,
            payload={"attempts": stimulus.attempts},
        )
        return stimulus

    def release_stimulus(
        self,
        stimulus_id: str,
        claim_token: str,
        *,
        error: str,
        retry_delay_seconds: float = 0.0,
    ) -> Stimulus:
        stimulus = self._claimed_stimulus(stimulus_id, claim_token)
        now = utc_now()
        stimulus.last_error = error[:10_000]
        stimulus.claim_token = None
        stimulus.claimed_at = None
        if stimulus.attempts >= stimulus.max_attempts:
            stimulus.state = StimulusState.FAILED.value
            stimulus.completed_at = now
            event_type = "stimulus.failed"
        else:
            stimulus.state = StimulusState.PENDING.value
            stimulus.not_before = now + timedelta(seconds=max(0.0, retry_delay_seconds))
            event_type = "stimulus.requeued"
        self.session.flush()
        self.add_event(
            event_type,
            run_id=stimulus.run_id,
            thread_id=stimulus.thread_id,
            post_id=stimulus.source_post_id,
            agent_id=stimulus.target_agent_id,
            stimulus_id=stimulus.id,
            payload={"attempts": stimulus.attempts, "error": stimulus.last_error},
        )
        return stimulus

    def _claimed_stimulus(self, stimulus_id: str, claim_token: str) -> Stimulus:
        stimulus = self.get_stimulus(stimulus_id)
        if stimulus.claim_token != claim_token:
            raise ClaimConflictError("stimulus claim token does not match")
        if stimulus.state not in {
            StimulusState.CLAIMED.value,
            StimulusState.PROCESSING.value,
        }:
            raise InvalidStateError(f"stimulus is {stimulus.state}, not claimed")
        return stimulus

    # -------------------------------------------------------------------- turns
    def create_turn(
        self,
        *,
        thread_id: str,
        agent_id: str,
        run_id: str | None = None,
        stimulus_id: str | None = None,
        triggering_event_id: int | None = None,
        context_post_ids: Sequence[str] = (),
        context_snapshot: Mapping[str, Any] | None = None,
        scheduler_scores: Mapping[str, Any] | None = None,
        selection_reason: str | None = None,
        prompt: str | None = None,
        prompt_version: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        sampling_settings: Mapping[str, Any] | None = None,
        seed: int | None = None,
        retrieved_memory_ids: Sequence[str] = (),
        idempotency_key: str | None = None,
        claim_token: str | None = None,
    ) -> Turn:
        thread = self.get_thread(thread_id)
        self.get_agent(agent_id)
        if run_id is None:
            run_id = thread.run_id
        if stimulus_id is not None:
            existing = self.session.scalar(
                select(Turn)
                .where(
                    Turn.stimulus_id == stimulus_id,
                    Turn.agent_id == agent_id,
                    Turn.state.in_(
                        [
                            TurnState.SELECTED.value,
                            TurnState.CALLING.value,
                            TurnState.COMPLETED.value,
                            TurnState.PASSED.value,
                        ]
                    ),
                )
                .order_by(desc(Turn.started_at))
            )
            if existing is not None:
                return existing
        if idempotency_key is not None:
            existing = self.session.scalar(
                select(Turn).where(Turn.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return existing
        try:
            turn_config = normalize_config(self.get_run(run_id).config if run_id else None)
        except ValueError as exc:
            raise InvalidStateError(str(exc)) from None
        turn = Turn(
            run_id=run_id,
            thread_id=thread_id,
            agent_id=agent_id,
            stimulus_id=stimulus_id,
            triggering_event_id=triggering_event_id,
            idempotency_key=idempotency_key or new_uuid(),
            claim_token=claim_token,
            session_type=turn_config["session_type"],
            policy_snapshot=deepcopy(turn_config["policy"]),
            context_post_ids=list(context_post_ids),
            context_snapshot=dict(context_snapshot or {}),
            scheduler_scores=dict(scheduler_scores or {}),
            selection_reason=selection_reason,
            prompt=prompt,
            prompt_version=prompt_version,
            provider=provider,
            model=model,
            sampling_settings=dict(sampling_settings or {}),
            seed=seed,
            retrieved_memory_ids=list(retrieved_memory_ids),
        )
        self.session.add(turn)
        self.session.flush()
        self.add_event(
            "turn.selected",
            run_id=run_id,
            thread_id=thread_id,
            agent_id=agent_id,
            stimulus_id=stimulus_id,
            payload={
                "turn_id": turn.id,
                "context_post_ids": list(context_post_ids),
                "scheduler_scores": dict(scheduler_scores or {}),
                "selection_reason": selection_reason,
                "session_type": turn.session_type,
                "policy_snapshot": deepcopy(turn.policy_snapshot),
                "outcome": turn.outcome,
                "rejection_reason": turn.rejection_reason,
            },
        )
        return turn

    def get_turn(self, turn_id: str) -> Turn:
        turn = self.session.get(Turn, turn_id)
        if turn is None:
            raise NotFoundError(f"turn {turn_id} was not found")
        return turn

    def list_turns(
        self,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        limit: int = 500,
    ) -> list[Turn]:
        stmt = select(Turn)
        if run_id is not None:
            stmt = stmt.where(Turn.run_id == run_id)
        if thread_id is not None:
            stmt = stmt.where(Turn.thread_id == thread_id)
        return list(self.session.scalars(stmt.order_by(desc(Turn.started_at)).limit(limit)))

    def mark_turn_calling(self, turn_id: str) -> Turn:
        turn = self.get_turn(turn_id)
        if turn.state == TurnState.CALLING.value:
            return turn
        if turn.state != TurnState.SELECTED.value:
            raise InvalidStateError(f"cannot call model for a {turn.state} turn")
        turn.state = TurnState.CALLING.value
        self.session.flush()
        self.add_event(
            "turn.calling",
            run_id=turn.run_id,
            thread_id=turn.thread_id,
            agent_id=turn.agent_id,
            stimulus_id=turn.stimulus_id,
            payload={"turn_id": turn.id},
        )
        return turn

    def append_turn_retry(self, turn_id: str, retry: Mapping[str, Any]) -> Turn:
        turn = self.get_turn(turn_id)
        turn.retry_history = [*turn.retry_history, dict(retry)]
        self.session.flush()
        return turn

    def finish_turn(
        self,
        turn_id: str,
        *,
        state: TurnState | str,
        resulting_post_id: str | None = None,
        raw_output: Any | None = None,
        parsed_action: Mapping[str, Any] | None = None,
        validated_action: Mapping[str, Any] | None = None,
        error: str | None = None,
        latency_ms: int | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        total_tokens: int | None = None,
        outcome: TurnOutcome | str | None = None,
        rejection_reason: str | None = None,
    ) -> Turn:
        turn = self.get_turn(turn_id)
        new_state = str(state)
        if new_state not in {
            TurnState.COMPLETED.value,
            TurnState.PASSED.value,
            TurnState.FAILED.value,
        }:
            raise ValueError("finish_turn requires completed, passed, or failed state")
        terminal_outcome = str(outcome) if outcome is not None else infer_turn_outcome(new_state, error)
        if terminal_outcome not in {item.value for item in TurnOutcome}:
            raise ValueError("unknown turn outcome")
        if turn.state in {
            TurnState.COMPLETED.value,
            TurnState.PASSED.value,
            TurnState.FAILED.value,
        }:
            return turn
        if resulting_post_id is not None:
            self.get_post(resulting_post_id)
        turn.state = new_state
        turn.resulting_post_id = resulting_post_id
        turn.raw_output = raw_output
        turn.parsed_action = dict(parsed_action) if parsed_action is not None else None
        turn.validated_action = dict(validated_action) if validated_action is not None else None
        turn.error = error[:10_000] if error else None
        turn.outcome = terminal_outcome
        turn.rejection_reason = (
            rejection_reason if rejection_reason is not None else (
                turn.error if terminal_outcome not in {"executed", "passed"} else None
            )
        )
        turn.latency_ms = latency_ms
        turn.input_tokens = input_tokens
        turn.output_tokens = output_tokens
        turn.total_tokens = total_tokens if total_tokens is not None else input_tokens + output_tokens
        turn.completed_at = utc_now()
        self.session.flush()
        self.add_event(
            f"turn.{new_state}",
            run_id=turn.run_id,
            thread_id=turn.thread_id,
            post_id=resulting_post_id,
            agent_id=turn.agent_id,
            stimulus_id=turn.stimulus_id,
            payload={
                "turn_id": turn.id,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": turn.total_tokens,
                "error": turn.error,
                "outcome": turn.outcome,
                "rejection_reason": turn.rejection_reason,
                "session_type": turn.session_type,
                "policy_snapshot": deepcopy(turn.policy_snapshot),
            },
        )
        if turn.run_id is not None:
            self.increment_run_counters(
                turn.run_id,
                rounds=1,
                tokens=turn.total_tokens,
                model_calls=1,
            )
        return turn

    # ------------------------------------------------------------------ memory
    def create_memory(
        self,
        *,
        claim: str,
        run_id: str | None = None,
        thread_id: str | None = None,
        agent_id: str | None = None,
        source_post_ids: Sequence[str] = (),
        tags: Sequence[str] = (),
        confidence: float = 1.0,
    ) -> Memory:
        memory = Memory(
            claim=claim.strip(),
            run_id=run_id,
            thread_id=thread_id,
            agent_id=agent_id,
            source_post_ids=list(source_post_ids),
            tags=list(tags),
            confidence=confidence,
        )
        self.session.add(memory)
        self.session.flush()
        self.add_event(
            "memory.created",
            run_id=run_id,
            thread_id=thread_id,
            agent_id=agent_id,
            payload={"memory_id": memory.id, "source_post_ids": list(source_post_ids)},
        )
        return memory

    def list_memories(
        self,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        agent_id: str | None = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> list[Memory]:
        stmt = select(Memory)
        if run_id is not None:
            stmt = stmt.where(or_(Memory.run_id == run_id, Memory.run_id.is_(None)))
        if thread_id is not None:
            stmt = stmt.where(or_(Memory.thread_id == thread_id, Memory.thread_id.is_(None)))
        if agent_id is not None:
            stmt = stmt.where(or_(Memory.agent_id == agent_id, Memory.agent_id.is_(None)))
        if active_only:
            stmt = stmt.where(Memory.active.is_(True))
        return list(self.session.scalars(stmt.order_by(desc(Memory.updated_at)).limit(limit)))

    # -------------------------------------------------------------- crash/retry
    def recover_inflight_work(
        self,
        *,
        stale_before: datetime | None = None,
        run_id: str | None = None,
    ) -> RecoveryResult:
        """Requeue stale leases and close orphaned model calls after a crash.

        Pending stimuli in nonterminal runs remain durable for the scheduler;
        leftover work in terminal runs is cancelled before lease recovery.
        When ``run_id`` is
        supplied, recovery is isolated to that run; turns are interrupted only
        when their associated stimulus lease is stale.
        """

        self.cleanup_terminal_run_work(run_id=run_id)
        cutoff = stale_before or (utc_now() - timedelta(minutes=2))
        now = utc_now()
        stimuli_stmt = select(Stimulus).where(
            Stimulus.state.in_(
                [StimulusState.CLAIMED.value, StimulusState.PROCESSING.value]
            ),
            Stimulus.claimed_at.is_not(None),
            # ``updated_at`` is the lease activity boundary.  A worker may
            # refresh or move a claim to processing without changing claimed_at.
            Stimulus.updated_at <= cutoff,
        )
        if run_id is not None:
            stimuli_stmt = stimuli_stmt.where(Stimulus.run_id == run_id)
        stimuli = list(self.session.scalars(stimuli_stmt))
        requeued = 0
        failed = 0
        stale_ids: set[str] = set()
        for stimulus in stimuli:
            stale_ids.add(stimulus.id)
            stimulus.claim_token = None
            stimulus.claimed_at = None
            stimulus.last_error = "recovered after interrupted worker"
            if stimulus.attempts >= stimulus.max_attempts:
                stimulus.state = StimulusState.FAILED.value
                stimulus.completed_at = now
                event_type = "stimulus.failed"
                failed += 1
            else:
                stimulus.state = StimulusState.PENDING.value
                stimulus.not_before = now
                event_type = "stimulus.recovered"
                requeued += 1
            self.add_event(
                event_type,
                run_id=stimulus.run_id,
                thread_id=stimulus.thread_id,
                post_id=stimulus.source_post_id,
                agent_id=stimulus.target_agent_id,
                stimulus_id=stimulus.id,
                payload={"attempts": stimulus.attempts, "reason": stimulus.last_error},
            )

        # A turn belongs to the stimulus lease.  Its start time alone cannot
        # make it stale: a long-running call may still have a freshly updated
        # claim.  Only turns linked to leases recovered above are interrupted.
        unfinished_turns: list[Turn] = []
        if stale_ids:
            turns_stmt = select(Turn).where(
                Turn.state.in_([TurnState.SELECTED.value, TurnState.CALLING.value]),
                Turn.stimulus_id.in_(stale_ids),
            )
            if run_id is not None:
                turns_stmt = turns_stmt.where(Turn.run_id == run_id)
            unfinished_turns = list(self.session.scalars(turns_stmt))
        turns_failed = 0
        for turn in unfinished_turns:
            # If its stimulus was requeued, a new process will create or reuse this
            # record; marking the interrupted call failed preserves exact history.
            turn.state = TurnState.FAILED.value
            turn.error = "model call interrupted by process restart"
            turn.outcome = TurnOutcome.PROVIDER_FAILURE.value
            turn.rejection_reason = turn.error
            turn.completed_at = now
            turns_failed += 1
            self.add_event(
                "turn.recovered_failed",
                run_id=turn.run_id,
                thread_id=turn.thread_id,
                agent_id=turn.agent_id,
                stimulus_id=turn.stimulus_id,
                payload={
                    "turn_id": turn.id, "stimulus_recovered": turn.stimulus_id in stale_ids,
                    "outcome": turn.outcome, "rejection_reason": turn.rejection_reason,
                    "session_type": turn.session_type,
                    "policy_snapshot": deepcopy(turn.policy_snapshot),
                },
            )
        self.session.flush()
        return RecoveryResult(requeued, failed, turns_failed)


__all__ = [
    "ClaimConflictError",
    "IdempotencyConflictError",
    "InvalidStateError",
    "NotFoundError",
    "PostWriteResult",
    "RecoveryResult",
    "Repository",
    "RepositoryError",
]
