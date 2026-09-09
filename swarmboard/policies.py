"""Execution-time policy checks for model-proposed actions."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Sequence

from .gateways import AgentAction


_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)


@dataclass(slots=True, frozen=True)
class PolicyConfig:
    max_body_chars: int = 20_000
    max_title_chars: int = 240
    duplicate_lookback: int = 40
    exact_duplicate_case_sensitive: bool = False
    similarity_threshold: float = 0.92
    min_similarity_chars: int = 80
    max_agent_ping_pong_posts: int = 4
    allow_consecutive_posts: bool = False
    allow_self_replies: bool = False
    allow_repeated_posts: bool = False
    require_known_parent: bool = True
    consecutive_turn_cap: int | None = None
    enforce_cooldown: bool = False


@dataclass(slots=True)
class PolicyDecision:
    accepted: bool
    action: AgentAction | None
    reason: str
    fingerprint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def normalize_text(value: str, *, case_sensitive: bool = False) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = _SPACE_RE.sub(" ", value).strip()
    return value if case_sensitive else value.casefold()


def action_fingerprint(action: AgentAction, *, agent_id: str | None = None, thread_id: str | None = None) -> str:
    body = normalize_text(action.body or "")
    title = normalize_text(action.title or "")
    material = "\x1f".join((thread_id or "", agent_id or "", action.action, str(action.parent_post_id or ""), title, body))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _WORD_RE.findall(value)}


def text_similarity(left: str, right: str) -> float:
    """A conservative repetition score in [0, 1]."""

    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if left_norm == right_norm:
        return 1.0
    left_tokens, right_tokens = _tokens(left_norm), _tokens(right_norm)
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    sequence = SequenceMatcher(None, left_norm, right_norm, autojunk=False).ratio()
    # Both lexical and sequential similarity should be high before rejecting a
    # substantive reply.  The maximum still catches punctuation-only rewrites.
    return max(sequence, (jaccard + sequence) / 2)


def _identity(post: Any) -> str | None:
    agent_id = getattr(post, "author_agent_id", None)
    if agent_id:
        return str(agent_id)
    handle = getattr(post, "author_handle", None)
    if handle and str(getattr(post, "author_type", "")) == "agent":
        return f"handle:{str(handle).casefold()}"
    return None


def violates_ping_pong(posts: Sequence[Any], candidate_agent_id: str, limit: int) -> bool:
    """Return true if speaking would extend a two-agent alternating monopoly."""

    if limit <= 0:
        return False
    identities = [identity for post in posts if (identity := _identity(post)) is not None]
    if len(identities) < limit:
        return False
    prospective = identities + [str(candidate_agent_id)]
    tail = prospective[-(limit + 1) :]
    unique = set(tail)
    if len(unique) != 2:
        return False
    return all(tail[index] != tail[index - 1] for index in range(1, len(tail)))


def violates_consecutive_cap(posts: Sequence[Any], candidate_agent_id: str, limit: int | None) -> bool:
    """Humans or another participant end a consecutive posting streak."""
    if limit is None:
        return False
    count = 0
    for post in reversed(posts):
        if _identity(post) != str(candidate_agent_id):
            break
        count += 1
        if count >= limit:
            return True
    return False


def _permissions(agent: Any) -> Mapping[str, Any]:
    permissions = getattr(agent, "permissions", None)
    return permissions if isinstance(permissions, Mapping) else {}


class ActionPolicy:
    """Validate an action against a captured thread snapshot.

    This class is intentionally pure: it never writes to the database.  The
    engine can therefore retain the decision before atomically committing an
    accepted post/event pair.
    """

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config or PolicyConfig()

    def validate(
        self,
        action: AgentAction,
        *,
        agent: Any,
        thread: Any | None,
        posts: Sequence[Any],
        existing_fingerprints: Iterable[str] = (),
        now: datetime | None = None,
    ) -> PolicyDecision:
        agent_id = str(getattr(agent, "id", ""))
        thread_id = str(getattr(thread, "id", "")) if thread is not None else None
        fingerprint = action_fingerprint(action, agent_id=agent_id, thread_id=thread_id)

        # Passing has no side effect and remains safe if eligibility changed
        # while the provider call was in flight.
        if action.action == "pass":
            return PolicyDecision(True, action, "agent chose to pass", fingerprint)

        if not bool(getattr(agent, "enabled", True)):
            return PolicyDecision(False, None, "agent is disabled", fingerprint)
        permissions = _permissions(agent)
        if permissions.get("speak", permissions.get("can_post", True)) is False:
            return PolicyDecision(False, None, "agent is not permitted to post", fingerprint)
        thread_status = str(getattr(thread, "status", ""))
        if thread_status == "closed":
            return PolicyDecision(False, None, "thread is closed", fingerprint)

        if self.config.enforce_cooldown:
            cooldown = max(0.0, float(getattr(agent, "cooldown_seconds", 0) or 0))
            last_spoke = getattr(agent, "last_spoke_at", None)
            if cooldown and isinstance(last_spoke, datetime):
                if last_spoke.tzinfo is None:
                    last_spoke = last_spoke.replace(tzinfo=timezone.utc)
                current_time = now or datetime.now(timezone.utc)
                if current_time.tzinfo is None:
                    current_time = current_time.replace(tzinfo=timezone.utc)
                if (current_time - last_spoke).total_seconds() < cooldown:
                    return PolicyDecision(False, None, "agent cooldown has not elapsed", fingerprint)

        body = action.body or ""
        if len(body) > self.config.max_body_chars:
            return PolicyDecision(False, None, "body exceeds configured character limit", fingerprint)
        if action.title and len(action.title) > self.config.max_title_chars:
            return PolicyDecision(False, None, "title exceeds configured character limit", fingerprint)

        if action.action == "new_thread" and permissions.get("new_thread", permissions.get("can_create_threads", True)) is False:
            return PolicyDecision(False, None, "agent is not permitted to create threads", fingerprint)

        post_by_id = {str(getattr(post, "id", "")): post for post in posts}
        if action.parent_post_id is not None:
            parent = post_by_id.get(str(action.parent_post_id))
            if parent is None and self.config.require_known_parent:
                return PolicyDecision(False, None, "parent_post_id is not in the captured thread context", fingerprint)
            if not self.config.allow_self_replies and parent is not None and _identity(parent) == agent_id:
                return PolicyDecision(False, None, "immediate self-replies are not allowed", fingerprint)

        if not self.config.allow_consecutive_posts and posts and _identity(posts[-1]) == agent_id:
            return PolicyDecision(False, None, "agent cannot reply immediately after itself", fingerprint)

        if violates_consecutive_cap(posts, agent_id, self.config.consecutive_turn_cap):
            return PolicyDecision(False, None, "consecutive turn cap reached", fingerprint)

        if violates_ping_pong(posts, agent_id, self.config.max_agent_ping_pong_posts):
            return PolicyDecision(False, None, "two-agent ping-pong limit reached", fingerprint)

        if self.config.allow_repeated_posts:
            return PolicyDecision(True, action, "autonomous board action", fingerprint)

        known_fingerprints = set(existing_fingerprints)
        if fingerprint in known_fingerprints:
            return PolicyDecision(False, None, "identical action was already committed", fingerprint)

        normalized = normalize_text(body, case_sensitive=self.config.exact_duplicate_case_sensitive)
        for post in list(posts)[-self.config.duplicate_lookback :]:
            prior_body = str(getattr(post, "body", "") or "")
            prior_normalized = normalize_text(prior_body, case_sensitive=self.config.exact_duplicate_case_sensitive)
            if normalized == prior_normalized:
                return PolicyDecision(
                    False,
                    None,
                    "duplicate of an existing post",
                    fingerprint,
                    {"duplicate_post_id": str(getattr(post, "id", "")), "similarity": 1.0},
                )
            if min(len(normalized), len(prior_normalized)) < self.config.min_similarity_chars:
                continue
            similarity = text_similarity(normalized, prior_normalized)
            if similarity >= self.config.similarity_threshold:
                return PolicyDecision(
                    False,
                    None,
                    "substantially repeats an existing post",
                    fingerprint,
                    {"duplicate_post_id": str(getattr(post, "id", "")), "similarity": round(similarity, 4)},
                )

        return PolicyDecision(True, action, "action satisfies execution policies", fingerprint)


__all__ = [
    "ActionPolicy",
    "PolicyConfig",
    "PolicyDecision",
    "action_fingerprint",
    "normalize_text",
    "text_similarity",
    "violates_ping_pong",
]
