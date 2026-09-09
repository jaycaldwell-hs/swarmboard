"""SQLAlchemy models for Swarmboard's durable event-driven state.

The database deliberately stores enum values as ordinary strings.  That keeps
the SQLite file inspectable and lets newer application versions add states
without requiring a table rebuild.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .config import DEFAULT_RUN_MAX_TOKENS


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid4())


def normalize_agent_handle(value: str) -> str:
    """Return the canonical identity used by mentions and persistence."""

    normalized = value.strip().lstrip("@").casefold()
    if not normalized:
        raise ValueError("agent handle cannot be empty")
    if len(normalized) > 80:
        raise ValueError("agent handle cannot exceed 80 characters after normalization")
    return normalized


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC in SQLite and always return timezone-aware values."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ThreadStatus(StringEnum):
    ACTIVE = "active"
    DORMANT = "dormant"
    CLOSED = "closed"


class AuthorType(StringEnum):
    HUMAN = "human"
    AGENT = "agent"
    SYSTEM = "system"


class StimulusState(StringEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StimulusKind(StringEnum):
    HUMAN_POST = "human_post"
    AGENT_POST = "agent_post"
    MENTION = "mention"
    UNANSWERED_QUESTION = "unanswered_question"
    IDLE_REVISIT = "idle_revisit"
    NEW_EVIDENCE = "new_evidence"
    MANUAL_STEP = "manual_step"
    RECOVERY = "recovery"


class TurnState(StringEnum):
    SELECTED = "selected"
    CALLING = "calling"
    COMPLETED = "completed"
    PASSED = "passed"
    FAILED = "failed"


class RunState(StringEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    EMERGENCY_STOPPED = "emergency_stopped"
    FAILED = "failed"


class UUIDMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, onupdate=utc_now, nullable=False
    )


class Agent(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "agents"

    handle: Mapped[str] = mapped_column(String(80, collation="NOCASE"), nullable=False)
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(80), nullable=False, default="specialist")
    provider: Mapped[str] = mapped_column(String(80), nullable=False, default="ollama")
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    permissions: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    last_spoke_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    posts: Mapped[list[Post]] = relationship(back_populates="author_agent")
    turns: Mapped[list[Turn]] = relationship(back_populates="agent")

    __table_args__ = (
        CheckConstraint("cooldown_seconds >= 0", name="ck_agents_cooldown_nonnegative"),
        Index("uq_agents_handle_nocase", "handle", unique=True),
    )


class Run(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "runs"

    state: Mapped[str] = mapped_column(
        String(32), default=RunState.CREATED.value, nullable=False, index=True
    )
    seed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    continuous: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )

    max_rounds: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    max_posts: Mapped[int] = mapped_column(Integer, default=200, nullable=False)
    max_tokens: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_RUN_MAX_TOKENS, nullable=False
    )
    max_duration_seconds: Mapped[int] = mapped_column(Integer, default=3_600, nullable=False)
    per_agent_quota: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    per_thread_quota: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    max_cascade_depth: Mapped[int] = mapped_column(Integer, default=8, nullable=False)

    rounds_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    posts_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    model_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    virtual_time: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    threads: Mapped[list[Thread]] = relationship(back_populates="run")
    stimuli: Mapped[list[Stimulus]] = relationship(back_populates="run")
    turns: Mapped[list[Turn]] = relationship(back_populates="run")

    __table_args__ = (
        CheckConstraint("max_rounds > 0", name="ck_runs_max_rounds_positive"),
        CheckConstraint("max_posts > 0", name="ck_runs_max_posts_positive"),
        CheckConstraint("max_tokens > 0", name="ck_runs_max_tokens_positive"),
        CheckConstraint("max_duration_seconds > 0", name="ck_runs_duration_positive"),
        CheckConstraint("max_cascade_depth >= 0", name="ck_runs_cascade_nonnegative"),
    )


class Thread(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "threads"

    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=ThreadStatus.ACTIVE.value, nullable=False, index=True
    )
    current_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    latest_activity_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    dormant_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    wake_reason: Mapped[str | None] = mapped_column(String(80), nullable=True)

    run: Mapped[Run | None] = relationship(back_populates="threads")
    posts: Mapped[list[Post]] = relationship(
        back_populates="thread", cascade="all, delete-orphan", order_by="Post.sequence"
    )
    stimuli: Mapped[list[Stimulus]] = relationship(back_populates="thread")

    __table_args__ = (
        CheckConstraint("current_sequence >= 0", name="ck_threads_sequence_nonnegative"),
        Index("ix_threads_run_status_activity", "run_id", "status", "latest_activity_at"),
    )


class Post(UUIDMixin, Base):
    __tablename__ = "posts"

    thread_id: Mapped[str] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_post_id: Mapped[str | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_type: Mapped[str] = mapped_column(String(16), nullable=False)
    author_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    author_handle: Mapped[str] = mapped_column(String(80), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    thread: Mapped[Thread] = relationship(back_populates="posts")
    parent: Mapped[Post | None] = relationship(remote_side="Post.id", back_populates="children")
    children: Mapped[list[Post]] = relationship(back_populates="parent")
    author_agent: Mapped[Agent | None] = relationship(back_populates="posts")

    __table_args__ = (
        UniqueConstraint("thread_id", "sequence", name="uq_posts_thread_sequence"),
        # A delivery key identifies one logical post, including actions which
        # create a brand-new thread.  Scoping this constraint to ``thread_id``
        # would let a redelivered new-thread action mint the same post twice.
        # SQLite permits multiple NULL values in a unique index, so posts which
        # do not participate in idempotency remain unrestricted.
        Index("uq_posts_idempotency_key", "idempotency_key", unique=True),
        CheckConstraint("sequence > 0", name="ck_posts_sequence_positive"),
        CheckConstraint("length(trim(body)) > 0", name="ck_posts_body_nonempty"),
        Index("ix_posts_thread_created", "thread_id", "created_at"),
    )


class Event(Base):
    """Append-only audit record.  Application code never updates event rows."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uuid: Mapped[str] = mapped_column(String(36), default=new_uuid, unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("threads.id", ondelete="SET NULL"), nullable=True, index=True
    )
    post_id: Mapped[str | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), nullable=True, index=True
    )
    stimulus_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    actor_type: Mapped[str] = mapped_column(String(24), default="system", nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

    __table_args__ = (
        Index("ix_events_thread_id_id", "thread_id", "id"),
        Index("ix_events_run_id_id", "run_id", "id"),
    )


class Stimulus(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "stimuli"

    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    triggering_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_post_id: Mapped[str | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    target_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    state: Mapped[str] = mapped_column(
        String(24), default=StimulusState.PENDING.value, nullable=False, index=True
    )
    priority: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    cascade_depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    not_before: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run | None] = relationship(back_populates="stimuli")
    thread: Mapped[Thread] = relationship(back_populates="stimuli")
    turns: Mapped[list[Turn]] = relationship(back_populates="stimulus")

    __table_args__ = (
        CheckConstraint("cascade_depth >= 0", name="ck_stimuli_cascade_nonnegative"),
        CheckConstraint("attempts >= 0", name="ck_stimuli_attempts_nonnegative"),
        CheckConstraint("max_attempts > 0", name="ck_stimuli_max_attempts_positive"),
        Index("ix_stimuli_ready", "state", "not_before", "priority", "created_at"),
        Index("ix_stimuli_run_state", "run_id", "state"),
    )


class Turn(UUIDMixin, Base):
    __tablename__ = "turns"

    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    stimulus_id: Mapped[str | None] = mapped_column(
        ForeignKey("stimuli.id", ondelete="SET NULL"), nullable=True, index=True
    )
    triggering_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resulting_post_id: Mapped[str | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(24), default=TurnState.SELECTED.value, nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), default=new_uuid, unique=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)

    context_post_ids: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    context_snapshot: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    scheduler_scores: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    selection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sampling_settings: Mapped[dict[str, Any]] = mapped_column(
        MutableDict.as_mutable(JSON), default=dict, nullable=False
    )
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieved_memory_ids: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    raw_output: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    parsed_action: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    validated_action: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    retry_history: Mapped[list[dict[str, Any]]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    run: Mapped[Run | None] = relationship(back_populates="turns")
    agent: Mapped[Agent] = relationship(back_populates="turns")
    stimulus: Mapped[Stimulus | None] = relationship(back_populates="turns")

    __table_args__ = (
        Index("ix_turns_stimulus_agent", "stimulus_id", "agent_id"),
        Index("ix_turns_thread_started", "thread_id", "started_at"),
    )


class Memory(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "memories"

    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=True, index=True
    )
    agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    source_post_ids: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSON), default=list, nullable=False
    )
    tags: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_memories_confidence"),
        Index("ix_memories_scope_active", "thread_id", "agent_id", "active"),
    )


__all__ = [
    "Agent",
    "AuthorType",
    "Base",
    "Event",
    "Memory",
    "Post",
    "Run",
    "RunState",
    "Stimulus",
    "StimulusKind",
    "StimulusState",
    "Thread",
    "ThreadStatus",
    "Turn",
    "TurnState",
    "UTCDateTime",
    "new_uuid",
    "normalize_agent_handle",
    "utc_now",
]


class Experiment(Base):
    """Frozen inputs and transactionally updated, entirely simulated world."""
    __tablename__ = "experiments"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    world: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class Scenario(Base):
    """Content-addressed scenario versions; edits create new versions."""
    __tablename__ = "scenarios"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
