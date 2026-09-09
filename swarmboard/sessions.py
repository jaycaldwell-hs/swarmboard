"""Collaboration sessions and read-only access to the retired experiment records."""
from __future__ import annotations

import copy
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from . import cadence
from .models import Agent, Experiment, Post, Run, Thread, Turn
from .repository import InvalidStateError, Repository
from .run_policy import adapt_agent, is_research, normalize_config, policy_for

TERMINAL = {"stopped", "completed", "failed", "emergency_stopped"}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def is_legacy_scripted(run: Run) -> bool:
    return bool(run.config.get("experiment") and run.config.get("interaction_mode") != "autonomous")


def require_runnable(run: Run) -> None:
    if is_legacy_scripted(run):
        raise InvalidStateError("scripted experiments are archived; create a collaboration session instead")
    if run.config.get("discarded"):
        raise InvalidStateError("this unused setup was removed")


def retire_scripted_runs(repo: Repository) -> None:
    """Stop obsolete workers without deleting their inputs, outputs, or audit."""
    for run in repo.session.scalars(select(Run).where(Run.state.not_in(TERMINAL))):
        if is_legacy_scripted(run):
            repo.control_run(run.id, "stop", reason="scripted experiments retired")


def configured_agent(config, agent):
    """Compose registered configuration and session overrides before scheduling."""
    overrides = config.get("agent_overrides", {}).get(agent.id, {})
    if overrides:
        current = {name: getattr(agent, name) for name in (
            "id", "handle", "role", "persona", "provider", "model", "enabled", "settings",
            "permissions", "cooldown_seconds", "last_spoke_at", "persona_version")}
        current.update({name: copy.deepcopy(overrides[name]) for name in (
            "provider", "model", "persona", "settings", "persona_version") if name in overrides})
        agent = SimpleNamespace(**current)
    return agent


def session_agent(session, run: Run | None, agent):
    agent = configured_agent(run.config if run else {}, agent)
    if run is None or run.config.get("interaction_mode") != "autonomous":
        return adapt_agent(run, agent)
    saved = session.get(Experiment, run.id)
    if saved is None or agent.id not in {p["id"] for p in saved.manifest["participants"]}:
        raise InvalidStateError("participant is outside this session")
    # Configuration changes apply to future turns. Captured historical turns stay intact.
    last_spoke = None
    cooldown = 0
    if is_research(run):
        cooldown = agent.cooldown_seconds
        last_spoke = session.scalar(select(Post.created_at).join(Turn, Turn.resulting_post_id == Post.id)
                                   .where(Turn.run_id == run.id, Turn.agent_id == agent.id)
                                   .order_by(Post.created_at.desc()).limit(1))
    return adapt_agent(run, SimpleNamespace(
        id=agent.id, handle=agent.handle, role=agent.role, persona=agent.persona,
        provider=agent.provider, model=agent.model, enabled=agent.enabled,
        settings=copy.deepcopy(agent.settings or {}), permissions=dict(agent.permissions or {}),
        cooldown_seconds=cooldown, last_spoke_at=last_spoke,
        persona_version=getattr(agent, "persona_version", agent.settings.get("persona_version", 1)),
    ))


def create_session(repo: Repository, *, agents, title="Open board", body="Open-ended interaction",
                   seed=None, limits=None, continuous=True, cadence_mode="free", source_run_id=None,
                   author_handle="human", session_type="collaboration", policy="production") -> Run:
    from .autonomy import seed_invitation

    normalized = normalize_config({"session_type": session_type, "policy": policy})
    agents = list({a.id: a for a in agents}.values())
    if not agents or any(not a.enabled or not a.permissions.get("speak", True) for a in agents):
        raise InvalidStateError("choose at least one enabled participant with speaking permission")
    if cadence_mode not in ("free", cadence.NAME):
        raise InvalidStateError("unknown conversation cadence")
    if cadence_mode == cadence.NAME and (sum(a.handle == "ada" for a in agents) != 1 or len(agents) < 2):
        raise InvalidStateError("the Ada cadence requires Ada and at least one peer")

    actual_limits = {"max_rounds": 100, "max_tokens": 200_000,
                     "max_duration_seconds": 1200, **(limits or {})}
    actual_limits.setdefault("max_posts", actual_limits["max_rounds"] + 1)
    actual_limits.update(max_cascade_depth=actual_limits["max_rounds"],
                         per_agent_quota=actual_limits["max_rounds"],
                         per_thread_quota=actual_limits["max_posts"])
    config = {"collaboration": True, "interaction_mode": "autonomous",
              "agent_ids": [a.id for a in agents], "max_agents_per_stimulus": 1,
              "model_retries": 0, "cadence": cadence_mode, "title": title, **normalized}
    if source_run_id:
        config["source_run_id"] = source_run_id
    run = repo.create_run(seed=seed if seed is not None else uuid4().int % (2 ** 31),
                          continuous=continuous, config=config, **actual_limits)
    thread = repo.create_thread(run_id=run.id, title=title)
    manifest = {"version": "swarm-session-v1", "opening": {"title": title, "body": body, "author_handle": author_handle},
                "participants": [{"id": a.id, "handle": a.handle, "provider": a.provider, "model": a.model}
                                 for a in agents],
                "cadence": cadence_mode, "limits": actual_limits,
                "session_type": normalized["session_type"], "policy": copy.deepcopy(normalized["policy"])}
    # Keep the existing table name/schema so old conversations need no destructive migration.
    repo.session.add(Experiment(run_id=run.id, manifest=manifest,
                                manifest_sha256=digest(manifest), world={}))
    repo.session.flush()
    opening = repo.create_post(thread_id=thread.id, body=body, author_type="human",
                               author_handle=author_handle, metadata={"session_input": True},
                               idempotency_key=f"session:{run.id}:opening")
    seed_invitation(repo, run, thread, opening.post, key=f"session:{run.id}:opening")
    repo.add_event("session.prepared", run_id=run.id, thread_id=thread.id,
                   actor_type="human", actor_id=author_handle,
                   payload={"source_run_id": source_run_id, **normalized})
    return run


