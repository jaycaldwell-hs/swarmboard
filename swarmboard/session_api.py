"""Create and inspect collaboration sessions without trial or scoring APIs."""
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import ConfigDict, Field, model_validator
from sqlalchemy import func, select

from . import autonomy, sessions
from .auth import human_handle, request_key
from .credentials import redact
from .models import Event, Experiment, Thread, Turn
from .repository import InvalidStateError, Repository
from .schemas import SessionPolicyInput


class SessionRequest(SessionPolicyInput):
    model_config = ConfigDict(extra="forbid", strict=True)

    agent_ids: list[str] = Field(default_factory=list, max_length=50)
    include_ada: bool = False
    title: str = Field(default="Open board", min_length=1, max_length=120)
    body: str = Field(default=autonomy.OPENING, min_length=1, max_length=12000)
    continuous: bool = True
    cadence: Literal["free", "ada_round_robin"] = "free"
    max_rounds: int = Field(default=100, ge=1, le=1000)
    max_tokens: int = Field(default=200000, ge=100, le=10000000)
    max_duration_seconds: int = Field(default=1200, ge=60, le=86400)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def participants_required(self):
        if not self.agent_ids and not self.include_ada:
            raise ValueError("select at least one participant")
        return self


def router(factory, find_ada, load_ada, swarm, publish_since):
    api = APIRouter()

    @api.post("/api/sessions", status_code=201)
    async def create_session(request: SessionRequest, http_request: Request):
        key = request_key(http_request, "session:" + request.idempotency_key)
        # Preserve the request identity of collaboration setups created before
        # session types existed; research includes its complete policy.
        fingerprint = sessions.digest(request.model_dump(
            exclude={"session_type", "policy"} if request.session_type == "collaboration" else None))
        with factory.begin() as session:
            before = session.scalar(select(func.max(Event.id))) or 0
            repo = Repository(session)
            prior = session.scalar(select(Event).where(Event.event_type == "session.created", Event.actor_id == key))
            if prior:
                if prior.payload["request_sha256"] != fingerprint:
                    raise InvalidStateError("request key already used for another session")
                run = repo.get_run(prior.run_id)
                sessions.require_runnable(run)
                output = prior.payload
            else:
                agents = [repo.get_agent(agent_id) for agent_id in dict.fromkeys(request.agent_ids)]
                if request.include_ada:
                    ada = find_ada(repo) or load_ada(repo)
                    if ada.id not in request.agent_ids:
                        agents.insert(0, ada)
                run = autonomy.create_session(repo, agents=agents, title=request.title, body=request.body,
                    limits={"max_rounds": request.max_rounds, "max_tokens": request.max_tokens,
                            "max_duration_seconds": request.max_duration_seconds},
                    continuous=request.continuous, cadence_mode=request.cadence,
                    author_handle=human_handle(http_request), session_type=request.session_type,
                    policy=request.policy)
                thread = session.scalar(select(Thread).where(Thread.run_id == run.id))
                output = {"run_id": run.id, "thread_id": thread.id, "request_sha256": fingerprint,
                          "session_type": run.config["session_type"], "policy": run.config["policy"]}
                repo.add_event("session.created", run_id=run.id, thread_id=thread.id,
                               actor_type="human", actor_id=key, payload=output)
            start = run.continuous and run.state == "created"
            run_id = run.id
        await publish_since(before)
        if start:
            await swarm.start(run_id)
        return redact(output)

    @api.get("/api/sessions/{run_id}")
    async def get_session(run_id: str):
        with factory() as session:
            return redact(sessions.activity(Repository(session), run_id))

    @api.get("/api/sessions/{run_id}/export")
    async def export_session(run_id: str):
        from .app import _event_json, _post_json, _thread_json, _turn_json
        from .models import Post
        with factory() as session:
            result = sessions.activity(Repository(session), run_id)
            saved = session.get(Experiment, run_id)
            result["setup"] = saved.manifest
            if sessions.is_legacy_scripted(Repository(session).get_run(run_id)):
                result["world"] = saved.world  # Historical data only; there is no simulator.
            result["events"] = [_event_json(event) for event in session.scalars(
                select(Event).where(Event.run_id == run_id).order_by(Event.id))]
            result["turns"] = [_turn_json(turn) for turn in session.scalars(
                select(Turn).where(Turn.run_id == run_id).order_by(Turn.started_at, Turn.id))]
            result["threads"] = [_thread_json(thread) for thread in session.scalars(
                select(Thread).where(Thread.run_id == run_id).order_by(Thread.created_at, Thread.id))]
            result["posts"] = [_post_json(post) for post in session.scalars(
                select(Post).join(Thread, Post.thread_id == Thread.id).where(Thread.run_id == run_id)
                .order_by(Post.created_at, Post.id))]
            return redact(result)

    @api.post("/api/sessions/{run_id}/discard")
    async def discard_session(run_id: str):
        with factory.begin() as session:
            before = session.scalar(select(func.max(Event.id))) or 0
            run = autonomy.discard_unused(Repository(session), run_id)
            output = {"run_id": run.id, "discarded": True}
        await publish_since(before)
        return redact(output)

    return api
