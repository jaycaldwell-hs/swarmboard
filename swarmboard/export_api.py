"""Download research artifacts for either collaboration or research sessions."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from . import research_export
from .repository import Repository


def router(factory):
    api = APIRouter()

    def require_run(run_id):
        # Fail unknown IDs before sending streaming response headers.
        with factory() as session:
            Repository(session).get_run(run_id)

    @api.get("/api/runs/{run_id}/export.jsonl")
    def export_jsonl(run_id: str, include_prompts: bool = True):
        require_run(run_id)
        return StreamingResponse(
            research_export.jsonl(factory, run_id, include_prompts=include_prompts),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="swarmboard-{run_id}.jsonl"'},
        )

    @api.get("/api/runs/{run_id}/events.jsonl")
    def export_events(run_id: str):
        require_run(run_id)
        return StreamingResponse(
            research_export.jsonl(factory, run_id, events_only=True),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="events-{run_id}.jsonl"'},
        )

    @api.get("/api/runs/{run_id}/export.zip")
    def export_zip(run_id: str, include_prompts: bool = True):
        require_run(run_id)
        archive = research_export.zip_bundle(factory, run_id, include_prompts=include_prompts)

        def chunks():
            try:
                while chunk := archive.read(64 * 1024):
                    yield chunk
            finally:
                archive.close()

        return StreamingResponse(chunks(), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="swarmboard-{run_id}.zip"'})

    return api
