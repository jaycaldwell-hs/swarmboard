"""Seeded weighted-fair scheduling for Swarmboard agents."""

from __future__ import annotations

import hashlib
import math
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .policies import violates_ping_pong


_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")
_WAKE_KINDS = {
    "human_post",
    "mention",
    "unanswered_question",
    "idle_revisit",
    "new_evidence",
}


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value or ""))


def _stable_seed(*parts: Any) -> int:
    digest = hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _words(value: str) -> set[str]:
    return {match.group(0).casefold() for match in _WORD_RE.finditer(value)}


def _agent_role(agent: Any) -> str:
    settings = getattr(agent, "settings", None)
    configured = None
    if isinstance(settings, Mapping):
        configured = settings.get("scheduler_role") or settings.get("role")
    source = str(
        configured
        or getattr(agent, "role", "")
        or getattr(agent, "persona", "")
        or getattr(agent, "handle", "")
    ).casefold()
    if "fact" in source and ("check" in source or "verify" in source):
        return "fact_checker"
    role_aliases = {
        "fact_checker": ("receipts", "evidence checker", "source checker"),
        "synthesizer": ("synthesizer", "thread weaver", "recapper", "connector"),
        "moderator": ("moderator", "community host", "host"),
        "critic": ("critic", "friendly skeptic", "skeptic", "sideeye"),
        "proposer": ("proposer", "conversation starter", "starter", "spark"),
        "specialist": ("specialist", "practical regular", "neighbor"),
    }
    for role, aliases in role_aliases.items():
        if any(alias in source for alias in aliases):
            return role
    return "specialist"


def _post_agent_id(post: Any) -> str | None:
    value = getattr(post, "author_agent_id", None)
    return str(value) if value else None


def _latest_trigger_post(posts: Sequence[Any], stimulus: Any | None) -> Any | None:
    post_id = str(
        getattr(stimulus, "source_post_id", None)
        or getattr(stimulus, "post_id", "")
        or ""
    )
    if post_id:
        for post in reversed(posts):
            if str(getattr(post, "id", "")) == post_id:
                return post
    return posts[-1] if posts else None


def _outstanding_questions(posts: Sequence[Any]) -> list[Any]:
    replied_to = {str(getattr(post, "parent_post_id", "")) for post in posts if getattr(post, "parent_post_id", None)}
    return [
        post
        for post in posts
        if "?" in str(getattr(post, "body", "")) and str(getattr(post, "id", "")) not in replied_to
    ]


def _contains_mention(body: str, handle: str) -> bool:
    if not handle:
        return False
    return re.search(rf"(?<![\w@])@{re.escape(handle)}(?![\w-])", body, re.IGNORECASE) is not None


@dataclass(slots=True, frozen=True)
class SchedulerConfig:
    max_selected: int = 2
    recent_window: int = 8
    domination_window: int = 20
    ping_pong_limit: int = 4
    allow_consecutive_posts: bool = False
    mention_weight: float = 8.0
    question_weight: float = 2.5
    expertise_weight: float = 3.0
    novelty_weight: float = 1.4
    recent_penalty: float = 1.1
    domination_penalty: float = 4.0
    role_injection_weight: float = 4.5
    jitter: float = 0.20
    role_injection_probability: float = 0.24
    minimum_score: float = 0.05


@dataclass(slots=True)
class CandidateScore:
    agent_id: str
    handle: str
    role: str
    score: float
    eligible: bool
    components: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class SchedulingDecision:
    selected_agent_ids: list[str]
    candidates: list[CandidateScore]
    reason: str
    seed: int
    wake_allowed: bool = True
    injected_role: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected_agent_ids": self.selected_agent_ids,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "reason": self.reason,
            "seed": self.seed,
            "wake_allowed": self.wake_allowed,
            "injected_role": self.injected_role,
        }


