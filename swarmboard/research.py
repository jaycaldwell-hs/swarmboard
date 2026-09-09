"""Research branches and attributed interventions over the existing ledger.

All writes use the caller's transaction. A fork owns new rows and never adds an
event to its source; inherited posts provide context without model usage.
"""
from __future__ import annotations

from copy import deepcopy
from math import ceil
from typing import Any, Mapping, Sequence
from uuid import uuid4

from sqlalchemy import select

from . import cadence, sessions
from .credentials import scrub_agent_settings
from .models import Agent, Event, Experiment, Post, Run, Stimulus, Thread, Turn, utc_now
from .repository import InvalidStateError, Repository
from .run_policy import normalize_config


_LIMITS = (
    "max_rounds", "max_posts", "max_tokens", "max_duration_seconds",
    "per_agent_quota", "per_thread_quota", "max_cascade_depth",
)
_RUNTIME_CONFIG = {
    "step_once", "discarded", "unavailable_agent_ids", "cadence_quiet",
    "cadence_cursor", "cadence_slot", "cadence_round", "cadence_next_index",
    "cadence_last_agent_id", "lineage", "post_id_map", "fork_roster",
    "sibling_group_id", "source_turn_id", "source_run_id", "rerun_of",
    "experiment", "inherited_agent_quota_remaining", "inherited_thread_quota_remaining",
}


