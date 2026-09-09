from __future__ import annotations

import asyncio
import copy
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from . import sessions, autonomy, cadence
from .auth import AuthSettings, BasicAuthMiddleware, human_handle, request_key
from .config import DEFAULT_RUN_MAX_TOKENS, Settings
from .credentials import scrub_agent_settings, validate_hosted_provider
from .database import init_db, make_engine, make_session_factory
from .event_stream import EventBroker
from .models import (
    Agent,
    Event,
    Post,
    Run,
    RunState,
    Stimulus,
    StimulusKind,
    Thread,
    ThreadStatus,
    Turn,
    normalize_agent_handle,
    utc_now,
)
from .personas import DEFAULT_AGENTS
from .persona_context import load_persona
from .harness import codex_model_source, persona_spec
from .repository import InvalidStateError, NotFoundError, Repository, RepositoryError
from .schemas import AgentCreate, AgentUpdate, HumanPostCreate, SessionPolicyInput
from .stimuli import plan_reactive_stimuli


PACKAGE_DIR = Path(__file__).resolve().parent
REPLAY_EVENT_PAGE_SIZE = 10_000
EVENT_STREAM_PAGE_SIZE = 1_000
TERMINAL_RUN_STATES = {
    RunState.STOPPED.value,
    RunState.COMPLETED.value,
    RunState.EMERGENCY_STOPPED.value,
    RunState.FAILED.value,
}


class APIInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class NewThreadRequest(APIInput):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=100_000)
    author_handle: str = Field(default="human", min_length=1, max_length=80)
    idempotency_key: str | None = Field(default=None, max_length=255)


class RunLimits(APIInput):
    max_rounds: int = Field(default=24, gt=0, le=10_000)
    max_posts: int = Field(default=80, gt=0, le=100_000)
    max_tokens: int = Field(default=DEFAULT_RUN_MAX_TOKENS, gt=0)
    max_duration_seconds: int = Field(default=1_800, gt=0)
    per_agent_quota: int = Field(default=16, gt=0)
    per_thread_quota: int = Field(default=80, gt=0)
    max_cascade_depth: int = Field(default=6, ge=0, le=100)


class NewRunRequest(SessionPolicyInput):
    thread_id: str
    seed: int = 0
    continuous: bool = False
    limits: RunLimits = Field(default_factory=RunLimits)
    max_agents_per_stimulus: int = Field(default=2, ge=1, le=2)
    model_retries: int = Field(default=2, ge=0, le=5)
    agent_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)


class AdaSessionRequest(SessionPolicyInput):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=12_000)
    peer_ids: list[str] = Field(min_length=1, max_length=100)
    continuous: bool = True
    limits: RunLimits
    idempotency_key: str = Field(min_length=1, max_length=120)


class AgentPatch(AgentUpdate):
    handle: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
    )


class ThreadStatusRequest(APIInput):
    status: Literal["active", "dormant", "closed"]
    reason: str | None = Field(default=None, max_length=2_000)


class WakeRequest(APIInput):
    reason: Literal["scheduled_revisit", "new_evidence", "direct_mention"]
    evidence: str | None = Field(default=None, max_length=20_000)


def _post_json(post: Post) -> dict[str, Any]:
    return jsonable_encoder(
        {
            "id": post.id,
            "thread_id": post.thread_id,
            "parent_post_id": post.parent_post_id,
            "author_type": post.author_type,
            "author_id": post.author_agent_id,
            "author_agent_id": post.author_agent_id,
            "author_handle": post.author_handle,
            "body": post.body,
            "sequence": post.sequence,
            "intent": post.intent,
            "metadata": post.metadata_json,
            "created_at": post.created_at,
        }
    )


def _agent_json(agent: Agent) -> dict[str, Any]:
    safe_settings, _ = scrub_agent_settings(agent.settings)
    return jsonable_encoder(
        {
            "id": agent.id,
            "handle": agent.handle,
            "persona": agent.persona,
            "role": agent.role,
            "provider": agent.provider,
            "model": agent.model,
            "settings": safe_settings,
            "permissions": agent.permissions,
            "cooldown_seconds": agent.cooldown_seconds,
            "enabled": agent.enabled,
            "last_spoke_at": agent.last_spoke_at,
            "created_at": agent.created_at,
            "updated_at": agent.updated_at,
        }
    )


def _thread_json(
    thread: Thread,
    *,
    post_count: int | None = None,
    unanswered_count: int | None = None,
) -> dict[str, Any]:
    return jsonable_encoder(
        {
            "id": thread.id,
            "run_id": thread.run_id,
            "title": thread.title,
            "status": thread.status,
            "current_sequence": thread.current_sequence,
            "summary": thread.summary,
            "latest_activity_at": thread.latest_activity_at,
            "dormant_at": thread.dormant_at,
            "closed_at": thread.closed_at,
            "wake_reason": thread.wake_reason,
            "created_at": thread.created_at,
            "updated_at": thread.updated_at,
            "post_count": thread.current_sequence if post_count is None else post_count,
            "unanswered_count": unanswered_count or 0,
        }
    )


