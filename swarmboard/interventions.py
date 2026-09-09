"""Attributed human interventions with immutable instruction and memory history."""
from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from . import autonomy, cadence, sessions
from .credentials import redact, validate_agent_settings, validate_hosted_provider
from .models import Agent, Event, Experiment, Memory, Post, Run, normalize_agent_handle
from .persona_context import persona_snapshot
from .repository import InvalidStateError, Repository
from .stimuli import plan_reactive_stimuli


INSTRUCTION_CREATED = "instruction.created"
INSTRUCTION_REVOKED = "instruction.revoked"
MEMORY_SEEDED = "memory.seeded"
MEMORY_DEACTIVATED = "memory.deactivated"
CONFIG_CHANGED = "agent.config_changed"
REQUEST_RECORDED = "research.intervention"


def _hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _input(author, idempotency_key):
    if not isinstance(author, str) or not author.strip():
        raise InvalidStateError("an attributed human operator is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise InvalidStateError("interventions require an idempotency key")
    return author.strip()


def _body(value, *, maximum=100_000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"body must be nonblank text of at most {maximum} characters")
    return value


def _run(repo, run_id):
    run = repo.get_run(run_id)
    sessions.require_runnable(run)
    if run.state in sessions.TERMINAL:
        raise InvalidStateError("terminal sessions cannot accept interventions; fork instead")
    return run


def _participant(repo, run, agent_id):
    agent = repo.get_agent(agent_id)
    ids = run.config.get("agent_ids")
    if ids is None:
        saved = repo.session.get(Experiment, run.id)
        ids = [person["id"] for person in saved.manifest["participants"]] if saved else [a.id for a in repo.list_agents()]
    if agent.id not in ids:
        raise InvalidStateError("participant is outside this session")
    return agent


def _prior(repo, author, key, fingerprint):
    prior = repo.session.scalar(select(Event).where(
        Event.event_type == REQUEST_RECORDED, Event.actor_id == author,
        Event.payload["idempotency_key"].as_string() == key,
    ).order_by(Event.id).limit(1))
    if prior and prior.payload["request_sha256"] != fingerprint:
        raise InvalidStateError("request key already used for another intervention")
    return deepcopy(prior.payload["result"]) if prior else None


def _record(repo, *, run_id, author, key, fingerprint, result, thread_id=None, agent_id=None):
    repo.add_event(REQUEST_RECORDED, run_id=run_id, thread_id=thread_id, agent_id=agent_id,
                   actor_type="human", actor_id=author, payload={
                       "idempotency_key": key, "request_sha256": fingerprint,
                       "result": deepcopy(result),
                   })
    return result


def _base(event):
    return {"id": event.id, "event_id": event.id, "run_id": event.run_id,
            "agent_id": event.agent_id, "author": event.actor_id,
            "created_at": event.created_at.isoformat()}


def _events(repo, run_id, types, at_event_id=None):
    query = select(Event).where(Event.run_id == run_id, Event.event_type.in_(types)).order_by(Event.id)
    if at_event_id is not None:
        query = query.where(Event.id <= at_event_id)
    return list(repo.session.scalars(query))


def _instruction_views(repo, run_id, *, at_event_id=None):
    events = _events(repo, run_id, (INSTRUCTION_CREATED, INSTRUCTION_REVOKED), at_event_id)
    revoked = {event.payload["instruction_id"]: event for event in events if event.event_type == INSTRUCTION_REVOKED}
    return [
        {**_base(event), "body": event.payload["body"], "body_sha256": event.payload["body_sha256"],
         "revoked": event.id in revoked, "revocation": _base(revoked[event.id]) if event.id in revoked else None}
        for event in events if event.event_type == INSTRUCTION_CREATED
    ]


def active_instructions(repo, run_id, agent_id, at_event_id=None):
    """Return only the target participant's unrevoked instructions at the cutoff."""
    return [instruction for instruction in _instruction_views(repo, run_id, at_event_id=at_event_id)
            if instruction["agent_id"] == agent_id and not instruction["revoked"]]


def inactive_memory_ids(repo, run_id, at_event_id=None):
    """Fold memory lifecycle events; future seeds are hidden in historical views."""
    inactive = set(repo.session.scalars(select(Memory.id).where(Memory.run_id == run_id, Memory.active.is_(False))))
    all_events = _events(repo, run_id, ("memory.created", MEMORY_SEEDED, MEMORY_DEACTIVATED))
    for event in all_events:
        if at_event_id is not None and event.id > at_event_id:
            if event.event_type in {"memory.created", MEMORY_SEEDED}:
                inactive.add(event.payload["memory_id"])
            continue
        if event.event_type == MEMORY_DEACTIVATED or (event.event_type == MEMORY_SEEDED and not event.payload.get("active", True)):
            inactive.add(event.payload["memory_id"])
        if event.event_type == MEMORY_SEEDED and event.payload.get("replaces_memory_id"):
            inactive.add(event.payload["replaces_memory_id"])
    return inactive


def list_interventions(repo, run_id, *, at_event_id=None):
    repo.get_run(run_id)
    instructions = _instruction_views(repo, run_id, at_event_id=at_event_id)
    inactive = inactive_memory_ids(repo, run_id, at_event_id=at_event_id)
    events = _events(repo, run_id, (MEMORY_SEEDED, CONFIG_CHANGED, "post.created"), at_event_id)
    memories, changes, impersonations = [], [], []
    for event in events:
        payload = event.payload
        if event.event_type == MEMORY_SEEDED:
            memories.append({**_base(event), **deepcopy(payload), "id": payload["memory_id"],
                             "active": payload["memory_id"] not in inactive})
        elif event.event_type == CONFIG_CHANGED:
            changes.append({**_base(event), **deepcopy(payload)})
        elif payload.get("is_impersonation") or payload.get("is_system_notice"):
            post = repo.session.get(Post, event.post_id)
            impersonations.append({**_base(event), **deepcopy(payload), "post_id": event.post_id,
                                   "thread_id": event.thread_id, "body": post.body if post else None})
    return {"instructions": instructions, "memories": memories,
            "config_changes": changes, "impersonations": impersonations}


def research_post(repo, *, thread_id, body, author, as_handle=None, system_author=False,
                  parent_post_id=None, idempotency_key):
    author = _input(author, idempotency_key)
    body = _body(body, maximum=50_000)
    if bool(as_handle) == bool(system_author):
        raise ValueError("choose either an impersonated handle or a system author")
    displayed = None
    if as_handle:
        displayed = normalize_agent_handle(as_handle)
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", displayed) or displayed == "system":
            raise ValueError("as_handle must be a valid participant handle; use system_author for notices")
    fingerprint = sessions.digest({"operation": "research_post", "thread_id": thread_id, "body": body,
                                    "as_handle": displayed, "system_author": system_author,
                                    "parent_post_id": parent_post_id})
    prior = _prior(repo, author, idempotency_key, fingerprint)
    if prior is not None:
        return prior
    thread = repo.get_thread(thread_id)
    if not thread.run_id:
        raise InvalidStateError("research posts require a research session")
    run = _run(repo, thread.run_id)
    if run.config.get("session_type") != "research":
        raise InvalidStateError("impersonation and system posts require a research session")
    displayed_agent = repo.session.scalar(select(Agent).where(Agent.handle == displayed)) if displayed else None
    metadata = {
        "author_human": author, "displayed_as_agent": displayed,
        "displayed_as_agent_id": displayed_agent.id if displayed_agent else None,
        "is_impersonation": not system_author, "is_system_notice": bool(system_author),
        "displayed_author_type": "system" if system_author else "agent",
        "session_type": "research",
    }
    result = repo.create_post(
        thread_id=thread_id, body=body, parent_post_id=parent_post_id,
        author_type="human", author_handle=author, author_agent_id=None,
        idempotency_key=f"intervention:post:{sessions.digest([author, idempotency_key])}",
        metadata=metadata, event_payload=metadata, operation="research_post",
    )
    stimulus_ids = []
    if cadence.enabled(run):
        stimulus = cadence.on_input(repo, run)
        stimulus_ids = [stimulus.id] if stimulus else []
    else:
        parent = repo.get_post(parent_post_id) if parent_post_id else None
        parent_agent_id = (parent.metadata_json.get("displayed_as_agent_id") or parent.author_agent_id) if parent else None
        plans = plan_reactive_stimuli(
            result.post.body, repo.list_agents(enabled_only=True, agent_ids=run.config.get("agent_ids")),
            default_kind="human_post" if system_author else "agent_post", default_priority=5,
            exclude_agent_id=None if autonomy.enabled(run) else (displayed_agent.id if displayed_agent else None),
            reply_to_agent_id=parent_agent_id,
        )
        for plan in plans:
            stimulus = repo.add_stimulus(
                run_id=run.id, thread_id=thread.id, kind=plan.kind,
                target_agent_id=plan.target_agent_id, source_post_id=result.post.id,
                triggering_event_id=result.event.id, priority=plan.priority, payload=plan.payload,
                dedupe_key=f"run:{run.id}:post:{result.post.id}:{plan.dedupe_label}",
            )
            stimulus_ids.append(stimulus.id)
    output = {"run_id": run.id, "thread_id": thread.id, "post_id": result.post.id,
              "post": {"id": result.post.id, "thread_id": thread.id, "body": result.post.body,
                       "author_type": "human", "author_handle": author, "author_agent_id": None,
                       "parent_post_id": parent_post_id, "metadata": metadata, "sequence": result.post.sequence},
              "stimulus_ids": stimulus_ids, "created": result.created}
    return _record(repo, run_id=run.id, thread_id=thread.id, author=author,
                    key=idempotency_key, fingerprint=fingerprint, result=output)


def create_instruction(repo, *, run_id, agent_id, body, author, idempotency_key):
    author = _input(author, idempotency_key)
    body = _body(body)
    fingerprint = sessions.digest({"operation": "instruction", "run_id": run_id, "agent_id": agent_id, "body": body})
    prior = _prior(repo, author, idempotency_key, fingerprint)
    if prior is not None:
        return prior
    run = _run(repo, run_id)
    _participant(repo, run, agent_id)
    event = repo.add_event(INSTRUCTION_CREATED, run_id=run_id, agent_id=agent_id,
                           actor_type="human", actor_id=author,
                           payload={"body": body, "body_sha256": _hash(body), "target_agent_id": agent_id,
                                    "session_type": run.config.get("session_type", "collaboration")})
    result = {**_base(event), "body": body, "body_sha256": _hash(body), "revoked": False, "revocation": None}
    return _record(repo, run_id=run_id, agent_id=agent_id, author=author,
                    key=idempotency_key, fingerprint=fingerprint, result=result)


def revoke_instruction(repo, *, instruction_id, author, idempotency_key):
    author = _input(author, idempotency_key)
    fingerprint = sessions.digest({"operation": "revoke_instruction", "instruction_id": instruction_id})
    prior = _prior(repo, author, idempotency_key, fingerprint)
    if prior is not None:
        return prior
    instruction = repo.get_event(instruction_id)
    if instruction.event_type != INSTRUCTION_CREATED or not instruction.run_id:
        raise InvalidStateError("revocation must target a private instruction")
    _run(repo, instruction.run_id)
    if repo.session.scalar(select(Event.id).where(Event.event_type == INSTRUCTION_REVOKED,
            Event.payload["instruction_id"].as_integer() == instruction_id).limit(1)) is not None:
        raise InvalidStateError("instruction is already revoked")
    event = repo.add_event(INSTRUCTION_REVOKED, run_id=instruction.run_id, agent_id=instruction.agent_id,
                           actor_type="human", actor_id=author,
                           payload={"instruction_id": instruction_id, "body_sha256": instruction.payload["body_sha256"]})
    result = {**_base(event), "instruction_id": instruction_id, "revoked": True}
    return _record(repo, run_id=instruction.run_id, agent_id=instruction.agent_id, author=author,
                    key=idempotency_key, fingerprint=fingerprint, result=result)


def _configuration(run, agent):
    current = {"provider": agent.provider, "model": agent.model, "persona": agent.persona,
               "settings": deepcopy(agent.settings),
               "persona_version": agent.persona_version}
    current.update(deepcopy(run.config.get("agent_overrides", {}).get(agent.id, {})))
    return current


def _persona_hash(configuration):
    snapshot = persona_snapshot(configuration["settings"])
    return snapshot.digest if snapshot else _hash(configuration["persona"])


def change_configuration(repo, *, run_id, agent_id, author, idempotency_key, **changes):
    author = _input(author, idempotency_key)
    if not changes or set(changes) - {"provider", "model", "persona", "settings"}:
        raise ValueError("configuration updates require provider, model, persona, or settings")
    fingerprint = sessions.digest({"operation": "configuration", "run_id": run_id, "agent_id": agent_id, "changes": changes})
    prior = _prior(repo, author, idempotency_key, fingerprint)
    if prior is not None:
        return prior
    run = _run(repo, run_id)
    agent = _participant(repo, run, agent_id)
    before = _configuration(run, agent)
    after = {**deepcopy(before), **deepcopy(changes)}
    if not isinstance(after["provider"], str) or not after["provider"]:
        raise ValueError("provider must be specified")
    if not isinstance(after["model"], str) or not after["model"].strip() or len(after["model"]) > 255:
        raise ValueError("model must be nonblank text of at most 255 characters")
    _body(after["persona"], maximum=100_000)
    if not isinstance(after["settings"], Mapping):
        raise ValueError("settings must be an object")
    try:
        previous_persona = persona_snapshot(before["settings"])
        if "persona" in changes and previous_persona is not None:
            # Ada's generic persona field is absent from her prompt. Replace the
            # captured memory content while preserving the authored instructions.
            after["settings"]["persona_harness"] = {
                **previous_persona.model_dump(), "memory": after["persona"],
                "source": f"session-override:{run_id}",
            }
        after["settings"] = validate_agent_settings(after["settings"])
        validate_hosted_provider(after["provider"], after["settings"])
        persona_snapshot(after["settings"])
        before["persona_sha256"] = _persona_hash(before)
        after["persona_sha256"] = _persona_hash(after)
    except ValueError as exc:
        # Validation errors must not echo private persona files or credentials.
        if exc.__class__.__name__ == "ValidationError":
            raise ValueError("invalid captured persona snapshot") from None
        raise
    before.setdefault("persona_version", 1)
    after["persona_version"] = before["persona_version"] + (after["persona_sha256"] != before["persona_sha256"])
    run.config = {**run.config, "agent_overrides": {
        **deepcopy(run.config.get("agent_overrides", {})), agent_id: after,
    }, "unavailable_agent_ids": [candidate for candidate in run.config.get("unavailable_agent_ids", [])
                                if candidate != agent_id]}
    repo.session.flush()
    event = repo.add_event(CONFIG_CHANGED, run_id=run_id, agent_id=agent_id,
                           actor_type="human", actor_id=author, payload={
                               "before": redact(before), "after": redact(after),
                               "persona_sha256": after["persona_sha256"], "persona_version": after["persona_version"],
                               "session_type": run.config.get("session_type", "collaboration"),
                               "fields": sorted(changes),
                           })
    result = {**_base(event), **deepcopy(event.payload)}
    return _record(repo, run_id=run_id, agent_id=agent_id, author=author,
                    key=idempotency_key, fingerprint=fingerprint, result=result)


def seed_memory(repo, *, run_id, agent_id, body, author, idempotency_key,
                tags: Sequence[str] = (), active=True, replaces_memory_id=None):
    author = _input(author, idempotency_key)
    body = _body(body).strip()
    if not isinstance(tags, (list, tuple)) or len(tags) > 50 or any(
        not isinstance(tag, str) or not tag.strip() or len(tag) > 100 for tag in tags
    ):
        raise ValueError("memory tags must be nonblank strings of at most 100 characters")
    tags = list(dict.fromkeys(tag.strip() for tag in tags))
    if type(active) is not bool:
        raise ValueError("active must be a boolean")
    fingerprint = sessions.digest({"operation": "memory", "run_id": run_id, "agent_id": agent_id,
                                    "body": body, "tags": tags, "active": active,
                                    "replaces_memory_id": replaces_memory_id})
    prior = _prior(repo, author, idempotency_key, fingerprint)
    if prior is not None:
        return prior
    run = _run(repo, run_id)
    _participant(repo, run, agent_id)
    version = 1
    if replaces_memory_id:
        previous = repo.session.get(Memory, replaces_memory_id)
        if previous is None or previous.run_id != run_id or previous.agent_id != agent_id:
            raise InvalidStateError("replaced memory must belong to the same session and participant")
        prior_seed = repo.session.scalar(select(Event).where(Event.event_type == MEMORY_SEEDED,
            Event.payload["memory_id"].as_string() == replaces_memory_id).order_by(Event.id).limit(1))
        version = (prior_seed.payload.get("version", 1) if prior_seed else 1) + 1
    memory = repo.create_memory(claim=body, run_id=run_id, agent_id=agent_id, tags=tags)
    event = repo.add_event(MEMORY_SEEDED, run_id=run_id, agent_id=agent_id,
                           actor_type="human", actor_id=author, payload={
                               "memory_id": memory.id, "body": memory.claim, "body_sha256": _hash(memory.claim),
                               "tags": tags, "active": active, "version": version,
                               "replaces_memory_id": replaces_memory_id,
                           })
    result = {**_base(event), **deepcopy(event.payload), "id": memory.id}
    return _record(repo, run_id=run_id, agent_id=agent_id, author=author,
                    key=idempotency_key, fingerprint=fingerprint, result=result)


def deactivate_memory(repo, *, memory_id, author, idempotency_key):
    author = _input(author, idempotency_key)
    fingerprint = sessions.digest({"operation": "deactivate_memory", "memory_id": memory_id})
    prior = _prior(repo, author, idempotency_key, fingerprint)
    if prior is not None:
        return prior
    memory = repo.session.get(Memory, memory_id)
    if memory is None or not memory.run_id:
        raise InvalidStateError("memory must belong to a session")
    _run(repo, memory.run_id)
    if memory_id in inactive_memory_ids(repo, memory.run_id):
        raise InvalidStateError("memory is already inactive")
    event = repo.add_event(MEMORY_DEACTIVATED, run_id=memory.run_id, agent_id=memory.agent_id,
                           actor_type="human", actor_id=author,
                           payload={"memory_id": memory.id, "body_sha256": _hash(memory.claim)})
    result = {**_base(event), "id": memory.id, "memory_id": memory.id, "active": False}
    return _record(repo, run_id=memory.run_id, agent_id=memory.agent_id, author=author,
                    key=idempotency_key, fingerprint=fingerprint, result=result)
