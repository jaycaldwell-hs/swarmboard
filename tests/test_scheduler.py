from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from swarmboard.scheduler import SchedulerConfig, WeightedFairScheduler
from swarmboard.stimuli import mentioned_handles, plan_reactive_stimuli


def agent(
    agent_id: str,
    handle: str,
    *,
    role: str = "specialist",
    scheduler_role: str | None = None,
    enabled: bool = True,
):
    settings = {"expertise": []}
    if scheduler_role is not None:
        settings["scheduler_role"] = scheduler_role
    return SimpleNamespace(
        id=agent_id,
        handle=handle,
        role=role,
        persona=role,
        enabled=enabled,
        permissions={"speak": True},
        settings=settings,
        cooldown_seconds=0,
        last_spoke_at=None,
    )


def post(post_id: str, body: str, *, author_agent_id: str | None = None, parent_post_id=None):
    return SimpleNamespace(
        id=post_id,
        body=body,
        author_agent_id=author_agent_id,
        author_type="agent" if author_agent_id else "human",
        author_handle=author_agent_id or "human",
        parent_post_id=parent_post_id,
    )


def test_post_planning_emits_targeted_mentions_without_duplicate_generic_work() -> None:
    agents = [agent("a1", "critic"), agent("a2", "synth"), agent("a3", "off", enabled=False)]
    plans = plan_reactive_stimuli(
        "@Critic please check this; @critic again, and @off too.",
        agents,
        default_kind="human_post",
        default_priority=5,
    )

    assert mentioned_handles("@Critic and @critic and @synth") == ["critic", "synth"]
    assert [(plan.kind, plan.target_agent_id) for plan in plans] == [("mention", "a1")]


def test_question_planning_uses_an_explicit_durable_reason() -> None:
    plans = plan_reactive_stimuli(
        "What evidence would settle this?",
        [agent("a1", "critic")],
        default_kind="human_post",
        default_priority=5,
    )
    assert len(plans) == 1
    assert plans[0].kind == "unanswered_question"
    assert plans[0].priority == 6


def test_threaded_reply_targets_parent_author_without_an_at_mention() -> None:
    plans = plan_reactive_stimuli(
        "Ada, what would you pick?", [agent("a1", "ada"), agent("a2", "hiro")],
        default_kind="agent_post", default_priority=2, exclude_agent_id="a2", reply_to_agent_id="a1",
    )
    assert len(plans) == 1
    assert plans[0].target_agent_id == "a1"
    assert plans[0].payload["reason"] == "reply_to_author"
    explicit = plan_reactive_stimuli(
        "@hiro, your turn.", [agent("a1", "ada"), agent("a2", "hiro")],
        default_kind="human_post", default_priority=5, reply_to_agent_id="a1",
    )
    assert [plan.target_agent_id for plan in explicit] == ["a2"]


@pytest.mark.parametrize("parent", ["self", "disabled", "outside_run"])
def test_threaded_reply_does_not_target_self_disabled_or_excluded_participants(parent: str) -> None:
    plans = plan_reactive_stimuli(
        "What next?", [agent("self", "writer"), agent("disabled", "off", enabled=False)],
        default_kind="agent_post", default_priority=2, exclude_agent_id="self", reply_to_agent_id=parent,
    )
    assert len(plans) == 1 and plans[0].target_agent_id is None


def test_social_board_roles_keep_scheduler_functions_out_of_view() -> None:
    agents = [
        agent("a1", "spark", role="conversation starter", scheduler_role="proposer"),
        agent("a2", "neighbor", role="practical regular", scheduler_role="specialist"),
        agent("a3", "sideeye", role="friendly skeptic", scheduler_role="critic"),
        agent("a4", "receipts", role="receipts checker", scheduler_role="fact_checker"),
        agent("a5", "weaver", role="thread weaver", scheduler_role="synthesizer"),
        agent("a6", "host", role="community host", scheduler_role="moderator"),
    ]
    scheduler = WeightedFairScheduler(
        SchedulerConfig(max_selected=1, role_injection_probability=0, jitter=0)
    )

    decision = scheduler.select(
        agents,
        thread=SimpleNamespace(id="thread-social", title="Weekend plans", status="active"),
        posts=[post("p-social", "What sounds fun?")],
        stimulus=SimpleNamespace(id="stimulus-social", source_post_id="p-social", kind="human_post"),
        run_seed=3,
    )

    assert {candidate.handle: candidate.role for candidate in decision.candidates} == {
        "spark": "proposer",
        "neighbor": "specialist",
        "sideeye": "critic",
        "receipts": "fact_checker",
        "weaver": "synthesizer",
        "host": "moderator",
    }


def test_seeded_scheduler_is_repeatable_and_prioritizes_direct_mentions() -> None:
    scheduler = WeightedFairScheduler(
        SchedulerConfig(max_selected=1, role_injection_probability=0, jitter=0.2)
    )
    agents = [agent("a1", "critic"), agent("a2", "synth")]
    thread = SimpleNamespace(id="thread-1", title="Review", status="active")
    posts = [post("p1", "@synth can you consolidate this?")]
    stimulus = SimpleNamespace(id="stimulus-1", source_post_id="p1", kind="mention")

    first = scheduler.select(agents, thread=thread, posts=posts, stimulus=stimulus, run_seed=19)
    second = scheduler.select(agents, thread=thread, posts=posts, stimulus=stimulus, run_seed=19)

    assert first.selected_agent_ids == ["a2"]
    assert first.as_dict() == second.as_dict()


def test_dormant_threads_only_wake_for_explicit_wake_reasons() -> None:
    scheduler = WeightedFairScheduler(SchedulerConfig(max_selected=1, role_injection_probability=0))
    agents = [agent("a1", "critic")]
    thread = SimpleNamespace(id="thread-1", title="Dormant", status="dormant")
    posts = [post("p1", "@critic revisit this?")]

    mention = scheduler.select(
        agents,
        thread=thread,
        posts=posts,
        stimulus=SimpleNamespace(id="s1", source_post_id="p1", kind="mention"),
        run_seed=1,
    )
    cascade = scheduler.select(
        agents,
        thread=thread,
        posts=posts,
        stimulus=SimpleNamespace(id="s2", source_post_id="p1", kind="agent_post"),
        run_seed=1,
    )

    assert mention.wake_allowed is True
    assert mention.selected_agent_ids == ["a1"]
    assert cascade.wake_allowed is False
    assert cascade.selected_agent_ids == []


def test_cooldown_makes_an_agent_ineligible() -> None:
    now = datetime.now(timezone.utc)
    cooling = agent("a1", "critic")
    cooling.cooldown_seconds = 60
    cooling.last_spoke_at = now - timedelta(seconds=5)
    ready = agent("a2", "synth")
    scheduler = WeightedFairScheduler(SchedulerConfig(max_selected=1, role_injection_probability=0))

    decision = scheduler.select(
        [cooling, ready],
        thread=SimpleNamespace(id="t", title="Topic", status="active"),
        posts=[post("p", "A fresh prompt")],
        stimulus=SimpleNamespace(id="s", source_post_id="p", kind="human_post"),
        run_seed=3,
        now=now,
    )

    assert decision.selected_agent_ids == ["a2"]
    cooled = next(candidate for candidate in decision.candidates if candidate.agent_id == "a1")
    assert cooled.eligible is False
    assert any("cooldown" in reason for reason in cooled.reasons)