def _run_json(run: Run, *, thread_id: str | None = None) -> dict[str, Any]:
    limits = {
        "rounds": run.max_rounds,
        "posts": run.max_posts,
        "tokens": run.max_tokens,
        "duration_seconds": run.max_duration_seconds,
        "per_agent": run.per_agent_quota,
        "per_thread": run.per_thread_quota,
        "cascade_depth": run.max_cascade_depth,
    }
    counters = {
        "rounds": run.rounds_used,
        "posts": run.posts_used,
        "tokens": run.tokens_used,
        "model_calls": run.model_calls,
        "virtual_time": run.virtual_time,
    }
    return jsonable_encoder(
        {
            "id": run.id,
            "thread_id": thread_id,
            "state": run.state,
            "continuous": run.continuous,
            "seed": run.seed,
            "config": run.config,
            "session_type": run.config.get("session_type", "collaboration"),
            "policy": run.config.get("policy", {}),
            "limits": limits,
            "counters": counters,
            "max_rounds": run.max_rounds,
            "max_posts": run.max_posts,
            "max_tokens": run.max_tokens,
            "max_duration_seconds": run.max_duration_seconds,
            "per_agent_quota": run.per_agent_quota,
            "per_thread_quota": run.per_thread_quota,
            "max_cascade_depth": run.max_cascade_depth,
            "rounds_used": run.rounds_used,
            "posts_used": run.posts_used,
            "tokens_used": run.tokens_used,
            "model_calls": run.model_calls,
            "virtual_time": run.virtual_time,
            "started_at": run.started_at,
            "paused_at": run.paused_at,
            "stopped_at": run.stopped_at,
            "finished_at": run.finished_at,
            "heartbeat_at": run.heartbeat_at,
            "stop_reason": run.stop_reason,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        }
    )


def _run_activities(session: Session, runs: list[Run]) -> dict[str, dict[str, Any]]:
    """Describe actual work separately from the session's durable lifecycle."""
    activities = {
        run.id: {"state": run.state, "calling_agents": [], "pending_stimuli": 0, "next_ready_at": None}
        for run in runs
    }
    running_ids = [run.id for run in runs if run.state == RunState.RUNNING.value]
    if not running_ids:
        return activities
    for run_id, handle in session.execute(
        select(Turn.run_id, Agent.handle).join(Agent, Agent.id == Turn.agent_id)
        .where(Turn.run_id.in_(running_ids), Turn.state == "calling")
    ):
        activities[run_id]["calling_agents"].append(handle)
    queued = session.execute(
        select(Stimulus.run_id, func.count(Stimulus.id), func.min(Stimulus.not_before))
        .where(Stimulus.run_id.in_(running_ids), Stimulus.state.in_(["pending", "claimed", "processing"]))
        .group_by(Stimulus.run_id)
    )
    for run_id, count, ready_at in queued:
        activities[run_id].update(pending_stimuli=count, next_ready_at=ready_at)
    active_threads = set(session.scalars(
        select(Thread.run_id).where(Thread.run_id.in_(running_ids), Thread.status == ThreadStatus.ACTIVE.value)
    ))
    now = utc_now()
    for run_id in running_ids:
        activity = activities[run_id]
        if activity["calling_agents"]:
            activity["state"] = "thinking"
        elif activity["pending_stimuli"]:
            activity["state"] = "cooldown" if activity["next_ready_at"] and activity["next_ready_at"] > now else "queued"
        else:
            activity["state"] = "idle" if run_id in active_threads else "dormant"
    return jsonable_encoder(activities)


def _event_json(event: Event) -> dict[str, Any]:
    return jsonable_encoder(
        {
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
            "payload": event.payload,
            "created_at": event.created_at,
        }
    )


def _turn_json(turn: Turn) -> dict[str, Any]:
    return jsonable_encoder(
        {
            "id": turn.id,
            "run_id": turn.run_id,
            "thread_id": turn.thread_id,
            "agent_id": turn.agent_id,
            "stimulus_id": turn.stimulus_id,
            "triggering_event_id": turn.triggering_event_id,
            "resulting_post_id": turn.resulting_post_id,
            "state": turn.state,
            "session_type": turn.session_type,
            "policy_snapshot": turn.policy_snapshot,
            "outcome": turn.outcome,
            "rejection_reason": turn.rejection_reason,
            "idempotency_key": turn.idempotency_key,
            "claim_token": turn.claim_token,
            "context_post_ids": turn.context_post_ids,
            "context_snapshot": turn.context_snapshot,
            "scheduler_scores": turn.scheduler_scores,
            "selection_reason": turn.selection_reason,
            "prompt": turn.prompt,
            "prompt_version": turn.prompt_version,
            "provider": turn.provider,
            "model": turn.model,
            "sampling_settings": turn.sampling_settings,
            "seed": turn.seed,
            "retrieved_memory_ids": turn.retrieved_memory_ids,
            "raw_output": turn.raw_output,
            "parsed_action": turn.parsed_action,
            "validated_action": turn.validated_action,
            "retry_history": turn.retry_history,
            "error": turn.error,
            "started_at": turn.started_at,
            "completed_at": turn.completed_at,
            "latency_ms": turn.latency_ms,
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "total_tokens": turn.total_tokens,
        }
    )


