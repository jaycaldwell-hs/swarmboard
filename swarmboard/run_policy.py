"""Validated per-run research controls, separate from immutable ledger rules."""
from __future__ import annotations

import copy
import math
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, Mapping


PRODUCTION = {
    "profile": "production", "dedup": True, "loop_prevention": True,
    "cooldowns": True, "cooldown_seconds": None, "dormancy": True,
    "consecutive_turn_cap": 1, "schema_mode": "strict",
}
PERMISSIVE = {
    "profile": "permissive", "dedup": False, "loop_prevention": False,
    "cooldowns": False, "cooldown_seconds": None, "dormancy": False,
    "consecutive_turn_cap": None, "schema_mode": "capture",
}
PRESETS = {"production": PRODUCTION, "permissive": PERMISSIVE}


def normalize_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy a run config and canonicalize its immutable type and policy.

    Unrelated run settings are retained. Policy overrides are explicit custom
    profiles; collaboration accepts only the unmodified production preset.
    """
    if config is not None and not isinstance(config, Mapping):
        raise ValueError("run config must be an object")
    result = copy.deepcopy(dict(config or {}))
    session_type = result.get("session_type", "collaboration")
    if session_type not in ("collaboration", "research"):
        raise ValueError("session_type must be collaboration or research")
    supplied = result.get("policy", "production")
    if isinstance(supplied, str):
        if supplied not in PRESETS:
            raise ValueError("policy preset must be production or permissive")
        policy = dict(PRESETS[supplied])
    elif isinstance(supplied, Mapping):
        unknown = set(supplied) - set(PRODUCTION)
        if unknown:
            raise ValueError("unknown policy fields: " + ", ".join(sorted(str(k) for k in unknown)))
        profile = supplied.get("profile", "production")
        if profile not in ("production", "permissive", "custom"):
            raise ValueError("policy profile must be production, permissive, or custom")
        baseline = PRESETS.get(profile, PRODUCTION)
        policy = {**baseline, **dict(supplied)}
        if profile == "custom" or any(policy[key] != baseline[key] for key in policy if key != "profile"):
            policy["profile"] = "custom"
    else:
        raise ValueError("policy must be a preset name or an object")

    for key in ("dedup", "loop_prevention", "cooldowns", "dormancy"):
        if type(policy[key]) is not bool:
            raise ValueError(f"policy {key} must be a boolean")
    seconds = policy["cooldown_seconds"]
    if seconds is not None and (
        isinstance(seconds, bool) or not isinstance(seconds, (int, float))
        or not math.isfinite(seconds) or seconds < 0
    ):
        raise ValueError("policy cooldown_seconds must be a finite nonnegative number or null")
    cap = policy["consecutive_turn_cap"]
    if cap is not None and (type(cap) is not int or cap < 1):
        raise ValueError("policy consecutive_turn_cap must be a positive integer or null")
    if policy["schema_mode"] not in ("strict", "capture"):
        raise ValueError("policy schema_mode must be strict or capture")
    if session_type == "collaboration" and policy != PRODUCTION:
        raise ValueError("collaboration sessions require the production policy")
    result["session_type"] = session_type
    result["policy"] = policy
    return result


def policy_for(run) -> dict[str, Any]:
    return normalize_config(getattr(run, "config", None))["policy"]


def is_research(run) -> bool:
    return bool(run and run.config.get("session_type") == "research")


def effective_scheduler_config(run, base):
    """Apply research fences while leaving legacy collaboration behavior intact."""
    if not is_research(run):
        return base
    policy = policy_for(run)
    return replace(base, ping_pong_limit=base.ping_pong_limit if policy["loop_prevention"] else 0,
                   allow_consecutive_posts=True, consecutive_turn_cap=policy["consecutive_turn_cap"],
                   cooldowns=policy["cooldowns"], dormancy=policy["dormancy"])


def effective_action_config(run, base):
    if not is_research(run):
        return base
    policy = policy_for(run)
    return replace(base, max_agent_ping_pong_posts=base.max_agent_ping_pong_posts if policy["loop_prevention"] else 0,
                   allow_self_replies=not policy["loop_prevention"], allow_consecutive_posts=True,
                   consecutive_turn_cap=policy["consecutive_turn_cap"], allow_repeated_posts=not policy["dedup"],
                   enforce_cooldown=policy["cooldowns"])


def adapt_agent(run, agent):
    """Return a research cooldown view without mutating the registered agent."""
    if not is_research(run):
        return agent
    view = SimpleNamespace(**{key: copy.deepcopy(getattr(agent, key)) for key in (
        "id", "handle", "role", "persona", "provider", "model", "enabled", "settings",
        "permissions", "cooldown_seconds", "last_spoke_at",
    ) if hasattr(agent, key)})
    policy = policy_for(run)
    if not policy["cooldowns"]:
        view.cooldown_seconds = 0
        view.last_spoke_at = None
    elif policy["cooldown_seconds"] is not None:
        view.cooldown_seconds = policy["cooldown_seconds"]
    return view
