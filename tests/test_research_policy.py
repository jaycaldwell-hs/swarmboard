from __future__ import annotations

import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from swarmboard import autonomy, sessions
from swarmboard.gateways import AgentAction
from swarmboard.models import Event, Run, Thread, utc_now
from swarmboard.policies import ActionPolicy, PolicyConfig
from swarmboard.repository import Repository
from swarmboard.run_policy import (
    PERMISSIVE, PRODUCTION, adapt_agent, effective_action_config,
    effective_scheduler_config, normalize_config, policy_for,
)
from swarmboard.scheduler import SchedulerConfig, WeightedFairScheduler
from tests.test_engine_acceptance import make_database
from tests.test_scheduler import agent, post


def research(policy="permissive"):
    return SimpleNamespace(config=normalize_config({"session_type": "research", "policy": policy}))


def test_policy_normalization_is_complete_repeatable_and_does_not_mutate_input():
    original = {"other": {"items": [1]}, "session_type": "research", "policy": "permissive"}
    before = copy.deepcopy(original)
    result = normalize_config(original)
    assert result["policy"] == PERMISSIVE and original == before
    assert normalize_config(result) == result
    result["other"]["items"].append(2)
    result["policy"]["dedup"] = True
    assert original == before and PERMISSIVE["dedup"] is False
    assert normalize_config(None) == {"session_type": "collaboration", "policy": PRODUCTION}
    assert normalize_config({"policy": {}})["policy"] == PRODUCTION


@pytest.mark.parametrize("bad", [
    {"dedup": "false"}, {"cooldowns": 0}, {"schema_mode": "anything"},
    {"consecutive_turn_cap": 0}, {"consecutive_turn_cap": True}, {"consecutive_turn_cap": 1.5},
    {"cooldown_seconds": -1}, {"cooldown_seconds": True}, {"cooldown_seconds": float("inf")},
    {"cooldown_seconds": float("nan")}, {"dedupe": False}, {"profile": "unknown"}, [],
])
def test_policy_rejects_invalid_knobs(bad):
    with pytest.raises(ValueError):
        research(bad)


@pytest.mark.parametrize("policy", ["permissive", {"dedup": False}, {"profile": "custom"},
                                   {"profile": "production", "cooldown_seconds": 0}])
def test_collaboration_rejects_every_nonproduction_policy(policy):
    with pytest.raises(ValueError, match="collaboration sessions require"):
        normalize_config({"policy": policy})


def test_custom_knobs_are_canonical_and_do_not_change_scheduler_weights():
    run = research({"profile": "permissive", "consecutive_turn_cap": 3, "cooldowns": True, "cooldown_seconds": 2.5})
    assert policy_for(run)["profile"] == "custom"
    base = SchedulerConfig(mention_weight=9, recent_penalty=3, domination_penalty=5)
    effective = effective_scheduler_config(run, base)
    assert effective.consecutive_turn_cap == 3 and effective.cooldowns
    assert (effective.mention_weight, effective.recent_penalty, effective.domination_penalty) == (9, 3, 5)
    participant = agent("a", "ada")
    participant.cooldown_seconds = 60
    assert adapt_agent(run, participant).cooldown_seconds == 2.5
    assert participant.cooldown_seconds == 60
    collaboration = SimpleNamespace(config={"interaction_mode": "autonomous"})
    assert effective_scheduler_config(collaboration, base) is base
    action_base = PolicyConfig(allow_repeated_posts=True, allow_self_replies=True)
    assert effective_action_config(collaboration, action_base) is action_base


def test_permissive_scheduler_bypasses_cooldown_loops_and_dormancy_but_keeps_permissions():
    now = utc_now()
    participant = agent("a", "ada")
    participant.cooldown_seconds = 60
    participant.last_spoke_at = now - timedelta(seconds=1)
    history = [post(str(index), "same", author_agent_id="a") for index in range(8)]
    kwargs = dict(thread=SimpleNamespace(id="t", title="Thread", status="dormant"), posts=history,
                  stimulus=SimpleNamespace(id="s", kind="agent_post"), run_seed=1, now=now)
    protected = WeightedFairScheduler(effective_scheduler_config(research("production"), SchedulerConfig()))
    relaxed = WeightedFairScheduler(effective_scheduler_config(research(), SchedulerConfig()))
    assert protected.select([participant], **kwargs).selected_agent_ids == []
    assert relaxed.select([participant], **kwargs).selected_agent_ids == ["a"]
    participant.permissions = {"speak": False}
    assert relaxed.select([participant], **kwargs).selected_agent_ids == []
    participant.permissions = {"speak": True}
    kwargs["thread"].status = "closed"
    assert relaxed.select([participant], **kwargs).selected_agent_ids == []


