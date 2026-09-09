from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .models import StimulusKind


_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_-]{0,79})(?![\w-])")


@dataclass(frozen=True, slots=True)
class StimulusPlan:
    kind: str
    target_agent_id: str | None
    priority: float
    payload: dict[str, Any]
    dedupe_label: str


def mentioned_handles(body: str) -> list[str]:
    """Return distinct handles in textual order, matched case-insensitively."""

    seen: set[str] = set()
    handles: list[str] = []
    for match in _MENTION_RE.finditer(body):
        normalized = match.group(1).casefold()
        if normalized not in seen:
            seen.add(normalized)
            handles.append(normalized)
    return handles


def plan_reactive_stimuli(
    body: str,
    agents: Iterable[Any],
    *,
    default_kind: StimulusKind | str,
    default_priority: float,
    exclude_agent_id: str | None = None,
    reply_to_agent_id: str | None = None,
) -> list[StimulusPlan]:
    """Turn post content into durable, non-overlapping response reasons.

    Direct mentions take precedence, followed by a threaded reply's author.
    Otherwise questions and generic posts invite a participant from the board.
    """

    by_handle = {
        str(getattr(agent, "handle", "")).casefold(): agent
        for agent in agents
        if bool(getattr(agent, "enabled", True))
    }
    plans: list[StimulusPlan] = []
    for handle in mentioned_handles(body):
        agent = by_handle.get(handle)
        agent_id = str(getattr(agent, "id", "")) if agent is not None else ""
        if not agent_id or agent_id == exclude_agent_id:
            continue
        plans.append(
            StimulusPlan(
                kind=StimulusKind.MENTION.value,
                target_agent_id=agent_id,
                priority=10.0,
                payload={"reason": "direct_mention", "mentioned_handle": handle},
                dedupe_label=f"mention:{agent_id}",
            )
        )
    if plans:
        return plans
    if reply_to_agent_id and reply_to_agent_id != exclude_agent_id:
        if any(str(agent.id) == reply_to_agent_id for agent in by_handle.values()):
            # Addressed replies use the same targeted queue and wake behavior as
            # mentions; the payload records why the recipient was selected.
            return [StimulusPlan(
                kind=StimulusKind.MENTION.value,
                target_agent_id=reply_to_agent_id,
                priority=9.0,
                payload={"reason": "reply_to_author"},
                dedupe_label=f"reply:{reply_to_agent_id}",
            )]
    if "?" in body:
        return [
            StimulusPlan(
                kind=StimulusKind.UNANSWERED_QUESTION.value,
                target_agent_id=None,
                priority=max(default_priority, 6.0),
                payload={"reason": "unanswered_question"},
                dedupe_label="unanswered-question",
            )
        ]
    kind = str(getattr(default_kind, "value", default_kind))
    return [
        StimulusPlan(
            kind=kind,
            target_agent_id=None,
            priority=default_priority,
            payload={"reason": kind},
            dedupe_label=kind.replace("_", "-"),
        )
    ]


__all__ = ["StimulusPlan", "mentioned_handles", "plan_reactive_stimuli"]