def _author(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidStateError("an attributed human operator is required")
    return value.strip()


def _roster(repo: Repository, run: Run | None, agent_ids: Sequence[str] | None) -> list[Agent]:
    chosen = agent_ids if agent_ids is not None else (run.config.get("agent_ids") if run else None)
    if chosen is None:
        saved = repo.session.get(Experiment, run.id) if run else None
        chosen = [p["id"] for p in saved.manifest["participants"]] if saved else [a.id for a in repo.list_agents(enabled_only=True)]
    agents = [repo.get_agent(agent_id) for agent_id in dict.fromkeys(chosen)]
    if not agents or any(not agent.enabled or not agent.permissions.get("speak", agent.permissions.get("can_post", True)) for agent in agents):
        raise InvalidStateError("choose enabled participants with speaking permission")
    return agents


def _participant_snapshot(agent: Agent) -> dict[str, Any]:
    import hashlib

    return {
        "id": agent.id, "handle": agent.handle, "role": agent.role,
        "provider": agent.provider, "model": agent.model,
        "persona": agent.persona,
        "persona_sha256": hashlib.sha256(agent.persona.encode("utf-8")).hexdigest(),
        "settings": deepcopy(scrub_agent_settings(agent.settings)[0]),
        "permissions": deepcopy(agent.permissions), "cooldown_seconds": agent.cooldown_seconds,
    }


def _generated_posts(repo: Repository, run_id: str) -> list[Post]:
    return [post for post in repo.session.scalars(
        select(Post).join(Thread).where(Thread.run_id == run_id, Post.author_type == "agent")
    ) if not post.metadata_json.get("is_inherited")]


def _remaining_limits(repo, source, source_thread, cutoff, agents, actual_limits, config):
    for limit, spent in (("max_rounds", source.rounds_used), ("max_posts", source.posts_used),
                         ("max_tokens", source.tokens_used)):
        actual_limits[limit] = getattr(source, limit) - spent
    elapsed = 0.0
    if source.started_at:
        elapsed = source.virtual_time if source.config.get("use_virtual_time") else (
            (source.finished_at or utc_now()) - source.started_at
        ).total_seconds()
    actual_limits["max_duration_seconds"] = source.max_duration_seconds - ceil(max(0.0, elapsed))
    generated = _generated_posts(repo, source.id)
    prior_agents = source.config.get("inherited_agent_quota_remaining", {})
    remaining = {
        agent.id: max(0, min(source.per_agent_quota, prior_agents.get(agent.id, source.per_agent_quota))
                      - sum(post.author_agent_id == agent.id for post in generated))
        for agent in agents
    }
    prior_thread = source.config.get("inherited_thread_quota_remaining", source.per_thread_quota)
    thread_remaining = max(0, min(source.per_thread_quota, prior_thread)
                           - sum(post.thread_id == source_thread.id for post in generated))
    config["inherited_agent_quota_remaining"] = remaining
    config["inherited_thread_quota_remaining"] = thread_remaining
    if cutoff:
        depth = repo.session.scalar(select(Stimulus.cascade_depth).join(
            Turn, Turn.stimulus_id == Stimulus.id
        ).where(Turn.resulting_post_id == cutoff.id).limit(1)) or 0
        actual_limits["max_cascade_depth"] = max(0, source.max_cascade_depth - depth)
    if any(actual_limits[key] <= 0 for key in ("max_rounds", "max_posts", "max_tokens", "max_duration_seconds")):
        raise InvalidStateError("source has no remaining run budget; fork with fresh budgets")
    if not any(remaining.values()) or thread_remaining <= 0:
        raise InvalidStateError("source has no remaining participant or thread quota; fork with fresh budgets")


def fork(
    repo: Repository, *, thread_id: str, at_post_id: str | None, author: str,
    policy: Mapping[str, Any] | str | None = None, agent_ids: Sequence[str] | None = None,
    config_overrides: Mapping[str, Any] | None = None, limits: Mapping[str, int] | None = None,
    inherit_remaining: bool = False, continuous: bool = False,
    sibling_group_id: str | None = None, source_turn_id: str | None = None,
    queue_initial: bool = True,
) -> dict[str, Any]:
    """Copy an inclusive source prefix into a fresh research session.

    ``at_post_id=None`` is used internally for a turn that had empty context.
    API callers normally select an explicit post. Remaining budgets refer to
    current source consumption, not a reconstructed historical counter state.
    """
    author = _author(author)
    source_thread = repo.get_thread(thread_id)
    source = repo.get_run(source_thread.run_id) if source_thread.run_id else None
    cutoff = repo.get_post(at_post_id) if at_post_id else None
    if cutoff and cutoff.thread_id != thread_id:
        raise InvalidStateError("fork post belongs to another thread")
    if source and source.stop_reason and "safety_block" in source.stop_reason:
        raise InvalidStateError("provider safety block: this workflow cannot be retried")
    agents = _roster(repo, source, agent_ids)
    source_config = source.config if source else {}
    config = {key: deepcopy(value) for key, value in source_config.items()
              if key not in _RUNTIME_CONFIG and not key.startswith("cadence_")}
    overrides = dict(config_overrides or {})
    if overrides.get("session_type", "research") != "research":
        raise InvalidStateError("forks are research sessions")
    forbidden = set(overrides) & (_RUNTIME_CONFIG | {"agent_ids", "interaction_mode", "collaboration"})
    if forbidden or any(key.startswith("cadence_") for key in overrides):
        raise InvalidStateError("fork configuration contains reserved fields")
    config.update(deepcopy(overrides))
    config.update(session_type="research", collaboration=True, interaction_mode="autonomous",
                  agent_ids=[agent.id for agent in agents], title=source_thread.title)
    if policy is not None:
        config["policy"] = deepcopy(policy)
    try:
        config = normalize_config(config)
    except ValueError as exc:
        raise InvalidStateError(str(exc)) from None
    actual_limits = {key: getattr(source, key) for key in _LIMITS} if source else {
        "max_rounds": 100, "max_posts": 200, "max_tokens": 200_000,
        "max_duration_seconds": 1200, "per_agent_quota": 100,
        "per_thread_quota": 200, "max_cascade_depth": 100,
    }
    if inherit_remaining:
        if source is None:
            raise InvalidStateError("an unowned thread has no run budgets to inherit")
        _remaining_limits(repo, source, source_thread, cutoff, agents, actual_limits, config)
    for key, value in (limits or {}).items():
        if key not in _LIMITS or type(value) is not int or value < (0 if key == "max_cascade_depth" else 1):
            raise InvalidStateError("fork limits must be positive integers with a nonnegative cascade depth")
        actual_limits[key] = value
    cadence_mode = config.get("cadence", "free")
    if cadence_mode not in {"free", cadence.NAME}:
        raise InvalidStateError("unknown conversation cadence")
    if cadence_mode == cadence.NAME and (sum(a.handle == "ada" for a in agents) != 1 or len(agents) < 2):
        raise InvalidStateError("the Ada cadence requires Ada and at least one peer")

    parent = {
        "run_id": source.id if source else None, "thread_id": thread_id,
        "post_id": at_post_id, "at_sequence": cutoff.sequence if cutoff else 0,
    }
    previous_lineage = source_config.get("lineage", {})
    lineage = {
        "parent_run_id": parent["run_id"], "parent_thread_id": thread_id,
        "parent_post_id": at_post_id, "at_sequence": parent["at_sequence"],
        "author": author, "session_type": "research",
        "source_session_type": source_config.get("session_type", "collaboration"),
        "ancestors": [*deepcopy(previous_lineage.get("ancestors", [])), parent],
    }
    config.update(lineage=lineage, source_run_id=parent["run_id"],
                  fork_roster=[_participant_snapshot(agent) for agent in agents])
    if sibling_group_id:
        config["sibling_group_id"] = sibling_group_id
    if source_turn_id:
        config["source_turn_id"] = source_turn_id
    child = repo.create_run(seed=source.seed if source else 0, continuous=continuous,
                            config=config, **actual_limits)
    thread = repo.create_thread(run_id=child.id, title=source_thread.title,
                                actor_type="human", actor_id=author)
    source_posts = list(repo.session.scalars(select(Post).where(
        Post.thread_id == thread_id, Post.sequence <= parent["at_sequence"]
    ).order_by(Post.sequence)))
    post_map: dict[str, str] = {}
    latest = None
    for source_post in source_posts:
        inherited = {
            "is_inherited": True, "inherited_from_post_id": source_post.id,
            "inherited_from_thread_id": source_thread.id,
            "inherited_from_run_id": parent["run_id"], "inherited_by": author,
            "inherited_created_at": source_post.created_at.isoformat(),
        }
        latest = repo.create_post(
            thread_id=thread.id, body=source_post.body, author_type=source_post.author_type,
            author_handle=source_post.author_handle, author_agent_id=source_post.author_agent_id,
            parent_post_id=post_map.get(source_post.parent_post_id or ""), intent=source_post.intent,
            idempotency_key=f"fork:{child.id}:post:{source_post.id}",
            metadata={**deepcopy(source_post.metadata_json), **inherited},
            event_payload=inherited, operation="inherit",
        )
        post_map[source_post.id] = latest.post.id
    # Preserve aliases from earlier forks for reused prompts across ancestry.
    for ancestor_id, source_id in source_config.get("post_id_map", {}).items():
        if source_id in post_map:
            post_map[ancestor_id] = post_map[source_id]
    child.posts_used = 0
    child.config = {**child.config, "post_id_map": post_map}
    manifest = {
        "version": "swarm-session-v1", "opening": {
            "title": thread.title, "body": source_posts[0].body if source_posts else "",
            "author_handle": source_posts[0].author_handle if source_posts else author,
        },
        "participants": deepcopy(config["fork_roster"]), "cadence": config.get("cadence", "free"),
        "limits": actual_limits, "session_type": "research", "policy": deepcopy(config["policy"]),
        "lineage": lineage,
    }
    repo.session.add(Experiment(run_id=child.id, manifest=manifest,
                                manifest_sha256=sessions.digest(manifest), world={}))
    repo.session.flush()
    if queue_initial:
        repo.add_stimulus(
            run_id=child.id, thread_id=thread.id, kind="new_evidence",
            source_post_id=latest.post.id if latest else None,
            triggering_event_id=latest.event.id if latest else None,
            priority=5, dedupe_key=f"fork:{child.id}:opening",
            payload={"reason": "research_fork", "author": author},
        )
    result = {
        "run_id": child.id, "thread_id": thread.id, "lineage": lineage,
        "session_type": "research", "policy": deepcopy(config["policy"]),
        "post_id_map": post_map, "inherited_posts": len(source_posts),
        "sibling_group_id": sibling_group_id,
    }
    repo.add_event("research.forked", run_id=child.id, thread_id=thread.id,
                   actor_type="human", actor_id=author, payload={
                       **deepcopy(result), "inherit_remaining": inherit_remaining,
                       "limits": actual_limits, "source_turn_id": source_turn_id,
                   })
    return result


def force_turn(
    repo: Repository, *, run_id: str, agent_id: str, author: str,
    thread_id: str | None = None, stimulus_post_id: str | None = None,
    override_cooldown: bool = False, reuse_turn_id: str | None = None,
    idempotency_key: str | None = None,
) -> Stimulus:
    author = _author(author)
    fingerprint = sessions.digest({
        "run_id": run_id, "agent_id": agent_id, "thread_id": thread_id,
        "stimulus_post_id": stimulus_post_id, "override_cooldown": override_cooldown,
        "reuse_turn_id": reuse_turn_id,
    })
    dedupe = f"research:force:{run_id}:{sessions.digest([author, idempotency_key])}" if idempotency_key else None
    if dedupe:
        prior = repo.session.scalar(select(Stimulus).where(Stimulus.dedupe_key == dedupe))
        if prior:
            if prior.payload.get("request_sha256") != fingerprint:
                raise InvalidStateError("request key already used for another forced turn")
            return prior
    run = repo.get_run(run_id)
    sessions.require_runnable(run)
    if run.state in sessions.TERMINAL:
        raise InvalidStateError("cannot force a turn in a terminal run")
    agent = repo.get_agent(agent_id)
    roster = run.config.get("agent_ids")
    if roster is None:
        saved = repo.session.get(Experiment, run.id)
        roster = [item["id"] for item in saved.manifest["participants"]] if saved else [a.id for a in repo.list_agents()]
    if agent_id not in roster or agent_id in run.config.get("unavailable_agent_ids", []):
        raise InvalidStateError("forced participant is outside the available session roster")
    if not agent.enabled or not agent.permissions.get("speak", agent.permissions.get("can_post", True)):
        raise InvalidStateError("forced participant must be enabled and permitted to speak")
    source_post = repo.get_post(stimulus_post_id) if stimulus_post_id else None
    if thread_id is None and source_post:
        thread_id = source_post.thread_id
    if thread_id is None:
        thread_id = repo.session.scalar(select(Thread.id).where(
            Thread.run_id == run_id, Thread.status != "closed"
        ).order_by(Thread.latest_activity_at.desc(), Thread.id).limit(1))
    if not thread_id:
        raise InvalidStateError("session has no open thread for a forced turn")
    thread = repo.get_thread(thread_id)
    if thread.run_id != run_id or thread.status == "closed":
        raise InvalidStateError("forced thread must be open and belong to this session")
    if source_post and source_post.thread_id != thread.id:
        raise InvalidStateError("forced stimulus post belongs to another thread")
    if reuse_turn_id:
        original = repo.get_turn(reuse_turn_id)
        lineage = run.config.get("lineage", {})
        if original.agent_id != agent_id or original.thread_id != lineage.get("parent_thread_id"):
            raise InvalidStateError("reused turn must belong to this fork's source and selected participant")
        if original.prompt is None:
            raise InvalidStateError("original turn has no captured prompt to reuse")
    stimulus = repo.add_stimulus(
        run_id=run_id, thread_id=thread.id, kind="new_evidence", target_agent_id=agent_id,
        source_post_id=source_post.id if source_post else None, priority=1_000_000,
        dedupe_key=dedupe, payload={
            "forced": True, "forced_by": author, "override_cooldown": bool(override_cooldown),
            "reuse_turn_id": reuse_turn_id, "request_sha256": fingerprint,
        },
    )
    repo.add_event("research.turn_forced", run_id=run_id, thread_id=thread.id,
                   stimulus_id=stimulus.id, agent_id=agent_id, actor_type="human", actor_id=author,
                   payload={**deepcopy(stimulus.payload), "session_type": run.config.get("session_type", "collaboration"),
                            "policy": normalize_config(run.config)["policy"], "stimulus_post_id": stimulus_post_id})
    return stimulus


def resample(
    repo: Repository, *, turn_id: str, n: int = 1, reuse_prompt: bool = True,
    author: str, idempotency_key: str,
) -> dict[str, Any]:
    author = _author(author)
    if type(n) is not int or not 1 <= n <= 20:
        raise InvalidStateError("resample count must be between 1 and 20")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise InvalidStateError("resample requires an idempotency key")
    fingerprint = sessions.digest({"turn_id": turn_id, "n": n, "reuse_prompt": reuse_prompt})
    prior = repo.session.scalar(select(Event).where(
        Event.event_type == "research.resampled", Event.actor_id == author,
        Event.payload["idempotency_key"].as_string() == idempotency_key,
    ))
    if prior:
        if prior.payload.get("request_sha256") != fingerprint:
            raise InvalidStateError("request key already used for another resample")
        return deepcopy(prior.payload["result"])
    original = repo.get_turn(turn_id)
    if reuse_prompt and original.prompt is None:
        raise InvalidStateError("original turn has no captured prompt to reuse")
    context_cutoff = repo.session.scalar(select(Post).where(
        Post.thread_id == original.thread_id, Post.id.in_(original.context_post_ids)
    ).order_by(Post.sequence.desc()).limit(1))
    if context_cutoff is None and original.resulting_post_id:
        result_post = repo.get_post(original.resulting_post_id)
        if result_post.thread_id == original.thread_id:
            context_cutoff = repo.session.scalar(select(Post).where(
                Post.thread_id == original.thread_id, Post.sequence < result_post.sequence
            ).order_by(Post.sequence.desc()).limit(1))
    if original.agent_id not in {agent.id for agent in _roster(repo, repo.get_run(original.run_id) if original.run_id else None, None)}:
        raise InvalidStateError("original participant is outside the current source roster")
    group_id = str(uuid4())
    forks = []
    for index in range(n):
        child = fork(
            repo, thread_id=original.thread_id,
            at_post_id=context_cutoff.id if context_cutoff else None, author=author,
            sibling_group_id=group_id, source_turn_id=original.id, queue_initial=False,
        )
        forced = force_turn(
            repo, run_id=child["run_id"], thread_id=child["thread_id"], agent_id=original.agent_id,
            author=author, reuse_turn_id=original.id if reuse_prompt else None,
            idempotency_key=f"resample:{group_id}:{index}",
        )
        forks.append({**child, "stimulus_id": forced.id})
    result = {"sibling_group_id": group_id, "forks": forks}
    repo.add_event("research.resampled", run_id=forks[0]["run_id"], thread_id=forks[0]["thread_id"],
                   actor_type="human", actor_id=author, payload={
                       "idempotency_key": idempotency_key, "request_sha256": fingerprint,
                       "source_turn_id": turn_id, "reuse_prompt": reuse_prompt,
                       "result": deepcopy(result),
                   })
    return result