class WeightedFairScheduler:
    """Score eligible agents and select a seeded weighted sample.

    The seed is derived from the run seed and durable stimulus identity, so crash
    recovery reaches the same selection without depending on process-global RNG
    state.  Fairness is represented by recent-participation and domination costs,
    while mentions and relevant expertise can outweigh those costs.
    """

    def __init__(self, config: SchedulerConfig | None = None) -> None:
        self.config = config or SchedulerConfig()

    def select(
        self,
        agents: Sequence[Any],
        *,
        thread: Any,
        posts: Sequence[Any],
        stimulus: Any | None,
        run_seed: int,
        now: datetime | None = None,
        max_selected: int | None = None,
        quota_remaining: Mapping[str, int] | None = None,
    ) -> SchedulingDecision:
        now = now or datetime.now(timezone.utc)
        stimulus_identity = getattr(stimulus, "id", None) or getattr(stimulus, "dedupe_key", None) or "manual"
        decision_seed = _stable_seed(run_seed, getattr(thread, "id", ""), stimulus_identity)
        rng = random.Random(decision_seed)
        kind = _enum_value(getattr(stimulus, "kind", "manual_step"))
        status = _enum_value(getattr(thread, "status", "active"))

        if status == "closed":
            return SchedulingDecision([], [], "thread is closed", decision_seed, wake_allowed=False)
        if status == "dormant" and kind not in _WAKE_KINDS:
            return SchedulingDecision(
                [], [], f"{kind or 'unknown'} is not permitted to wake a dormant thread", decision_seed, wake_allowed=False
            )

        trigger_post = _latest_trigger_post(posts, stimulus)
        trigger_body = str(getattr(trigger_post, "body", "") or "")
        topic = " ".join((str(getattr(thread, "title", "") or ""), trigger_body))
        topic_words = _words(topic)
        questions = _outstanding_questions(posts)
        recent_posts = list(posts)[-self.config.recent_window :]
        domination_posts = [post for post in list(posts)[-self.config.domination_window :] if _post_agent_id(post)]

        injected_role = self._choose_injected_role(posts, questions, rng)
        candidates = [
            self._score_agent(
                agent,
                posts=posts,
                recent_posts=recent_posts,
                domination_posts=domination_posts,
                trigger_body=trigger_body,
                topic_words=topic_words,
                has_questions=bool(questions),
                injected_role=injected_role,
                now=now,
                jitter_rng=random.Random(_stable_seed(decision_seed, getattr(agent, "id", ""))),
                quota_remaining=quota_remaining,
            )
            for agent in agents
        ]
        candidates.sort(key=lambda candidate: (-candidate.score, candidate.handle.casefold(), candidate.agent_id))
        eligible = [candidate for candidate in candidates if candidate.eligible]
        if not eligible:
            return SchedulingDecision([], candidates, "no agent is currently eligible", decision_seed, injected_role=injected_role)

        count = min(max_selected or self.config.max_selected, self.config.max_selected, len(eligible))
        selected: list[CandidateScore] = []

        # Direct mentions get the first opportunity to speak.  Remaining slots
        # use weighted sampling, preserving variety without losing determinism.
        mentioned = [candidate for candidate in eligible if candidate.components.get("direct_mention", 0) > 0]
        if mentioned and count:
            selected.append(mentioned[0])

        pool = [candidate for candidate in eligible if candidate not in selected]
        while len(selected) < count and pool:
            picked = self._weighted_pick(pool, rng)
            selected.append(picked)
            pool.remove(picked)

        selected_ids = [candidate.agent_id for candidate in selected]
        reason_bits = [
            f"@{candidate.handle}: " + ", ".join(candidate.reasons[:3])
            for candidate in selected
        ]
        return SchedulingDecision(
            selected_ids,
            candidates,
            "; ".join(reason_bits) or "seeded weighted-fair selection",
            decision_seed,
            injected_role=injected_role,
        )

    def _score_agent(
        self,
        agent: Any,
        *,
        posts: Sequence[Any],
        recent_posts: Sequence[Any],
        domination_posts: Sequence[Any],
        trigger_body: str,
        topic_words: set[str],
        has_questions: bool,
        injected_role: str | None,
        now: datetime,
        jitter_rng: random.Random,
        quota_remaining: Mapping[str, int] | None,
    ) -> CandidateScore:
        agent_id = str(getattr(agent, "id", ""))
        handle = str(getattr(agent, "handle", "") or agent_id)
        role = _agent_role(agent)
        components: dict[str, float] = {"base": 1.0}
        reasons: list[str] = []
        eligible = bool(getattr(agent, "enabled", True))
        if not eligible:
            reasons.append("disabled")

        permissions = getattr(agent, "permissions", None)
        if isinstance(permissions, Mapping) and permissions.get("speak", permissions.get("can_post", True)) is False:
            eligible = False
            reasons.append("posting permission denied")
        if quota_remaining is not None and quota_remaining.get(agent_id, 1) <= 0:
            eligible = False
            reasons.append("agent quota exhausted")

        if not self.config.allow_consecutive_posts and posts and _post_agent_id(posts[-1]) == agent_id:
            components["self_reply"] = -100.0
            eligible = False
            reasons.append("would immediately self-reply")
        if violates_ping_pong(posts, agent_id, self.config.ping_pong_limit):
            components["ping_pong"] = -100.0
            eligible = False
            reasons.append("two-agent ping-pong limit")

        cooldown_seconds = max(0.0, float(getattr(agent, "cooldown_seconds", 0) or 0))
        last_spoke = getattr(agent, "last_spoke_at", None)
        if cooldown_seconds and isinstance(last_spoke, datetime):
            if last_spoke.tzinfo is None:
                last_spoke = last_spoke.replace(tzinfo=timezone.utc)
            remaining = cooldown_seconds - (now - last_spoke).total_seconds()
            if remaining > 0:
                components["cooldown"] = -100.0
                eligible = False
                reasons.append(f"cooldown has {math.ceil(remaining)}s remaining")

        if _contains_mention(trigger_body, handle):
            components["direct_mention"] = self.config.mention_weight
            reasons.append("directly mentioned")
        if has_questions:
            components["unanswered_question"] = self.config.question_weight
            reasons.append("unanswered question")

        settings = getattr(agent, "settings", None)
        expertise_value: Any = settings.get("expertise", []) if isinstance(settings, Mapping) else []
        if isinstance(expertise_value, str):
            expertise_text = expertise_value
        elif isinstance(expertise_value, Sequence):
            expertise_text = " ".join(str(item) for item in expertise_value)
        else:
            expertise_text = ""
        expertise_words = _words(" ".join((expertise_text, str(getattr(agent, "persona", "") or ""))))
        if expertise_words and topic_words:
            overlap = len(expertise_words & topic_words) / max(1, min(len(topic_words), 12))
            expertise_score = min(self.config.expertise_weight, self.config.expertise_weight * overlap)
            if expertise_score:
                components["expertise"] = expertise_score
                reasons.append("topic matches expertise")

        agent_post_count = sum(_post_agent_id(post) == agent_id for post in posts)
        recent_count = sum(_post_agent_id(post) == agent_id for post in recent_posts)
        if agent_post_count == 0:
            components["novelty"] = self.config.novelty_weight
            reasons.append("has not participated yet")
        elif not any(_post_agent_id(post) == agent_id for post in recent_posts):
            components["novelty"] = self.config.novelty_weight * 0.55
            reasons.append("voice absent from recent discussion")
        if recent_count:
            components["recent_participation"] = -self.config.recent_penalty * recent_count
            reasons.append(f"spoke {recent_count} time(s) recently")
        if domination_posts:
            share = sum(_post_agent_id(post) == agent_id for post in domination_posts) / len(domination_posts)
            if share:
                components["domination"] = -self.config.domination_penalty * share
                if share >= 0.4:
                    reasons.append("high recent share")

        if injected_role == role:
            components["role_injection"] = self.config.role_injection_weight
            reasons.append(f"{role.replace('_', '-')} perspective requested")

        configured_weight = settings.get("scheduler_weight", 1.0) if isinstance(settings, Mapping) else 1.0
        try:
            weight_bonus = max(0.1, min(float(configured_weight), 5.0))
        except (TypeError, ValueError):
            weight_bonus = 1.0
        components["configured_weight"] = weight_bonus - 1.0
        components["seeded_jitter"] = jitter_rng.uniform(0.0, self.config.jitter)
        score = sum(components.values())
        if not eligible:
            score = min(score, -1.0)
        return CandidateScore(agent_id, handle, role, round(score, 6), eligible, components, reasons)

    def _choose_injected_role(self, posts: Sequence[Any], questions: Sequence[Any], rng: random.Random) -> str | None:
        if rng.random() >= self.config.role_injection_probability:
            return None
        # A fact-checker is most useful while questions/claims are unresolved;
        # synthesis becomes more useful as a thread grows.
        if questions:
            roles = ("fact_checker", "critic", "synthesizer")
            weights = (0.50, 0.30, 0.20)
        elif len(posts) >= 8:
            roles = ("synthesizer", "critic", "fact_checker")
            weights = (0.55, 0.30, 0.15)
        else:
            roles = ("critic", "fact_checker", "synthesizer")
            weights = (0.50, 0.30, 0.20)
        return rng.choices(roles, weights=weights, k=1)[0]

    def _weighted_pick(self, candidates: Sequence[CandidateScore], rng: random.Random) -> CandidateScore:
        minimum = min(candidate.score for candidate in candidates)
        weights = [max(self.config.minimum_score, candidate.score - minimum + self.config.minimum_score) for candidate in candidates]
        return rng.choices(list(candidates), weights=weights, k=1)[0]


__all__ = [
    "CandidateScore",
    "SchedulerConfig",
    "SchedulingDecision",
    "WeightedFairScheduler",
]
