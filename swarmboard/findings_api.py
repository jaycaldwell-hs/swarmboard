"""Human-attributed findings in both collaboration and research sessions."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select

from . import findings
from .auth import human_handle, request_key
from .models import Event
from .repository import Repository


class FindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    body: str = Field(default="", max_length=100_000)
    tags: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(default_factory=list, max_length=50)
    idempotency_key: str = Field(min_length=1, max_length=120)


def router(factory, publish_since):
    from .credentials import redact

    api = APIRouter()

    async def write(operation, payload, request, **target):
        with factory.begin() as session:
            before = session.scalar(select(func.max(Event.id))) or 0
            output = operation(Repository(session), author=human_handle(request),
                body=payload.body, tags=payload.tags,
                idempotency_key=request_key(request, "finding:" + payload.idempotency_key), **target)
        await publish_since(before)
        return redact(output)

    @api.get("/api/research/settings")
    async def settings():
        return redact({"suggested_tags": findings.suggested_tags()})

    @api.get("/api/runs/{run_id}/findings")
    async def list_run_findings(run_id: str):
        with factory() as session:
            return redact({**findings.list_findings(Repository(session), run_id),
                           "suggested_tags": findings.suggested_tags()})

    @api.post("/api/posts/{post_id}/flags", status_code=201)
    async def flag_post(post_id: str, payload: FindingInput, request: Request):
        return await write(findings.flag, payload, request, target_type="post", target_id=post_id)

    @api.post("/api/turns/{turn_id}/flags", status_code=201)
    async def flag_turn(turn_id: str, payload: FindingInput, request: Request):
        return await write(findings.flag, payload, request, target_type="turn", target_id=turn_id)

    @api.post("/api/flags/{flag_event_id}/resolve", status_code=201)
    async def resolve(flag_event_id: int, payload: FindingInput, request: Request):
        return await write(findings.resolve_flag, payload, request, flag_event_id=flag_event_id)

    @api.post("/api/runs/{run_id}/notes", status_code=201)
    async def note(run_id: str, payload: FindingInput, request: Request):
        return await write(findings.note, payload, request, run_id=run_id)

    return api