def _unanswered_count(posts: list[Post]) -> int:
    replied_to = {post.parent_post_id for post in posts if post.parent_post_id}
    return sum("?" in post.body and post.id not in replied_to for post in posts)


def _latest_event_id(session: Session) -> int:
    return int(session.scalar(select(func.max(Event.id))) or 0)


def _all_run_events(repo: Repository, run_id: str) -> list[Event]:
    """Read a complete ordered event stream without a silent replay cap."""

    events: list[Event] = []
    after_id = 0
    while True:
        page = repo.list_events(
            after_id=after_id,
            run_id=run_id,
            limit=REPLAY_EVENT_PAGE_SIZE,
        )
        events.extend(page)
        if len(page) < REPLAY_EVENT_PAGE_SIZE:
            return events
        after_id = page[-1].id


def _add_post_stimuli(
    repo: Repository,
    *,
    run_id: str,
    thread_id: str,
    post: Post,
    triggering_event_id: int | None,
    default_kind: StimulusKind | str,
    default_priority: float,
    cascade_depth: int = 0,
) -> list[str]:
    run = repo.get_run(run_id)
    if cadence.enabled(run):
        stimulus = cadence.on_input(repo, run)
        return [stimulus.id] if stimulus else []
    if sessions.is_legacy_scripted(run):
        return []  # Retired scripted runs remain read-only.
    parent = repo.session.get(Post, post.parent_post_id) if post.parent_post_id else None
    plans = plan_reactive_stimuli(
        post.body,
        repo.list_agents(enabled_only=True, agent_ids=repo.get_run(run_id).config.get("agent_ids")),
        default_kind=default_kind,
        default_priority=default_priority,
        exclude_agent_id=post.author_agent_id,
        reply_to_agent_id=parent.author_agent_id if parent is not None else None,
    )
    stimuli = [
        repo.add_stimulus(
            run_id=run_id,
            thread_id=thread_id,
            kind=plan.kind,
            triggering_event_id=triggering_event_id,
            source_post_id=post.id,
            target_agent_id=plan.target_agent_id,
            priority=plan.priority,
            payload=plan.payload,
            cascade_depth=cascade_depth,
            dedupe_key=f"run:{run_id}:post:{post.id}:{plan.dedupe_label}",
        )
        for plan in plans
    ]
    return [stimulus.id for stimulus in stimuli]


