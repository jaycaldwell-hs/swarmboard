"""Append-only human findings, projected from the existing event ledger."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Sequence

from sqlalchemy import select

from .models import Event, Turn
from .repository import InvalidStateError, Repository
from .sessions import digest


FLAG_CREATED = "research.flag_created"
FLAG_RESOLVED = "research.flag_resolved"
NOTE_CREATED = "research.note_created"
FINDING_EVENTS = (FLAG_CREATED, FLAG_RESOLVED, NOTE_CREATED)


def _tags(values: Sequence[str]) -> list[str]:
    if not isinstance(values, (list, tuple)) or len(values) > 50:
        raise InvalidStateError("finding tags must be a list of at most 50 strings")
    if any(not isinstance(tag, str) or not tag.strip() or len(tag) > 100 for tag in values):
        raise InvalidStateError("finding tags must be nonempty strings of at most 100 characters")
    return list(dict.fromkeys(tag.strip() for tag in values))


def suggested_tags() -> list[str]:
    """Suggested tags are a JSON array; researchers may supply any other tags."""
    try:
        configured = json.loads(os.getenv("SWARMBOARD_SUGGESTED_FINDING_TAGS", "[]"))
    except (ValueError, TypeError):
        raise InvalidStateError("SWARMBOARD_SUGGESTED_FINDING_TAGS must be a JSON array of strings") from None
    return _tags(configured)


def _input(author, body, tags, idempotency_key):
    if not isinstance(author, str) or not author.strip():
        raise InvalidStateError("an attributed human operator is required")
    if not isinstance(body, str) or len(body) > 100_000:
        raise InvalidStateError("finding body must be text of at most 100000 characters")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise InvalidStateError("findings require an idempotency key")
    return author.strip(), body, _tags(tags)


def _prior(repo, author, idempotency_key, fingerprint):
    prior = repo.session.scalar(select(Event).where(
        Event.event_type.in_(FINDING_EVENTS), Event.actor_id == author,
        Event.payload["idempotency_key"].as_string() == idempotency_key,
    ).order_by(Event.id).limit(1))
    if prior and prior.payload.get("request_sha256") != fingerprint:
        raise InvalidStateError("request key already used for another finding")
    return prior


def _base(event: Event) -> dict[str, Any]:
    return {
        "id": event.id, "run_id": event.run_id, "author": event.actor_id,
        "created_at": event.created_at.isoformat(), "tags": list(event.payload.get("tags", [])),
        "body": event.payload.get("body", ""), "body_sha256": event.payload.get("body_sha256"),
    }


def _flag_view(event, resolution=None):
    return {
        **_base(event), "target_type": event.payload["target_type"],
        "target_id": event.payload["target_id"], "post_id": event.payload.get("target_post_id"),
        "turn_id": event.payload.get("target_turn_id"), "thread_id": event.thread_id,
        "resolved": resolution is not None, "resolution": _base(resolution) if resolution else None,
    }


def list_findings(repo: Repository, run_id: str) -> dict[str, Any]:
    repo.get_run(run_id)
    events = list(repo.session.scalars(select(Event).where(
        Event.run_id == run_id, Event.event_type.in_(FINDING_EVENTS)).order_by(Event.id)))
    resolutions = {event.payload["flag_event_id"]: event for event in events if event.event_type == FLAG_RESOLVED}
    return {
        "flags": [_flag_view(event, resolutions.get(event.id)) for event in events if event.event_type == FLAG_CREATED],
        "notes": [_base(event) for event in events if event.event_type == NOTE_CREATED],
    }


def flag(repo: Repository, *, target_type: str, target_id: str, author: str,
         body: str = "", tags: Sequence[str] = (), idempotency_key: str) -> dict[str, Any]:
    author, body, tags = _input(author, body, tags, idempotency_key)
    if not body.strip() and not tags:
        raise InvalidStateError("a flag requires a tag or a note")
    if target_type == "post":
        post = repo.get_post(target_id)
        thread = repo.get_thread(post.thread_id)
        turn = repo.session.scalar(select(Turn).where(Turn.resulting_post_id == post.id).order_by(Turn.started_at).limit(1))
        run_id, post_id, turn_id, agent_id = thread.run_id, post.id, turn.id if turn else None, post.author_agent_id
    elif target_type == "turn":
        turn = repo.get_turn(target_id)
        thread = repo.get_thread(turn.thread_id)
        run_id, post_id, turn_id, agent_id = turn.run_id, turn.resulting_post_id, turn.id, turn.agent_id
        if thread.run_id != run_id:
            raise InvalidStateError("turn and thread belong to different sessions")
    else:
        raise InvalidStateError("flags must target a post or turn")
    if not run_id:
        raise InvalidStateError("findings require a session")
    repo.get_run(run_id)
    fingerprint = digest({"operation": "flag", "target_type": target_type, "target_id": target_id,
                          "body": body, "tags": tags})
    event = _prior(repo, author, idempotency_key, fingerprint)
    if event is None:
        event = repo.add_event(FLAG_CREATED, run_id=run_id, thread_id=thread.id, post_id=post_id,
            agent_id=agent_id, actor_type="human", actor_id=author, payload={
                "target_type": target_type, "target_id": target_id, "target_post_id": post_id,
                "target_turn_id": turn_id, "body": body, "tags": tags,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "idempotency_key": idempotency_key, "request_sha256": fingerprint,
            })
    resolution = repo.session.scalar(select(Event).where(
        Event.event_type == FLAG_RESOLVED, Event.payload["flag_event_id"].as_integer() == event.id,
    ).order_by(Event.id).limit(1))
    return _flag_view(event, resolution)


def resolve_flag(repo: Repository, *, flag_event_id: int, author: str, body: str = "",
                 tags: Sequence[str] = (), idempotency_key: str) -> dict[str, Any]:
    author, body, tags = _input(author, body, tags, idempotency_key)
    original = repo.get_event(flag_event_id)
    if original.event_type != FLAG_CREATED or not original.run_id:
        raise InvalidStateError("resolution must reference a flag event")
    fingerprint = digest({"operation": "resolve", "flag_event_id": flag_event_id, "body": body, "tags": tags})
    resolution = _prior(repo, author, idempotency_key, fingerprint)
    if resolution is None:
        existing = repo.session.scalar(select(Event.id).where(
            Event.event_type == FLAG_RESOLVED, Event.payload["flag_event_id"].as_integer() == flag_event_id).limit(1))
        if existing is not None:
            raise InvalidStateError("flag is already resolved")
        resolution = repo.add_event(FLAG_RESOLVED, run_id=original.run_id, thread_id=original.thread_id,
            post_id=original.post_id, agent_id=original.agent_id, actor_type="human", actor_id=author,
            payload={"flag_event_id": flag_event_id, "body": body, "tags": tags,
                     "target_post_id": original.payload.get("target_post_id"),
                     "target_turn_id": original.payload.get("target_turn_id"),
                     "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                     "idempotency_key": idempotency_key, "request_sha256": fingerprint})
    return _flag_view(original, resolution)


def note(repo: Repository, *, run_id: str, author: str, body: str,
         tags: Sequence[str] = (), idempotency_key: str) -> dict[str, Any]:
    author, body, tags = _input(author, body, tags, idempotency_key)
    if not body.strip():
        raise InvalidStateError("a run note requires nonblank text")
    repo.get_run(run_id)
    fingerprint = digest({"operation": "note", "run_id": run_id, "body": body, "tags": tags})
    event = _prior(repo, author, idempotency_key, fingerprint)
    if event is None:
        event = repo.add_event(NOTE_CREATED, run_id=run_id, actor_type="human", actor_id=author, payload={
            "body": body, "tags": tags, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "idempotency_key": idempotency_key, "request_sha256": fingerprint,
        })
    return _base(event)