def test_consecutive_cap_matches_scheduler_and_action_policy_and_human_resets_streak():
    run = research({"profile": "permissive", "consecutive_turn_cap": 3})
    scheduler = WeightedFairScheduler(effective_scheduler_config(run, SchedulerConfig()))
    policy = ActionPolicy(effective_action_config(run, PolicyConfig()))
    participant = agent("a", "ada")
    thread = SimpleNamespace(id="t", title="Thread", status="active")
    history = [post(str(index), "same", author_agent_id="a") for index in range(3)]
    action = AgentAction(action="reply", body="same", intent="clarify")
    for history_slice, accepted in [(history[:2], True), (history, False), (history + [post("h", "continue")], True)]:
        decision = scheduler.select([participant], thread=thread, posts=history_slice,
                                    stimulus=SimpleNamespace(id="s", kind="agent_post"), run_seed=1)
        assert bool(decision.selected_agent_ids) is accepted
        assert policy.validate(action, agent=participant, thread=thread, posts=history_slice).accepted is accepted


def test_permissive_execution_disables_dedup_and_ping_pong_but_preserves_parent_and_enabled_fences():
    participant = agent("a", "ada")
    thread = SimpleNamespace(id="t", status="active")
    history = [post(str(index), "repeat", author_agent_id="b" if index % 2 else "a") for index in range(4)]
    action = AgentAction(action="reply", body="repeat", intent="clarify")
    protected = ActionPolicy(effective_action_config(research("production"), PolicyConfig()))
    relaxed = ActionPolicy(effective_action_config(research(), PolicyConfig()))
    assert protected.validate(action, agent=participant, thread=thread, posts=history).reason == "two-agent ping-pong limit reached"
    assert relaxed.validate(action, agent=participant, thread=thread, posts=history).accepted
    duplicate_only = research({"loop_prevention": False, "consecutive_turn_cap": None})
    assert "duplicate" in ActionPolicy(effective_action_config(duplicate_only, PolicyConfig())).validate(
        action, agent=participant, thread=thread, posts=history).reason
    bad_parent = action.model_copy(update={"parent_post_id": "unknown"})
    assert not relaxed.validate(bad_parent, agent=participant, thread=thread, posts=history).accepted
    participant.enabled = False
    assert not relaxed.validate(action, agent=participant, thread=thread, posts=history).accepted


def test_execution_cooldown_fence_uses_run_policy_and_supplied_clock():
    now = utc_now()
    participant = agent("a", "ada")
    participant.cooldown_seconds = 60
    participant.last_spoke_at = now - timedelta(seconds=5)
    action = AgentAction(action="reply", body="hello", intent="clarify")
    args = dict(agent=participant, thread=SimpleNamespace(id="t", status="active"), posts=[], now=now)
    policy = ActionPolicy(effective_action_config(research("production"), PolicyConfig()))
    assert "cooldown" in policy.validate(action, **args).reason
    assert policy.validate(action, **{**args, "now": now + timedelta(seconds=60)}).accepted
    assert ActionPolicy(effective_action_config(research(), PolicyConfig())).validate(action, **args).accepted


def test_session_restart_preserves_policy_type_and_scopes_cooldown_to_generated_run_history():
    db, factory = make_database()
    with factory.begin() as session:
        repo = Repository(session)
        participant = repo.create_agent(handle="ada", provider="codex", model="gpt-6-astra", persona="Participant", cooldown_seconds=80)
        run = autonomy.create_session(repo, agents=[participant], continuous=False,
                                      session_type="research", policy={"cooldown_seconds": 20})
        thread = session.scalar(select(Thread).where(Thread.run_id == run.id))
        generated = repo.create_agent_post(thread.id, participant.id, "A generated contribution.").post
        turn = repo.create_turn(thread_id=thread.id, agent_id=participant.id, run_id=run.id)
        turn.resulting_post_id = generated.id
        session.flush()
        active_view = sessions.session_agent(session, run, participant)
        assert active_view.cooldown_seconds == 20 and active_view.last_spoke_at == generated.created_at
        clone = sessions.restart(repo, run)
        assert clone.config["session_type"] == "research" and clone.config["policy"] == run.config["policy"]
        assert sessions.session_agent(session, clone, participant).last_spoke_at is None
        assert sessions.activity(repo, clone.id)["policy"] == run.config["policy"]
        collaboration = autonomy.create_session(repo, agents=[participant], continuous=False)
        assert collaboration.config["session_type"] == "collaboration"
        assert sessions.session_agent(session, collaboration, participant).cooldown_seconds == 0
        before = (len(list(session.scalars(select(Run)))), len(list(session.scalars(select(Event)))))
        with pytest.raises(ValueError, match="collaboration"):
            autonomy.create_session(repo, agents=[participant], policy="permissive")
        assert (len(list(session.scalars(select(Run)))), len(list(session.scalars(select(Event))))) == before
    db.dispose()
