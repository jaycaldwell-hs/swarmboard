"""Versioned, complete research exports from a consistent SQLite read snapshot."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from tempfile import SpooledTemporaryFile
from typing import Any, Iterator
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.encoders import jsonable_encoder
from sqlalchemy import and_, func, inspect, or_, select

from .credentials import redact
from .findings import list_findings
from .models import Agent, Event, Experiment, Post, Run, Stimulus, Thread, Turn, utc_now
from .repository import Repository
from .run_policy import normalize_config
from .context_views import persona_identity
from . import sessions


EXPORT_SCHEMA_VERSION = 1
PAGE_SIZE = 200


def _hash(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if isinstance(value, str) else None


def _fields(row) -> dict[str, Any]:
    return {attribute.key: deepcopy(getattr(row, attribute.key))
            for attribute in inspect(type(row)).column_attrs}


@contextmanager
def snapshot(factory, run_id: str):
    """Keep every page and ZIP member on the same committed database snapshot.

    SQLite's legacy DB-API transaction mode does not begin a read transaction
    for SELECT alone, so explicitly begin before the first read.
    """
    with factory() as session:
        session.connection().exec_driver_sql("BEGIN")
        try:
            repo = Repository(session)
            run = repo.get_run(run_id)
            yield repo, run, utc_now(), list_findings(repo, run_id)
        finally:
            session.rollback()


def _ordered_rows(repo, model, timestamp, criterion) -> Iterator[Any]:
    cursor = None
    while True:
        query = select(model).where(criterion)
        if cursor is not None:
            query = query.where(or_(timestamp > cursor[0], and_(timestamp == cursor[0], model.id > cursor[1])))
        page = list(repo.session.scalars(query.order_by(timestamp, model.id).limit(PAGE_SIZE)))
        if not page:
            return
        for row in page:
            yield row
        cursor = (getattr(page[-1], timestamp.key), page[-1].id)


def _record(record_type: str, **values) -> dict[str, Any]:
    return {"record_type": record_type, "export_schema_version": EXPORT_SCHEMA_VERSION, **values}


def header_record(repo, run, exported_at: datetime, findings: dict, *, include_prompts: bool) -> dict:
    saved = repo.session.get(Experiment, run.id)
    manifest = saved.manifest if saved else {}
    ids = run.config.get("agent_ids")
    if ids is None:
        ids = [person["id"] for person in manifest.get("participants", [])]
    if not ids:
        ids = list(repo.session.scalars(select(Turn.agent_id).where(Turn.run_id == run.id).distinct()))
    roster = []
    for agent_id in dict.fromkeys(ids):
        agent = repo.session.get(Agent, agent_id)
        if agent is None:
            roster.append({"id": agent_id, "registration_available": False})
            continue
        agent = sessions.configured_agent(run.config, agent)
        roster.append({
            "id": agent.id, "handle": agent.handle, "provider": agent.provider, "model": agent.model,
            "settings": deepcopy(agent.settings), "permissions": deepcopy(agent.permissions),
            "persona_sha256": persona_identity(agent)["sha256"],
            "persona_version": persona_identity(agent)["version"],
            "persona_identity": persona_identity(agent),
            "configuration_as_of": "export", "enabled": agent.enabled,
        })
    opening = deepcopy(manifest.get("opening"))
    if opening is None:
        first = repo.session.scalar(select(Post).join(Thread).where(Thread.run_id == run.id)
                                    .order_by(Post.created_at, Post.id).limit(1))
        if first:
            opening = {"post_id": first.id, "thread_id": first.thread_id,
                       "body": first.body, "author_handle": first.author_handle,
                       "author_type": first.author_type}
    config = normalize_config(run.config)
    from .interventions import list_interventions
    return _record(
        "header", run_id=run.id, exported_at=exported_at,
        session_type=config["session_type"], policy=config["policy"], config=deepcopy(run.config),
        state=run.state, seed=run.seed, continuous=run.continuous,
        budgets={
            "limits": {name: getattr(run, name) for name in (
                "max_rounds", "max_posts", "max_tokens", "max_duration_seconds",
                "per_agent_quota", "per_thread_quota", "max_cascade_depth")},
            "used": {name: getattr(run, name) for name in (
                "rounds_used", "posts_used", "tokens_used", "model_calls", "virtual_time")},
        },
        roster=roster, roster_at_creation=deepcopy(manifest.get("participants", [])),
        opening=opening, lineage=deepcopy(run.config.get("lineage")),
        sibling_group_id=run.config.get("sibling_group_id"),
        source_turn_id=run.config.get("source_turn_id"), interventions=list_interventions(repo, run.id),
        findings=findings, include_prompts=include_prompts,
        snapshot={"last_event_id": repo.session.scalar(select(func.max(Event.id))) or 0},
    )


def _turn_record(repo, run, turn, sequence, findings, *, include_prompts):
    data = _fields(turn)
    prompt = data.pop("prompt")
    stimulus = repo.session.get(Stimulus, turn.stimulus_id) if turn.stimulus_id else None
    payload = stimulus.payload if stimulus else {}
    scores = turn.scheduler_scores or {}
    post = repo.session.get(Post, turn.resulting_post_id) if turn.resulting_post_id else None
    context = turn.context_snapshot or {}
    persona = deepcopy(context.get("persona_snapshot"))
    if persona is None:
        persona = {"version": None, "sha256": None, "capture_status": "not_separately_captured"}
    post_map = run.config.get("post_id_map", {})
    effective_ids = [post_map.get(post_id, post_id) for post_id in turn.context_post_ids]
    inherited_ids = [item.id for item in repo.session.scalars(select(Post).where(Post.id.in_(effective_ids)))
                     if item.metadata_json.get("is_inherited")]
    data.update(
        turn_id=turn.id, turn_sequence=sequence,
        post_sequence=post.sequence if post else None,
        prompt_sha256=_hash(prompt), prompt_ref=f"/api/turns/{turn.id}", persona=persona,
        raw_output_sha256=_hash(turn.raw_output),
        forced=bool(scores.get("forced") or payload.get("forced")),
        forced_by=scores.get("forced_by") or payload.get("forced_by"),
        reuse_turn_id=payload.get("reuse_turn_id"),
        lineage=deepcopy(run.config.get("lineage")),
        sibling_group_id=run.config.get("sibling_group_id"),
        source_turn_id=run.config.get("source_turn_id"),
        is_inherited=bool(post and post.metadata_json.get("is_inherited")),
        inherited_context_post_ids=inherited_ids,
        context_post_id_map={post_id: post_map[post_id] for post_id in turn.context_post_ids if post_id in post_map},
        flags=[flag for flag in findings["flags"] if flag.get("turn_id") == turn.id
               or (turn.resulting_post_id and flag.get("post_id") == turn.resulting_post_id)],
        notes=findings["notes"], interventions=deepcopy(context.get("interventions", {})),
    )
    if include_prompts:
        data["prompt"] = prompt
    return _record("turn", **data)


def records(repo, run, exported_at, findings, *, include_prompts=True) -> Iterator[dict]:
    yield header_record(repo, run, exported_at, findings, include_prompts=include_prompts)
    for sequence, turn in enumerate(_ordered_rows(repo, Turn, Turn.started_at, Turn.run_id == run.id), 1):
        yield _turn_record(repo, run, turn, sequence, findings, include_prompts=include_prompts)
    thread_ids = select(Thread.id).where(Thread.run_id == run.id)
    for post in _ordered_rows(repo, Post, Post.created_at, Post.thread_id.in_(thread_ids)):
        data = _fields(post)
        data["metadata"] = data.pop("metadata_json")
        yield _record("post", **data, post_id=post.id, run_id=run.id,
                      is_inherited=bool(post.metadata_json.get("is_inherited")),
                      flags=[flag for flag in findings["flags"] if flag.get("post_id") == post.id])


def event_records(repo, run_id) -> Iterator[dict]:
    after_id = 0
    while True:
        page = repo.list_events(run_id=run_id, after_id=after_id, limit=PAGE_SIZE)
        if not page:
            return
        for event in page:
            yield _record("event", **_fields(event))
        after_id = page[-1].id


def finding_records(run_id, findings) -> Iterator[dict]:
    for kind, plural in (("flag", "flags"), ("note", "notes")):
        for finding in findings[plural]:
            yield _record(kind, **{"run_id": run_id, **deepcopy(finding)})


def encode_record(record: dict) -> bytes:
    original = jsonable_encoder(record)
    safe = redact(original)
    safe["redacted"] = safe != original
    if record.get("record_type") == "turn":
        safe["raw_output_redacted"] = safe.get("raw_output") != original.get("raw_output")
        safe["prompt_redacted"] = "prompt" in original and safe.get("prompt") != original.get("prompt")
    return (json.dumps(safe, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def jsonl(factory, run_id, *, include_prompts=True, events_only=False) -> Iterator[bytes]:
    with snapshot(factory, run_id) as (repo, run, exported_at, findings):
        source = event_records(repo, run_id) if events_only else records(
            repo, run, exported_at, findings, include_prompts=include_prompts)
        for record in source:
            yield encode_record(record)


def zip_bundle(factory, run_id, *, include_prompts=True):
    """Write compressed members incrementally; spill large bundles to disk."""
    output = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        with snapshot(factory, run_id) as (repo, run, exported_at, findings):
            with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
                members = (
                    ("turns.jsonl", records(repo, run, exported_at, findings, include_prompts=include_prompts)),
                    ("events.jsonl", event_records(repo, run_id)),
                    ("findings.jsonl", finding_records(run_id, findings)),
                )
                for name, source in members:
                    with archive.open(name, "w") as member:
                        for record in source:
                            member.write(encode_record(record))
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
