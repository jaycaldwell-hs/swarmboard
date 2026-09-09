"""Human-only session interventions over the shared authenticated API."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from . import interventions
from .auth import human_handle, request_key
from .credentials import redact
from .models import Event
from .repository import Repository


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    idempotency_key: str = Field(min_length=1, max_length=120)


class ResearchPost(Operation):
    body: str = Field(min_length=1, max_length=50_000)
    as_handle: str | None = Field(default=None, min_length=1, max_length=80)
    system_author: bool = False
    parent_post_id: str | None = None


class Instruction(Operation):
    body: str = Field(min_length=1, max_length=100_000)


class Configuration(Operation):
    provider: Literal["openai_compatible", "codex"] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=255)
    persona: str | None = Field(default=None, min_length=1, max_length=100_000)
    settings: dict[str, Any] | None = None


class SeedMemory(Instruction):
    tags: list[str] = Field(default_factory=list, max_length=50)
    active: bool = True
    replaces_memory_id: str | None = None


def router(factory, swarm, publish_since):
    api = APIRouter()

    async def write(operation, payload, request, **target):
        values = payload.model_dump(exclude={"idempotency_key"}, exclude_unset=isinstance(payload, Configuration))
        try:
            with factory.begin() as session:
                before = session.scalar(select(func.max(Event.id))) or 0
                result = operation(Repository(session), **target, **values,
                    author=human_handle(request), idempotency_key=request_key(request, "intervention:" + payload.idempotency_key))
        except ValueError as exc:
            raise HTTPException(422, redact(str(exc))) from None
        await publish_since(before)
        if result.get("run_id"):
            swarm.notify(result["run_id"])
        return redact(result)

    @api.get("/api/runs/{run_id}/interventions")
    async def list_run_interventions(run_id: str):
        with factory() as session:
            return redact(interventions.list_interventions(Repository(session), run_id))

    @api.post("/api/threads/{thread_id}/research-posts", status_code=201)
    async def post(thread_id: str, payload: ResearchPost, request: Request):
        return await write(interventions.research_post, payload, request, thread_id=thread_id)

    @api.post("/api/runs/{run_id}/agents/{agent_id}/instructions", status_code=201)
    async def instruction(run_id: str, agent_id: str, payload: Instruction, request: Request):
        return await write(interventions.create_instruction, payload, request, run_id=run_id, agent_id=agent_id)

    @api.post("/api/instructions/{instruction_id}/revoke", status_code=201)
    async def revoke(instruction_id: int, payload: Operation, request: Request):
        return await write(interventions.revoke_instruction, payload, request, instruction_id=instruction_id)

    @api.patch("/api/runs/{run_id}/agents/{agent_id}/configuration")
    async def configuration(run_id: str, agent_id: str, payload: Configuration, request: Request):
        return await write(interventions.change_configuration, payload, request, run_id=run_id, agent_id=agent_id)

    @api.post("/api/runs/{run_id}/agents/{agent_id}/memories", status_code=201)
    async def seed_memory(run_id: str, agent_id: str, payload: SeedMemory, request: Request):
        return await write(interventions.seed_memory, payload, request, run_id=run_id, agent_id=agent_id)

    @api.post("/api/memories/{memory_id}/deactivate", status_code=201)
    async def deactivate(memory_id: str, payload: Operation, request: Request):
        return await write(interventions.deactivate_memory, payload, request, memory_id=memory_id)

    return api