def restart(repo: Repository, original: Run, *, seed=None, continuous=None) -> Run:
    require_runnable(original)
    if original.stop_reason and "safety_block" in original.stop_reason:
        raise InvalidStateError("provider safety block: this workflow cannot be retried")
    saved = repo.session.get(Experiment, original.id)
    if saved is None:
        raise InvalidStateError("original session has no saved setup")
    manifest = saved.manifest
    opening = manifest.get("opening") or {"title": manifest["scenario"]["name"],
                                          "body": manifest["scenario"]["opening"]}
    # Fetch in the saved order, not repository sort order. Never regrant revoked permissions.
    agents = [repo.get_agent(p["id"]) for p in manifest["participants"]]
    return create_session(repo, agents=agents, title=opening["title"], body=opening["body"],
                          seed=seed, limits=manifest.get("limits"),
                          continuous=False if continuous is None else continuous,
                          cadence_mode=manifest.get("cadence", original.config.get("cadence", "free")),
                          source_run_id=original.id, author_handle=opening.get("author_handle", "human"),
                          session_type=original.config.get("session_type", "collaboration"),
                          policy=policy_for(original))


def prepare_session(repo: Repository, run: Run) -> None:
    if run.config.get("interaction_mode") != "autonomous":
        return
    unavailable = set(run.config.get("unavailable_agent_ids", []))
    available = [a for a in repo.list_agents(enabled_only=True, agent_ids=run.config["agent_ids"])
                 if a.id not in unavailable and a.permissions.get("speak", True)]
    if not available:
        repo.set_run_state(run.id, "failed", reason="no participants are available")
    elif repo.session.scalar(select(Thread.id).where(Thread.run_id == run.id, Thread.status != "closed").limit(1)) is None:
        repo.set_run_state(run.id, "completed", reason="all threads closed")
    elif cadence.enabled(run):
        cadence.ensure_slot(repo, run)


def activity(repo: Repository, run_id: str) -> dict:
    run = repo.get_run(run_id)
    saved = repo.session.get(Experiment, run_id)
    if saved is None:
        raise InvalidStateError("this run has no saved session setup")
    archived = is_legacy_scripted(run)
    manifest = saved.manifest
    title = run.config.get("title") or manifest.get("opening", {}).get("title") or manifest.get("scenario", {}).get("name", "Session")
    unavailable = set(run.config.get("unavailable_agent_ids", []))
    participants = []
    for snapshot in manifest["participants"]:
        current = repo.session.get(Agent, snapshot["id"])
        # Active sessions use live configuration; archived records retain their recorded roster.
        identity = session_agent(repo.session, run, current) if current is not None and run.state not in TERMINAL and not archived else SimpleNamespace(**snapshot)
        participants.append({"id": snapshot["id"], "handle": identity.handle,
                             "provider": identity.provider, "model": identity.model,
                             "available": bool(not archived and run.state not in TERMINAL and current
                                               and current.enabled and current.permissions.get("speak", True)
                                               and current.id not in unavailable)})
    turns = list(repo.session.scalars(select(Turn).where(Turn.run_id == run_id).order_by(Turn.started_at, Turn.id)))
    handles = {p["id"]: p["handle"] for p in participants}
    actions = []
    for turn in turns:
        post = repo.session.get(Post, turn.resulting_post_id) if turn.resulting_post_id else None
        action = turn.validated_action or {}
        actions.append({"turn_id": turn.id, "handle": post.author_handle if post else handles.get(turn.agent_id, "unknown"),
                        "kind": action.get("action", turn.state), "state": turn.state,
                        "resulting_post_id": turn.resulting_post_id,
                        "thread_id": post.thread_id if post else turn.thread_id, "error": turn.error})
    return {"run_id": run.id, "title": title, "state": run.state, "stop_reason": run.stop_reason,
            "archived": archived, "participants": participants,
            "session_type": run.config.get("session_type", "collaboration"), "policy": policy_for(run),
            "cadence": {**cadence.context(repo, run), "quiet": run.config.get("cadence_quiet", False)} if cadence.enabled(run) else None,
            "metrics": {"turns_used": run.rounds_used, "tokens_used": run.tokens_used,
                        "threads": len(repo.list_threads(run_id=run_id, limit=10000)),
                        "new_thread": sum(a["kind"] == "new_thread" for a in actions),
                        "pass": sum(t.state == "passed" for t in turns),
                        "failed_turns": sum(t.state == "failed" for t in turns)},
            "actions": actions, "turn_ids": [t.id for t in turns]}
