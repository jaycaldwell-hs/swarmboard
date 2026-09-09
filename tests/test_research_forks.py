from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from swarmboard import cadence, research, sessions
from swarmboard.models import Agent, Event, Experiment, Post, Run, Stimulus, Thread, Turn
from swarmboard.repository import InvalidStateError, Repository
from .test_engine_acceptance import make_database


@pytest.fixture
def store():
    engine, factory = make_database()
    try:
        yield factory
    finally:
        engine.dispose()


def seed(repo, *, session_type="collaboration", policy="production"):
    first = repo.create_agent(handle="first", persona="First perspective", model="qwen/test", cooldown_seconds=20)
    second = repo.create_agent(handle="second", persona="Second perspective", model="deepseek/test", cooldown_seconds=20)
    run = sessions.create_session(
        repo, agents=[first, second], title="Source discussion", body="Opening from a human",
        author_handle="operator", session_type=session_type, policy=policy, continuous=False,
        limits={"max_rounds": 20, "max_tokens": 5000, "max_duration_seconds": 100},
    )
    thread = repo.list_threads(run_id=run.id)[0]
    opening = repo.list_posts(thread.id)[0]
    one = repo.create_agent_post(thread.id, first.id, "First contribution", parent_post_id=opening.id)
    two = repo.create_agent_post(thread.id, second.id, "Second contribution", parent_post_id=one.post.id)
    repo.create_human_post(thread.id, "A later human response", parent_post_id=two.post.id, author_handle="operator")
    return run, thread, [opening, one.post, two.post], [first, second]


def source_snapshot(repo, run_id):
    def rows(model, criterion):
        return deepcopy([
            {column.key: getattr(row, column.key) for column in model.__table__.columns}
            for row in repo.session.scalars(select(model).where(criterion))
        ])
    thread_ids = select(Thread.id).where(Thread.run_id == run_id)
    # SQL column `metadata` maps to Post.metadata_json, so use raw mappings there.
    return {
        "runs": rows(Run, Run.id == run_id),
        "threads": rows(Thread, Thread.run_id == run_id),
        "posts": deepcopy(list(repo.session.execute(Post.__table__.select().where(Post.thread_id.in_(thread_ids))).mappings())),
        "turns": rows(Turn, Turn.run_id == run_id),
        "stimuli": rows(Stimulus, Stimulus.run_id == run_id),
        "events": rows(Event, Event.run_id == run_id),
        "experiment": rows(Experiment, Experiment.run_id == run_id),
        "agents": rows(Agent, Agent.id.is_not(None)),
    }


@pytest.mark.parametrize("source_state", ["running", "stopped", "closed"])
def test_fork_copies_exact_prefix_without_mutating_source_or_spending_budget(store, source_state):
    with store.begin() as session:
        repo = Repository(session)
        source, thread, posts, agents = seed(repo)
        if source_state == "running":
            repo.control_run(source.id, "start")
        else:
            repo.control_run(source.id, "stop")
            if source_state == "closed":
                repo.set_thread_status(thread.id, "closed")
        before = source_snapshot(repo, source.id)
        child = research.fork(repo, thread_id=thread.id, at_post_id=posts[1].id, author="researcher")
        assert source_snapshot(repo, source.id) == before
        run = repo.get_run(child["run_id"])
        copied = repo.list_posts(child["thread_id"])
        assert [post.body for post in copied] == [post.body for post in posts[:2]]
        assert [post.sequence for post in copied] == [1, 2]
        assert copied[1].parent_post_id == copied[0].id
        assert copied[1].author_agent_id == agents[0].id
        assert {post.id for post in copied}.isdisjoint({post.id for post in posts})
        assert all(post.metadata_json["is_inherited"] for post in copied)
        assert [post.metadata_json["inherited_from_post_id"] for post in copied] == [post.id for post in posts[:2]]
        assert run.state == "created" and not run.continuous
        assert (run.posts_used, run.rounds_used, run.tokens_used, run.model_calls) == (0, 0, 0, 0)
        assert run.config["session_type"] == "research"
        assert run.config["policy"]["profile"] == "production"
        assert run.config["agent_ids"] == [agent.id for agent in agents]
        assert repo.get_thread(child["thread_id"]).status == "active"
        assert sessions.session_agent(session, run, agents[0]).last_spoke_at is None
        stimuli = repo.list_stimuli(run_id=run.id)
        assert len(stimuli) == 1 and stimuli[0].state == "pending"
        assert stimuli[0].source_post_id == copied[-1].id
        events = repo.list_events(run_id=run.id)
        inherited_events = [event for event in events if event.event_type == "post.created"]
        assert len(inherited_events) == 2
        assert all(event.payload["is_inherited"] for event in inherited_events)
        fork_event = next(event for event in events if event.event_type == "research.forked")
        assert fork_event.actor_type == "human" and fork_event.actor_id == "researcher"


