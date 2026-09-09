"""Attributed research operations on the existing durable board ledger."""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from . import research, sessions
from .auth import human_handle, request_key
from .credentials import redact
from .models import Event, Post, Run, Stimulus, Thread
from .repository import InvalidStateError, Repository


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ForkRequest(Operation):
    at_post_id: str
    policy: dict[str, Any] | str | None = None
    agent_ids: list[str] | None = Field(default=None, max_length=50)
    config_overrides: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, int] | None = None
    inherit_remaining: bool = False
    continuous: bool = False


class ForceRequest(Operation):
    agent_id: str
    thread_id: str | None = None
    stimulus_post_id: str | None = None
    override_cooldown: bool = False


class ResampleRequest(Operation):
    n: int = Field(default=1, ge=1, le=20)
    reuse_prompt: bool = True


def router(factory, swarm, publish_since):
    api = APIRouter()

    def previous(repo, key, fingerprint):
        event = repo.session.scalar(select(Event).where(Event.event_type == "research.request",
            Event.payload["request_key"].as_string() == key))
        if event is not None:
            if event.payload["request_sha256"] != fingerprint:
                raise InvalidStateError("request key already used for another research operation")
            return event.payload["result"]
        return None

    @api.post("/api/threads/{thread_id}/fork", status_code=201)
    async def fork_thread(thread_id: str, body: ForkRequest, request: Request):
        key = request_key(request, f"fork:{thread_id}:{body.idempotency_key}")
        fingerprint = sessions.digest(body.model_dump())
        with factory.begin() as session:
            before = session.scalar(select(func.max(Event.id))) or 0
            repo = Repository(session)
            result = previous(repo, key, fingerprint)
            if result is None:
                result = research.fork(repo, thread_id=thread_id, author=human_handle(request),
                    **body.model_dump(exclude={"idempotency_key"}))
                repo.add_event("research.request", run_id=result["run_id"], thread_id=result["thread_id"],
                    actor_type="human", actor_id=human_handle(request),
                    payload={"request_key": key, "request_sha256": fingerprint, "result": result})
        await publish_since(before)
        if body.continuous:
            with factory() as session:
                should_start = session.get(Run, result["run_id"]).state == "created"
            if should_start:
                await swarm.start(result["run_id"])
        return redact(result)

    @api.post("/api/runs/{run_id}/force-turn", status_code=201)
    async def force_turn(run_id: str, body: ForceRequest, request: Request):
        with factory.begin() as session:
            before = session.scalar(select(func.max(Event.id))) or 0
            stimulus = research.force_turn(Repository(session), run_id=run_id, author=human_handle(request),
                **body.model_dump(exclude={"idempotency_key"}),
                idempotency_key=request_key(request, f"force:{run_id}:{body.idempotency_key}"))
            result = {"stimulus_id": stimulus.id, "run_id": run_id, "thread_id": stimulus.thread_id}
        await publish_since(before)
        swarm.notify(run_id)
        return redact(result)

    @api.post("/api/turns/{turn_id}/resample", status_code=201)
    async def resample(turn_id: str, body: ResampleRequest, request: Request):
        with factory.begin() as session:
            before = session.scalar(select(func.max(Event.id))) or 0
            result = research.resample(Repository(session), turn_id=turn_id, n=body.n,
                reuse_prompt=body.reuse_prompt, author=human_handle(request),
                idempotency_key=request_key(request, f"resample:{turn_id}:{body.idempotency_key}"))
        await publish_since(before)
        return redact(result)

    @api.get("/api/threads/{thread_id}/forks")
    async def forks_of(thread_id: str):
        with factory() as session:
            Repository(session).get_thread(thread_id)
            rows = session.execute(select(Run, Thread).join(Thread, Thread.run_id == Run.id).where(
                Run.config["lineage"]["parent_thread_id"].as_string() == thread_id).order_by(Run.created_at)).all()
            return redact({"forks": [{"run_id": run.id, "thread_id": thread.id, "lineage": run.config["lineage"],
                               "sibling_group_id": run.config.get("sibling_group_id")} for run, thread in rows]})

    @api.get("/api/threads/{thread_id}/participant-view")
    async def participant_view(thread_id: str, agent_id: str, at_post_id: str | None = None):
        with factory() as session:
            repo = Repository(session)
            thread = repo.get_thread(thread_id)
            if not thread.run_id:
                raise InvalidStateError("participant views require a session")
            run = repo.get_run(thread.run_id)
            if agent_id not in run.config.get("agent_ids", []):
                raise InvalidStateError("participant is outside this session")
            agent = sessions.session_agent(session, run, repo.get_agent(agent_id))
            query = select(Post).where(Post.thread_id == thread.id).order_by(Post.sequence)
            at_event_id = None
            if at_post_id:
                point = repo.get_post(at_post_id)
                if point.thread_id != thread.id:
                    raise InvalidStateError("view point belongs to another thread")
                query = query.where(Post.sequence <= point.sequence)
                at_event_id = session.scalar(select(Event.id).where(Event.post_id == point.id,
                    Event.event_type == "post.created"))
            posts = list(session.scalars(query))
            stimulus = None if at_post_id else session.scalar(select(Stimulus).where(
                Stimulus.thread_id == thread.id, Stimulus.state.in_(["pending", "claimed", "processing"]),
                (Stimulus.target_agent_id == agent.id) | Stimulus.target_agent_id.is_(None))
                .order_by(Stimulus.priority.desc(), Stimulus.created_at).limit(1))
            if stimulus is None:
                stimulus = SimpleNamespace(id="participant-view", kind="new_evidence", source_post_id=posts[-1].id if posts else None,
                                           cascade_depth=0, payload={})
            context, messages, memories = swarm._build_context(session, run=run, thread=thread,
                posts=posts, stimulus=stimulus, agent=agent, at_event_id=at_event_id)
            participant_context = json.loads(messages[-1].content.split("\n", 1)[1])
            if at_post_id:
                participant_context["thread"]["current_sequence"] = posts[-1].sequence if posts else 0
                participant_context["thread"]["summary"] = None
                # Re-encode after historical visibility adjustments.
                messages[-1].content = "Captured immutable discussion context:\n" + json.dumps(participant_context, ensure_ascii=False, separators=(",", ":"))
            prompt = json.dumps([message.model_dump() for message in messages], ensure_ascii=False, separators=(",", ":"))
            if not at_post_id and stimulus.payload.get("reuse_turn_id"):
                original = repo.get_turn(stimulus.payload["reuse_turn_id"])
                prompt = original.prompt
                messages = swarm._decode_prompt(prompt)
                participant_context = json.loads(messages[-1].content.split("\n", 1)[1])
                memories = original.retrieved_memory_ids
            return redact({"run_id": run.id, "thread_id": thread.id, "agent_id": agent.id,
                    "messages": [m.model_dump() for m in messages], "context": participant_context, "prompt": prompt,
                    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "memory_ids": memories,
                    "at_post_id": at_post_id, "view_kind": "participant_context_preview"})

    return api
