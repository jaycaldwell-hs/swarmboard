"""Pydantic request/response contracts for the API and scheduler boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import DEFAULT_RUN_MAX_TOKENS
from .credentials import validate_agent_settings
from .persona_context import persona_snapshot
from .models import (
    AuthorType,
    RunState,
    StimulusKind,
    StimulusState,
    ThreadStatus,
    TurnState,
)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class AgentCreate(InputModel):
    handle: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    persona: str = Field(min_length=1, max_length=20_000)
    role: str = Field(default="specialist", min_length=1, max_length=80)
    provider: str = Field(default="ollama", min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=255)
    settings: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, Any] = Field(default_factory=dict)
    cooldown_seconds: int = Field(default=15, ge=0, le=86_400)
    enabled: bool = True

    @field_validator("settings")
    @classmethod
    def settings_must_reference_environment_credentials(
        cls, value: dict[str, Any]
    ) -> dict[str, Any]:
        value = validate_agent_settings(value)
        persona_snapshot(value)
        return value


class AgentUpdate(InputModel):
    persona: str | None = Field(default=None, min_length=1, max_length=20_000)
    role: str | None = Field(default=None, min_length=1, max_length=80)
    provider: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, min_length=1, max_length=255)
    settings: dict[str, Any] | None = None
    permissions: dict[str, Any] | None = None
    cooldown_seconds: int | None = Field(default=None, ge=0, le=86_400)
    enabled: bool | None = None

    @field_validator("settings")
    @classmethod
    def settings_must_reference_environment_credentials(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        value = validate_agent_settings(value)
        persona_snapshot(value)
        return value


class AgentRead(ORMModel):
    id: str
    handle: str
    persona: str
    role: str
    provider: str
    model: str
    settings: dict[str, Any]
    permissions: dict[str, Any]
    cooldown_seconds: int
    enabled: bool
    last_spoke_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RunCreate(InputModel):
    seed: int = 0
    continuous: bool = False
    config: dict[str, Any] = Field(default_factory=dict)
    max_rounds: int = Field(default=50, gt=0)
    max_posts: int = Field(default=200, gt=0)
    max_tokens: int = Field(default=DEFAULT_RUN_MAX_TOKENS, gt=0)
    max_duration_seconds: int = Field(default=3_600, gt=0)
    per_agent_quota: int = Field(default=50, gt=0)
    per_thread_quota: int = Field(default=100, gt=0)
    max_cascade_depth: int = Field(default=8, ge=0)


class RunRead(ORMModel):
    id: str
    state: RunState
    seed: int
    continuous: bool
    config: dict[str, Any]
    max_rounds: int
    max_posts: int
    max_tokens: int
    max_duration_seconds: int
    per_agent_quota: int
    per_thread_quota: int
    max_cascade_depth: int
    rounds_used: int
    posts_used: int
    tokens_used: int
    model_calls: int
    virtual_time: float
    started_at: datetime | None
    paused_at: datetime | None
    stopped_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    stop_reason: str | None
    created_at: datetime
    updated_at: datetime


class RunControlRequest(InputModel):
    action: Literal["start", "pause", "step", "resume", "stop", "emergency_stop"]
    reason: str | None = Field(default=None, max_length=2_000)


class ThreadCreate(InputModel):
    title: str = Field(min_length=1, max_length=300)
    run_id: str | None = None
    summary: str | None = Field(default=None, max_length=100_000)


class ThreadRead(ORMModel):
    id: str
    run_id: str | None
    title: str
    status: ThreadStatus
    current_sequence: int
    summary: str | None
    latest_activity_at: datetime
    dormant_at: datetime | None
    closed_at: datetime | None
    wake_reason: str | None
    created_at: datetime
    updated_at: datetime


class HumanPostCreate(InputModel):
    body: str = Field(min_length=1, max_length=100_000)
    parent_post_id: str | None = None
    author_handle: str = Field(default="human", min_length=1, max_length=80)
    idempotency_key: str | None = Field(default=None, max_length=255)


class PostCreate(HumanPostCreate):
    author_type: AuthorType = AuthorType.HUMAN
    author_agent_id: str | None = None
    intent: str | None = Field(default=None, max_length=32)
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class PostRead(ORMModel):
    id: str
    thread_id: str
    parent_post_id: str | None
    author_type: AuthorType
    author_agent_id: str | None
    author_handle: str
    body: str
    sequence: int
    intent: str | None
    idempotency_key: str | None
    metadata_json: dict[str, Any]
    created_at: datetime


class ThreadDetail(ThreadRead):
    posts: list[PostRead] = Field(default_factory=list)


class EventRead(ORMModel):
    id: int
    uuid: str
    event_type: str
    run_id: str | None
    thread_id: str | None
    post_id: str | None
    agent_id: str | None
    stimulus_id: str | None
    actor_type: str
    actor_id: str | None
    payload: dict[str, Any]
    created_at: datetime


class StimulusCreate(InputModel):
    thread_id: str
    kind: StimulusKind
    run_id: str | None = None
    triggering_event_id: int | None = None
    source_post_id: str | None = None
    target_agent_id: str | None = None
    priority: float = 0.0
    payload: dict[str, Any] = Field(default_factory=dict)
    cascade_depth: int = Field(default=0, ge=0)
    not_before: datetime | None = None
    max_attempts: int = Field(default=2, gt=0, le=20)
    dedupe_key: str | None = Field(default=None, max_length=255)


class StimulusRead(ORMModel):
    id: str
    run_id: str | None
    thread_id: str
    triggering_event_id: int | None
    source_post_id: str | None
    target_agent_id: str | None
    kind: StimulusKind
    state: StimulusState
    priority: float
    payload: dict[str, Any]
    cascade_depth: int
    not_before: datetime
    attempts: int
    max_attempts: int
    dedupe_key: str | None
    claim_token: str | None
    claimed_at: datetime | None
    completed_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


class TurnRead(ORMModel):
    id: str
    run_id: str | None
    thread_id: str
    agent_id: str
    stimulus_id: str | None
    triggering_event_id: int | None
    resulting_post_id: str | None
    state: TurnState
    idempotency_key: str
    claim_token: str | None
    context_post_ids: list[str]
    context_snapshot: dict[str, Any]
    scheduler_scores: dict[str, Any]
    selection_reason: str | None
    prompt: str | None
    prompt_version: str | None
    provider: str | None
    model: str | None
    sampling_settings: dict[str, Any]
    seed: int | None
    retrieved_memory_ids: list[str]
    raw_output: Any | None
    parsed_action: dict[str, Any] | None
    validated_action: dict[str, Any] | None
    retry_history: list[dict[str, Any]]
    error: str | None
    started_at: datetime
    completed_at: datetime | None
    latency_ms: int | None
    input_tokens: int
    output_tokens: int
    total_tokens: int


class MemoryCreate(InputModel):
    claim: str = Field(min_length=1, max_length=100_000)
    run_id: str | None = None
    thread_id: str | None = None
    agent_id: str | None = None
    source_post_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0, le=1)


class MemoryRead(ORMModel):
    id: str
    run_id: str | None
    thread_id: str | None
    agent_id: str | None
    claim: str
    source_post_ids: list[str]
    tags: list[str]
    confidence: float
    active: bool
    created_at: datetime
    updated_at: datetime


ActionName = Literal["reply", "new_thread", "pass", "propose_close"]
IntentName = Literal["challenge", "clarify", "support", "synthesize"]


class AgentAction(InputModel):
    """Validated model decision; reasoning text is intentionally not accepted."""

    action: ActionName
    parent_post_id: str | None = None
    title: str | None = Field(default=None, max_length=300)
    body: str | None = Field(default=None, max_length=100_000)
    intent: IntentName | None = None

    @model_validator(mode="after")
    def validate_fields_for_action(self) -> AgentAction:
        if self.action == "pass":
            if any(
                value is not None
                for value in (self.body, self.title, self.parent_post_id, self.intent)
            ):
                raise ValueError("pass cannot contain body, title, parent_post_id, or intent")
            return self
        if not self.body:
            raise ValueError(f"{self.action} requires body")
        if self.intent is None:
            raise ValueError(f"{self.action} requires intent")
        if self.action == "new_thread":
            if not self.title:
                raise ValueError("new_thread requires title")
            if self.parent_post_id is not None:
                raise ValueError("new_thread cannot have parent_post_id")
        elif self.title is not None:
            raise ValueError(f"{self.action} cannot contain title")
        return self


__all__ = [name for name in globals() if not name.startswith("_")]