def test_fork_clones_current_registration_policy_and_full_ancestry(store):
    with store.begin() as session:
        repo = Repository(session)
        source, thread, posts, agents = seed(repo, session_type="research", policy="permissive")
        source.config = {**source.config, "cadence_index": 9, "cadence_cycle_active": True,
                         "step_once": True, "unavailable_agent_ids": [agents[0].id], "custom_retained": "yes"}
        agents[0].model = "qwen/new-model"
        agents[0].persona = "Updated before fork"
        session.flush()
        before = source_snapshot(repo, source.id)
        first = research.fork(repo, thread_id=thread.id, at_post_id=posts[2].id, author="one")
        child_run = repo.get_run(first["run_id"])
        assert child_run.config["policy"] == source.config["policy"]
        assert not {"cadence_index", "cadence_cycle_active", "step_once", "unavailable_agent_ids"} & child_run.config.keys()
        assert child_run.config["custom_retained"] == "yes"
        manifest = session.get_one(Experiment, child_run.id).manifest
        assert manifest["participants"][0]["model"] == "qwen/new-model"
        assert manifest["participants"][0]["persona"] == "Updated before fork"
        child_before = source_snapshot(repo, child_run.id)
        second = research.fork(repo, thread_id=first["thread_id"],
                               at_post_id=first["post_id_map"][posts[2].id], author="two")
        assert source_snapshot(repo, child_run.id) == child_before
        assert source_snapshot(repo, source.id) == before
        assert second["lineage"]["parent_run_id"] == first["run_id"]
        assert [ancestor["run_id"] for ancestor in second["lineage"]["ancestors"]] == [source.id, first["run_id"]]
        assert second["post_id_map"][posts[0].id] == repo.list_posts(second["thread_id"])[0].id


def test_fork_allows_roster_policy_and_fresh_limit_overrides(store):
    with store.begin() as session:
        repo = Repository(session)
        source, thread, posts, agents = seed(repo)
        child = research.fork(repo, thread_id=thread.id, at_post_id=posts[0].id, author="researcher",
                              policy="permissive", agent_ids=[agents[1].id], continuous=True,
                              config_overrides={"model_retries": 2}, limits={"max_rounds": 7, "max_tokens": 900})
        run = repo.get_run(child["run_id"])
        assert run.config["agent_ids"] == [agents[1].id]
        assert run.config["policy"]["profile"] == "permissive"
        assert run.config["model_retries"] == 2
        assert run.max_rounds == 7 and run.max_tokens == 900 and run.continuous
        assert run.state == "created"  # Route starts it after committing.


def test_inherit_remaining_carries_run_and_per_participant_budgets(store):
    with store.begin() as session:
        repo = Repository(session)
        source, thread, posts, agents = seed(repo)
        source.config = {**source.config, "use_virtual_time": True}
        repo.control_run(source.id, "start")
        repo.increment_run_counters(source.id, rounds=3, tokens=125, virtual_time=12)
        child = research.fork(repo, thread_id=thread.id, at_post_id=posts[2].id,
                              author="researcher", inherit_remaining=True)
        run = repo.get_run(child["run_id"])
        assert run.max_rounds == source.max_rounds - 3
        assert run.max_posts == source.max_posts - 4
        assert run.max_tokens == source.max_tokens - 125
        assert run.max_duration_seconds == source.max_duration_seconds - 12
        assert run.config["inherited_agent_quota_remaining"] == {agent.id: source.per_agent_quota - 1 for agent in agents}
        assert run.config["inherited_thread_quota_remaining"] == source.per_thread_quota - 2
        assert run.posts_used == 0 and run.virtual_time == 0