def create_app(
    *,
    database_url: str | None = None,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    gateway: Any | None = None,
    recover_on_start: bool = True,
) -> FastAPI:
    settings = settings or Settings.from_env(database_url=database_url)
    auth_settings = AuthSettings.from_env()
    owned_engine: Engine | None = None
    if session_factory is None:
        owned_engine = make_engine(settings.database_url)
        session_factory = make_session_factory(owned_engine)
    factory = session_factory
    broker = EventBroker()

    async def publish(event: Event | dict[str, Any]) -> None:
        await broker.publish(_event_json(event) if isinstance(event, Event) else jsonable_encoder(event))

    async def audit_human_action(scope: dict[str, Any], response_status: int) -> None:
        route_path = getattr(scope.get("route"), "path", None)
        if route_path is None:
            return
        # Only the resolved route template and existing record IDs are captured.
        # Bodies, headers, query strings, and arbitrary path values stay out of
        # this audit. Each successful HTTP request has one separate event;
        # retries do not change the underlying operation's idempotency rules.
        params = scope.get("path_params", {})
        with factory.begin() as session:
            run = session.get(Run, params["run_id"]) if "run_id" in params else None
            thread = session.get(Thread, params["thread_id"]) if "thread_id" in params else None
            agent = session.get(Agent, params["agent_id"]) if "agent_id" in params else None
            event = Repository(session).add_event(
                "human.action", actor_type="human", actor_id=scope["state"]["authenticated_user"],
                run_id=run.id if run is not None else thread.run_id if thread is not None else None,
                thread_id=thread.id if thread is not None else None,
                agent_id=agent.id if agent is not None else None,
                payload={"method": scope["method"], "path": route_path, "status": response_status},
            )
            output = _event_json(event)
        await broker.publish(output)

    # Import here so test callers can build the schema and inject a gateway at
    # the single provider boundary without any production fake implementation.
    from .engine import EngineConfig, SwarmEngine

    swarm = SwarmEngine(
        session_factory=factory,
        gateway=gateway,
        publish=publish,
        config=EngineConfig(
            idle_seconds=settings.idle_seconds,
            dormant_seconds=settings.dormant_seconds,
            poll_seconds=settings.scheduler_poll_seconds,
            context_post_limit=settings.context_post_limit,
        ),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        bind = factory.kw.get("bind")
        if bind is None:
            raise RuntimeError("Swarmboard session factory must be bound to an engine")
        init_db(bind)
        with factory.begin() as session:
            repo = Repository(session)
            agents = repo.list_agents()
            for agent in agents:
                safe_settings, removed_fields = scrub_agent_settings(agent.settings)
                if not removed_fields:
                    continue
                agent.settings = safe_settings
                session.flush()
                repo.add_event(
                    "agent.credentials_scrubbed",
                    agent_id=agent.id,
                    actor_type="system",
                    payload={"removed_fields": removed_fields},
                )
            if not agents:
                for spec in DEFAULT_AGENTS:
                    provider = str(spec["provider"])
                    agent_settings = copy.deepcopy(spec["settings"])
                    agent_settings.setdefault("timeout_seconds", settings.model_timeout_seconds)
                    repo.create_agent(
                        handle=spec["handle"],
                        persona=spec["persona"],
                        role=spec["role"],
                        provider=provider,
                        model=str(spec["model"]),
                        settings=agent_settings,
                        permissions=spec["permissions"],
                        cooldown_seconds=15,
                    )
            for agent in repo.list_agents(enabled_only=True):
                try:
                    validate_hosted_provider(agent.provider, agent.settings)
                except ValueError:
                    agent.enabled = False
                    repo.add_event("agent.provider_disabled", agent_id=agent.id,
                        actor_type="system", payload={"fields": ["enabled"],
                            "reason": "provider configuration is no longer supported"})
        if recover_on_start:
            await swarm.recover()
        try:
            yield
        finally:
            await swarm.shutdown()
            if owned_engine is not None:
                owned_engine.dispose()

    app = FastAPI(
        title="Swarmboard",
        version="0.1.0",
        description="Local, bounded, event-driven multi-agent discussion board",
        lifespan=lifespan,
    )
    app.add_middleware(BasicAuthMiddleware, settings=auth_settings, audit=audit_human_action)
    app.state.settings = settings
    app.state.session_factory = factory
    app.state.engine = swarm
    app.state.broker = broker

    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(InvalidStateError)
    async def invalid_state_handler(_: Request, exc: InvalidStateError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(RepositoryError)
    async def repository_error_handler(_: Request, exc: RepositoryError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    async def publish_since(after_id: int) -> None:
        with factory() as session:
            events = Repository(session).list_events(after_id=after_id, limit=1_000)
            payloads = [_event_json(event) for event in events]
        for payload in payloads:
            await broker.publish(payload)

    @app.get("/", response_class=HTMLResponse)
    async def board(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="index.html", context={})

    @app.get("/sessions", response_class=HTMLResponse)
    async def session_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request=request, name="sessions.html", context={})

    @app.get("/experiments", include_in_schema=False)
    async def legacy_experiment_page(request: Request) -> RedirectResponse:
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(url=f"/sessions{query}", status_code=307)

    @app.api_route("/health", methods=["GET", "HEAD"], include_in_schema=False)
    async def health() -> dict[str, Any]:
        with factory() as session:
            session.execute(select(1)).scalar_one()
        return {"status": "ok"}

    @app.get("/api/state")
    async def state_snapshot(thread_id: str | None = None) -> dict[str, Any]:
        with factory() as session:
            repo = Repository(session)
            agents = repo.list_agents()
            threads = repo.list_threads(limit=200)
            selected = None
            selected_posts: list[Post] = []
            selected_thread: Thread | None = None
            if thread_id:
                selected_thread = repo.get_thread(thread_id)
            elif threads:
                selected_thread = threads[0]
            if selected_thread is not None and selected_thread.run_id and repo.get_run(selected_thread.run_id).config.get("discarded"):
                selected_thread = threads[0] if threads else None
            if selected_thread is not None:
                selected_posts = repo.list_posts(selected_thread.id, limit=1_000)
                selected = {
                    **_thread_json(
                        selected_thread,
                        post_count=len(selected_posts),
                        unanswered_count=_unanswered_count(selected_posts),
                    ),
                    "posts": [_post_json(post) for post in selected_posts],
                }

            thread_payloads: list[dict[str, Any]] = []
            for thread in threads:
                posts = selected_posts if selected_thread is not None and thread.id == selected_thread.id else []
                thread_payloads.append(
                    _thread_json(
                        thread,
                        post_count=thread.current_sequence,
                        unanswered_count=_unanswered_count(posts) if posts else 0,
                    )
                )

            runs = repo.list_runs(limit=100)
            thread_by_run = {thread.run_id: thread.id for thread in threads if thread.run_id}
            recent_events = list(
                session.scalars(select(Event).order_by(Event.id.desc()).limit(150))
            )
            recent_events.reverse()
            running = any(run.state == RunState.RUNNING.value for run in runs)
            activities = _run_activities(session, runs)
            return {
                "agents": [_agent_json(agent) for agent in agents],
                "threads": thread_payloads,
                "selected_thread": selected,
                "runs": [{**_run_json(run, thread_id=thread_by_run.get(run.id)), "activity": activities[run.id]} for run in runs],
                "events": [_event_json(event) for event in recent_events],
                "server": {
                    "scheduler_running": running,
                    "emergency_stopped": False,
                    "model_gateway": "live",
                },
            }

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: str) -> dict[str, Any]:
        with factory() as session:
            repo = Repository(session)
            thread = repo.get_thread(thread_id)
            posts = repo.list_posts(thread_id, limit=2_000)
            return {
                **_thread_json(thread, post_count=len(posts), unanswered_count=_unanswered_count(posts)),
                "posts": [_post_json(post) for post in posts],
            }

    @app.post("/api/threads", status_code=status.HTTP_201_CREATED)
    async def create_thread(payload: NewThreadRequest, request: Request) -> dict[str, Any]:
        with factory.begin() as session:
            before = _latest_event_id(session)
            repo = Repository(session)
            # The repository operation owns both the unique-key race and the
            # speculative thread rollback, keeping thread + post + event atomic.
            result = repo.create_human_thread(
                title=payload.title,
                body=payload.body,
                author_handle=ordinary_human_handle(repo, request, payload.author_handle),
                idempotency_key=request_key(request, payload.idempotency_key),
            )
            output = {
                "thread_id": result.post.thread_id,
                "post": _post_json(result.post),
                "created": result.created,
            }
        await publish_since(before)
        return output

    def ordinary_human_handle(repo: Repository, request: Request, fallback: str) -> str:
        handle = human_handle(request, fallback)
        if getattr(request.state, "authenticated_user", None) is not None:
            return handle
        # This ordinary human endpoint cannot create system authors or disguised
        # agent posts. Research impersonation requires its own attributed path.
        candidate = handle.lstrip("@").casefold()
        participant = repo.session.scalar(select(Agent.id).where(func.lower(Agent.handle) == candidate).limit(1))
        if candidate == "system" or participant is not None:
            raise HTTPException(422, "human posts cannot use a system or participant handle")
        return handle

    @app.post("/api/threads/{thread_id}/posts", status_code=status.HTTP_201_CREATED)
    async def create_post(thread_id: str, payload: HumanPostCreate, request: Request) -> dict[str, Any]:
        with factory.begin() as session:
            repo = Repository(session)
            before = _latest_event_id(session)
            result = repo.create_human_post(
                thread_id,
                payload.body,
                parent_post_id=payload.parent_post_id,
                author_handle=ordinary_human_handle(repo, request, payload.author_handle),
                idempotency_key=request_key(request, payload.idempotency_key),
            )
            thread = repo.get_thread(thread_id)
            stimulus_ids: list[str] = []
            if result.created and thread.run_id:
                run = repo.get_run(thread.run_id)
                if run.state not in TERMINAL_RUN_STATES:
                    stimulus_ids = _add_post_stimuli(
                        repo,
                        run_id=run.id,
                        thread_id=thread.id,
                        post=result.post,
                        triggering_event_id=result.event.id,
                        default_kind=StimulusKind.HUMAN_POST,
                        default_priority=5.0,
                    )
            output = {
                "post": _post_json(result.post),
                "created": result.created,
                "stimulus_ids": stimulus_ids,
            }
            run_to_wake = thread.run_id
        await publish_since(before)
        if run_to_wake:
            with factory() as session:
                run = session.get(Run, run_to_wake)
                should_wake = bool(run and run.state == RunState.RUNNING.value and run.continuous)
            if should_wake:
                swarm.notify(run_to_wake)
        return output

    @app.post("/api/threads/{thread_id}/status")
    async def set_thread_status(thread_id: str, payload: ThreadStatusRequest, request: Request) -> dict[str, Any]:
        with factory.begin() as session:
            before = _latest_event_id(session)
            thread = Repository(session).set_thread_status(
                thread_id,
                payload.status,
                reason=payload.reason,
                actor_type="human",
                actor_id=human_handle(request),
            )
            output = _thread_json(thread)
        await publish_since(before)
        return output

    @app.post("/api/threads/{thread_id}/wake")
    async def wake_thread(thread_id: str, payload: WakeRequest, request: Request) -> dict[str, Any]:
        with factory.begin() as session:
            repo = Repository(session)
            before = _latest_event_id(session)
            thread = repo.wake_thread(thread_id, reason=payload.reason, actor_type="human", actor_id=human_handle(request))
            source_post = repo.list_posts(thread.id, limit=2_000)[-1] if thread.current_sequence else None
            stimulus = None
            if thread.run_id and cadence.enabled(repo.get_run(thread.run_id)):
                stimulus = cadence.on_input(repo, repo.get_run(thread.run_id))
            elif thread.run_id and source_post and not sessions.is_legacy_scripted(repo.get_run(thread.run_id)):
                kind = StimulusKind.NEW_EVIDENCE if payload.reason == "new_evidence" else StimulusKind.IDLE_REVISIT
                stimulus = repo.add_stimulus(
                    run_id=thread.run_id,
                    thread_id=thread.id,
                    kind=kind,
                    source_post_id=source_post.id,
                    priority=6.0,
                    payload={"reason": payload.reason, "evidence": payload.evidence},
                    dedupe_key=f"wake:{thread.id}:{payload.reason}:{source_post.id}:{thread.current_sequence}",
                )
            output = {"thread": _thread_json(thread), "stimulus_id": stimulus.id if stimulus else None}
        await publish_since(before)
        return output

    @app.post("/api/agents", status_code=status.HTTP_201_CREATED)
    async def create_agent(payload: AgentCreate) -> dict[str, Any]:
        try:
            validate_hosted_provider(payload.provider, payload.settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        with factory.begin() as session:
            before = _latest_event_id(session)
            agent = Repository(session).create_agent(**payload.model_dump())
            output = _agent_json(agent)
        await publish_since(before)
        return output

    def find_ada(repo: Repository) -> Agent | None:
        agents = repo.list_agents()
        for handle in ("ada", "persona1"):
            candidate = next((agent for agent in agents if agent.handle == handle), None)
            if candidate is not None:
                if candidate.settings.get("persona_harness"):
                    return candidate
                if handle == "ada":
                    raise InvalidStateError("@ada already belongs to another participant")
        return None

    def load_ada(repo: Repository) -> Agent:
        # Only the server-configured directory is readable through this route.
        try:
            snapshot = load_persona(settings.persona_dir)
        except (OSError, ValueError) as exc:
            raise InvalidStateError(f"Ada's persona files could not be loaded: {exc}") from exc
        existing = find_ada(repo)
        if existing is None:
            return repo.create_agent(**persona_spec(snapshot, codex_model_source()).model_dump())
        previous_handle = existing.handle
        existing.handle = "ada"
        existing.role = "participant"
        existing.persona = "Personality and instructions come from AGENTS.md and memory.md."
        existing.settings = {
            **existing.settings, "display_name": "Ada", "persona_harness": snapshot.model_dump(), "expertise": [],
        }
        existing.settings.pop("scheduler_role", None)
        repo.session.flush()
        repo.add_event(
            "agent.updated", agent_id=existing.id, actor_type="human",
            payload={"fields": ["handle", "role", "persona", "settings"], "previous_handle": previous_handle, "persona_sha256": snapshot.digest},
        )
        return existing

    @app.get("/api/personas/ada")
    async def ada_status() -> dict[str, Any]:
        with factory() as session:
            repo = Repository(session)
            ada = find_ada(repo)
            peers = [agent for agent in repo.list_agents(enabled_only=True) if ada is None or agent.id != ada.id]
            defaults = [agent.id for agent in peers if agent.provider == "openai_compatible"]
            if ada is not None:
                previous = next((run for run in repo.list_runs() if run.state == "completed" and ada.id in run.config.get("agent_ids", [])), None)
                if previous is not None:
                    defaults = [agent.id for agent in peers if agent.id in previous.config["agent_ids"]]
            return {
                "agent": _agent_json(ada) if ada is not None else None,
                "files_available": all((settings.persona_dir / name).is_file() for name in ("AGENTS.md", "memory.md")),
                "default_peer_ids": defaults or [agent.id for agent in peers],
            }

    @app.post("/api/personas/ada/reload")
    async def reload_ada() -> dict[str, Any]:
        with factory.begin() as session:
            before = _latest_event_id(session)
            ada = load_ada(Repository(session))
            output = _agent_json(ada)
        await publish_since(before)
        return output

    @app.post("/api/personas/ada/sessions", status_code=status.HTTP_201_CREATED)
    async def start_ada_session(payload: AdaSessionRequest, request: Request) -> dict[str, Any]:
        key = request_key(request, f"ada-session:{payload.idempotency_key}")
        fingerprint = hashlib.sha256(payload.model_dump_json(
            exclude={"session_type", "policy"} if payload.session_type == "collaboration" else None).encode()).hexdigest()
        with factory.begin() as session:
            before = _latest_event_id(session)
            repo = Repository(session)
            prior = session.scalar(select(Post).where(Post.idempotency_key == key))
            if prior is not None:
                if prior.metadata_json.get("ada_request") != fingerprint:
                    raise InvalidStateError("this request key already belongs to a different Ada session")
                thread = repo.get_thread(prior.thread_id)
                run = repo.get_run(thread.run_id)
            else:
                peers = [repo.get_agent(agent_id) for agent_id in dict.fromkeys(payload.peer_ids)]
                if any(not peer.enabled for peer in peers):
                    raise InvalidStateError("selected peers must be enabled")
                ada = load_ada(repo)
                if not ada.enabled or ada.settings.get("persona_harness") is None:
                    raise InvalidStateError("enable Ada before starting a conversation")
                if ada.id in payload.peer_ids:
                    raise InvalidStateError("choose another participant as Ada's peer")
                opening = repo.create_human_thread(
                    title=payload.title, body=f"@ada, {payload.body}", author_handle=human_handle(request, "SYSTEM"),
                    idempotency_key=key, metadata={"ada_request": fingerprint},
                )
                thread = repo.get_thread(opening.post.thread_id)
                run = repo.create_run(
                    seed=41, continuous=payload.continuous,
                    config={"max_agents_per_stimulus": 1, "model_retries": 1, "agent_ids": [ada.id, *(peer.id for peer in peers)],
                            "session_type": payload.session_type, "policy": payload.policy},
                    **payload.limits.model_dump(),
                )
                thread.run_id = run.id
                repo.add_event("thread.run_attached", run_id=run.id, thread_id=thread.id, actor_type="human", payload={"run_id": run.id})
                _add_post_stimuli(
                    repo, run_id=run.id, thread_id=thread.id, post=opening.post,
                    triggering_event_id=opening.event.id, default_kind=StimulusKind.HUMAN_POST, default_priority=5.0,
                )
            run_id, thread_id = run.id, thread.id
            should_start = run.continuous and run.state == RunState.CREATED.value
        await publish_since(before)
        if should_start:
            await swarm.start(run_id)
        with factory() as session:
            return {"thread_id": thread_id, "run": _run_json(Repository(session).get_run(run_id), thread_id=thread_id)}

    @app.patch("/api/agents/{agent_id}")
    async def update_agent(agent_id: str, payload: AgentPatch, request: Request) -> dict[str, Any]:
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            raise HTTPException(422, "at least one agent field is required")
        with factory.begin() as session:
            repo = Repository(session)
            before = _latest_event_id(session)
            agent = repo.get_agent(agent_id)
            try:
                validate_hosted_provider(changes.get("provider", agent.provider), changes.get("settings", agent.settings))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            for name, value in changes.items():
                if name == "handle":
                    value = normalize_agent_handle(value)
                    duplicate = session.scalar(
                        select(Agent).where(
                            Agent.handle.collate("NOCASE") == value,
                            Agent.id != agent.id,
                        )
                    )
                    if duplicate is not None:
                        raise HTTPException(409, f"agent handle @{value} already exists")
                setattr(agent, name, value)
            session.flush()
            repo.add_event(
                "agent.updated",
                agent_id=agent.id,
                actor_type="human",
                actor_id=human_handle(request),
                payload={"fields": sorted(changes)},
            )
            output = _agent_json(agent)
        await publish_since(before)
        return output

    @app.post("/api/runs", status_code=status.HTTP_201_CREATED)
    async def create_run(payload: NewRunRequest, request: Request) -> dict[str, Any]:
        with factory.begin() as session:
            repo = Repository(session)
            before = _latest_event_id(session)
            thread = repo.get_thread(payload.thread_id)
            if thread.status == ThreadStatus.CLOSED.value:
                raise InvalidStateError("cannot start a run on a closed thread")
            if thread.run_id:
                attached = repo.get_run(thread.run_id)
                raise InvalidStateError(
                    f"thread is already attached to run {attached.id} ({attached.state}); "
                    "use rerun to preserve historical replay provenance"
                )
            if payload.agent_ids is not None:
                for agent_id in payload.agent_ids:
                    if not repo.get_agent(agent_id).enabled:
                        raise InvalidStateError("run participants must be enabled")
            run = repo.create_run(
                seed=payload.seed,
                continuous=payload.continuous,
                config={
                    "session_type": payload.session_type,
                    "policy": payload.policy,
                    "max_agents_per_stimulus": payload.max_agents_per_stimulus,
                    "model_retries": payload.model_retries,
                    **({"agent_ids": list(dict.fromkeys(payload.agent_ids))} if payload.agent_ids is not None else {}),
                },
                max_rounds=payload.limits.max_rounds,
                max_posts=payload.limits.max_posts,
                max_tokens=payload.limits.max_tokens,
                max_duration_seconds=payload.limits.max_duration_seconds,
                per_agent_quota=payload.limits.per_agent_quota,
                per_thread_quota=payload.limits.per_thread_quota,
                max_cascade_depth=payload.limits.max_cascade_depth,
            )
            thread.run_id = run.id
            thread.updated_at = utc_now()
            repo.add_event(
                "thread.run_attached",
                run_id=run.id,
                thread_id=thread.id,
                actor_type="human",
                actor_id=human_handle(request),
                payload={"run_id": run.id},
            )
            posts = repo.list_posts(thread.id, limit=2_000)
            if posts:
                source = posts[-1]
                _add_post_stimuli(
                    repo,
                    run_id=run.id,
                    thread_id=thread.id,
                    post=source,
                    triggering_event_id=None,
                    default_kind=StimulusKind.HUMAN_POST,
                    default_priority=5.0,
                )
            output = _run_json(run, thread_id=thread.id)
            run_id = run.id
        await publish_since(before)
        if payload.continuous:
            await swarm.start(run_id)
            with factory() as session:
                run = Repository(session).get_run(run_id)
                output = _run_json(run, thread_id=payload.thread_id)
        return output

    async def _run_control(run_id: str, action: str, reason: str | None = None) -> dict[str, Any]:
        try:
            if action == "start":
                await swarm.start(run_id)
            elif action == "pause":
                await swarm.pause(run_id)
            elif action == "resume":
                await swarm.resume(run_id)
            elif action == "step":
                result = await swarm.step(run_id)
                return jsonable_encoder(result)
            elif action == "stop":
                await swarm.stop(run_id, reason=reason)
            elif action == "emergency_stop":
                await swarm.emergency_stop(run_id, reason=reason)
            else:
                raise ValueError(action)
        finally:
            # Engine mutations publish their own events. Reading the durable row
            # here also gives controls a stable response after task cancellation.
            pass
        with factory() as session:
            run = Repository(session).get_run(run_id)
            thread = session.scalar(select(Thread).where(Thread.run_id == run_id).order_by(Thread.created_at))
            return _run_json(run, thread_id=thread.id if thread else None)

    @app.post("/api/runs/{run_id}/start")
    async def start_run(run_id: str) -> dict[str, Any]:
        return await _run_control(run_id, "start")

    @app.post("/api/runs/{run_id}/pause")
    async def pause_run(run_id: str) -> dict[str, Any]:
        return await _run_control(run_id, "pause")

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str) -> dict[str, Any]:
        return await _run_control(run_id, "resume")

    @app.post("/api/runs/{run_id}/step")
    async def step_run(run_id: str) -> dict[str, Any]:
        return await _run_control(run_id, "step")

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str) -> dict[str, Any]:
        return await _run_control(run_id, "stop", "stopped by human")

    @app.post("/api/runs/{run_id}/emergency-stop")
    async def emergency_stop_run(run_id: str) -> dict[str, Any]:
        return await _run_control(run_id, "emergency_stop", "emergency stop requested by human")

    @app.post("/api/emergency-stop")
    async def emergency_stop_all() -> dict[str, Any]:
        with factory() as session:
            ids = list(
                session.scalars(select(Run.id).where(Run.state.not_in(TERMINAL_RUN_STATES)))
            )
        for run_id in ids:
            await swarm.emergency_stop(run_id, reason="global emergency stop requested by human")
        return {"stopped_run_ids": ids}

    @app.get("/api/runs/{run_id}/replay")
    async def replay_run(run_id: str) -> dict[str, Any]:
        """Return the original stream verbatim; no provider is called."""
        with factory() as session:
            repo = Repository(session)
            run = repo.get_run(run_id)
            threads = list(
                session.scalars(
                    select(Thread)
                    .where(Thread.run_id == run_id)
                    .order_by(Thread.created_at, Thread.id)
                )
            )
            events = _all_run_events(repo, run_id)
            turns = list(
                session.scalars(
                    select(Turn)
                    .where(Turn.run_id == run_id)
                    .order_by(Turn.started_at, Turn.id)
                )
            )
            posts = list(
                session.scalars(
                    select(Post)
                    .join(Thread, Post.thread_id == Thread.id)
                    .where(Thread.run_id == run_id)
                    .order_by(Post.created_at, Post.id)
                )
            )
            return {
                "mode": "replay",
                "model_calls": 0,
                "run": _run_json(run, thread_id=threads[0].id if threads else None),
                "events": [_event_json(event) for event in events],
                "threads": [_thread_json(thread) for thread in threads],
                "posts": [_post_json(post) for post in posts],
                "turns": [_turn_json(turn) for turn in turns],
            }

    @app.post("/api/runs/{run_id}/rerun", status_code=status.HTTP_201_CREATED)
    async def rerun(run_id: str) -> dict[str, Any]:
        """Regenerate through the engine's single, audited rerun implementation."""
        new_run_id = await swarm.rerun(run_id)
        with factory() as session:
            repo = Repository(session)
            new_run = repo.get_run(new_run_id)
            threads = repo.list_threads(run_id=new_run_id, limit=1_000)
            thread_id = threads[0].id if threads else None
            return {
                "id": new_run.id,
                "thread_id": thread_id,
                "mode": "rerun",
                "source_run_id": run_id,
                "run": _run_json(new_run, thread_id=thread_id),
            }

    @app.get("/api/turns/{turn_id}")
    async def get_turn(turn_id: str) -> dict[str, Any]:
        with factory() as session:
            turn = Repository(session).get_turn(turn_id)
            responses = session.scalars(select(Event).where(Event.run_id == turn.run_id,
                Event.event_type == "provider.response").order_by(Event.id))
            return {**_turn_json(turn), "provider_responses": [event.payload for event in responses
                                                             if event.payload.get("turn_id") == turn_id]}

    @app.get("/api/events")
    async def event_stream(
        request: Request,
        after_id: int = Query(default=0, ge=0),
        once: bool = Query(default=False),
    ) -> StreamingResponse:
        header_id = request.headers.get("last-event-id")
        cursor = max(after_id, int(header_id) if header_id and header_id.isdigit() else 0)

        async def generate() -> AsyncIterator[str]:
            nonlocal cursor
            async with broker.subscribe() as queue:
                while not await request.is_disconnected():
                    # SQLite is the source of truth. Drain every durable page
                    # before waiting so reconnect backlogs and broker queue
                    # overflow cannot create permanent gaps.
                    while True:
                        with factory() as session:
                            backlog = Repository(session).list_events(
                                after_id=cursor,
                                limit=EVENT_STREAM_PAGE_SIZE,
                            )
                            payloads = [_event_json(event) for event in backlog]
                        for payload in payloads:
                            cursor = int(payload["id"])
                            import json

                            yield (
                                f"id: {payload['id']}\n"
                                f"event: update\n"
                                f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                            )
                        if len(backlog) < EVENT_STREAM_PAGE_SIZE:
                            break
                    if once:
                        return
                    try:
                        # Payload content is only a wake-up hint. The next loop
                        # reads by durable cursor, including any dropped hints.
                        await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": keep-alive\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    from .session_api import router as session_router
    app.include_router(session_router(factory, find_ada, load_ada, swarm, publish_since))
    return app


__all__ = ["create_app"]
