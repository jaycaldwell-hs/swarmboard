"""Separate participant perception from the attributed researcher ledger."""
from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace

from .persona_context import persona_snapshot


def text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def persona_identity(agent) -> dict:
    snapshot = persona_snapshot(agent.settings or {})
    version = getattr(agent, "persona_version", None) or agent.settings.get("persona_version", 1)
    if snapshot is not None:
        return {"version": version, "sha256": snapshot.digest, "kind": "persona_files", "files": snapshot.file_manifest}
    return {"version": version, "sha256": text_hash(agent.persona), "kind": "persona_text"}


def agent_snapshot(agent) -> dict:
    return {"id": agent.id, "handle": agent.handle, "persona": persona_identity(agent),
            "configuration": {"provider": agent.provider, "model": agent.model, "persona": agent.persona,
                              "settings": copy.deepcopy(dict(agent.settings or {}))}}


def participant_post(post):
    metadata = getattr(post, "metadata_json", {}) or {}
    if not (metadata.get("is_impersonation") or metadata.get("is_system_notice")):
        return post
    fields = {name: getattr(post, name) for name in ("id", "thread_id", "parent_post_id", "author_type",
        "author_agent_id", "author_handle", "body", "sequence", "intent", "created_at", "metadata_json")}
    fields["author_type"] = "system" if metadata.get("is_system_notice") else "agent"
    fields["author_handle"] = "SYSTEM" if metadata.get("is_system_notice") else metadata["displayed_as_agent"]
    fields["author_agent_id"] = metadata.get("displayed_as_agent_id")
    return SimpleNamespace(**fields)


def participant_posts(posts):
    return [participant_post(post) for post in posts]


def intervention_snapshot(repo, run, agent, posts, *, at_event_id=None):
    from .interventions import active_instructions, list_interventions
    state = list_interventions(repo, run.id, at_event_id=at_event_id)
    instructions = active_instructions(repo, run.id, agent.id, at_event_id=at_event_id)
    memory_ids = {m["id"] for m in state["memories"] if m.get("agent_id") == agent.id and m.get("active")}
    return {
        "private_instructions": instructions,
        "config_changes": [item for item in state["config_changes"] if item.get("agent_id") == agent.id
                           and (at_event_id is None or item.get("event_id", item.get("id", 0)) <= at_event_id)],
        "memories": [item for item in state["memories"] if item["id"] in memory_ids],
        "impersonations": [{"post_id": p.id, **copy.deepcopy(p.metadata_json)} for p in posts
                           if p.metadata_json.get("is_impersonation") or p.metadata_json.get("is_system_notice")],
    }