@pytest.mark.parametrize("invalid", ["other_post", "disabled_roster", "collaboration_type", "exhausted_budget",
                                     "unknown_cadence", "invalid_cadence_roster", "cadence_runtime_override"])
def test_invalid_fork_requests_leave_no_child_rows_or_events(store, invalid):
    with store.begin() as session:
        repo = Repository(session)
        source, thread, posts, agents = seed(repo)
        kwargs = {}
        post_id = posts[0].id
        if invalid == "other_post":
            other = repo.create_human_thread(title="Other", body="Unrelated")
            post_id = other.post.id
        elif invalid == "disabled_roster":
            agents[0].enabled = False
        elif invalid == "collaboration_type":
            kwargs["config_overrides"] = {"session_type": "collaboration"}
        elif invalid == "unknown_cadence":
            kwargs["config_overrides"] = {"cadence": "invented"}
        elif invalid == "invalid_cadence_roster":
            kwargs["config_overrides"] = {"cadence": cadence.NAME}
        elif invalid == "cadence_runtime_override":
            kwargs["config_overrides"] = {"cadence_index": 5}
        else:
            source.tokens_used = source.max_tokens
            kwargs["inherit_remaining"] = True
        session.flush()
        counts = [session.scalar(select(func.count()).select_from(model)) for model in (Run, Thread, Post, Event)]
        with pytest.raises(InvalidStateError):
            research.fork(repo, thread_id=thread.id, at_post_id=post_id, author="researcher", **kwargs)
        assert [session.scalar(select(func.count()).select_from(model)) for model in (Run, Thread, Post, Event)] == counts


def test_resample_is_idempotent_and_creates_only_forced_sibling_work(store):
    with store.begin() as session:
        repo = Repository(session)
        source, thread, posts, agents = seed(repo)
        turn = repo.create_turn(thread_id=thread.id, agent_id=agents[0].id,
                                context_post_ids=[posts[0].id], prompt='[{"role":"user","content":"Exact prompt\\n"}]')
        repo.finish_turn(turn.id, state="completed", resulting_post_id=posts[1].id,
                          raw_output='{"action":"reply"}')
        before = source_snapshot(repo, source.id)
        result = research.resample(repo, turn_id=turn.id, n=3, author="researcher", idempotency_key="sample-1")
        assert source_snapshot(repo, source.id) == before
        assert len(result["forks"]) == 3
        assert len({child["run_id"] for child in result["forks"]}) == 3
        for child in result["forks"]:
            assert len(repo.list_posts(child["thread_id"])) == 1
            stimuli = repo.list_stimuli(run_id=child["run_id"])
            assert len(stimuli) == 1
            assert stimuli[0].payload["forced"] is True
            assert stimuli[0].payload["forced_by"] == "researcher"
            assert stimuli[0].payload["reuse_turn_id"] == turn.id
            assert stimuli[0].target_agent_id == agents[0].id
            assert repo.get_run(child["run_id"]).config["sibling_group_id"] == result["sibling_group_id"]
        before_counts = (session.scalar(select(func.count(Run.id))), session.scalar(select(func.count(Event.id))))
        assert research.resample(repo, turn_id=turn.id, n=3, author="researcher", idempotency_key="sample-1") == result
        assert (session.scalar(select(func.count(Run.id))), session.scalar(select(func.count(Event.id)))) == before_counts
        with pytest.raises(InvalidStateError, match="request key"):
            research.resample(repo, turn_id=turn.id, n=2, author="researcher", idempotency_key="sample-1")


def test_force_turn_validates_request_identity_before_duplicate_delivery(store):
    with store.begin() as session:
        repo = Repository(session)
        source, thread, posts, agents = seed(repo)
        forced = research.force_turn(repo, run_id=source.id, thread_id=thread.id,
                                      agent_id=agents[0].id, author="researcher", idempotency_key="force-1")
        assert research.force_turn(repo, run_id=source.id, thread_id=thread.id,
                                   agent_id=agents[0].id, author="researcher", idempotency_key="force-1").id == forced.id
        with pytest.raises(InvalidStateError, match="request key"):
            research.force_turn(repo, run_id=source.id, thread_id=thread.id,
                                 agent_id=agents[1].id, author="researcher", idempotency_key="force-1")
