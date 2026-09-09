"""Participant previews share captured context semantics and disclose no lineage."""
from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import func, select

from swarmboard import autonomy, cadence
from swarmboard.models import Event, Post, Run, Stimulus, Thread, Turn
from swarmboard.repository import Repository
from tests.test_research_forced import force_client, prepared_session


def all_counts(app):
    with app.state.session_factory() as session:
        return tuple(session.scalar(select(func.count()).select_from(model))
                     for model in (Run, Thread, Post, Stimulus, Turn, Event))


async def preview(client, tid, aid, **query):
    response = await client.get(f"/api/threads/{tid}/participant-view", params={"agent_id": aid, **query})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_participant_preview_is_read_only_and_inherited_history_looks_ordinary(force_client):
    from swarmboard import research
    app, client, _ = force_client
    rid, tid, ids = prepared_session(app)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        second = repo.create_agent_post(tid, ids[0], "An original participant contribution.").post
        fork = research.fork(repo, thread_id=tid, at_post_id=second.id, author="researcher")
    before = all_counts(app)
    view = await preview(client, fork["thread_id"], ids[0])
    assert all_counts(app) == before
    assert view["agent_id"] == ids[0] and view["thread_id"] == fork["thread_id"]
    posts = view["context"]["posts"]
    assert len(posts) == 2 and posts[1]["body"] == "An original participant contribution."
    assert posts[1]["author_agent_id"] == ids[0]
    assert "is_inherited" not in json.dumps(view["context"])
    assert "inherited_from" not in view["prompt"]
    assert view["prompt_sha256"] == hashlib.sha256(view["prompt"].encode()).hexdigest()
    with app.state.session_factory() as session:
        durable = [session.get(Post, post["id"]) for post in posts]
        assert all(post.metadata_json["is_inherited"] for post in durable)
        assert session.get(Run, rid).config["session_type"] == "collaboration"


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed_cadence", [False, True])
async def test_historical_participant_preview_excludes_future_posts_and_cross_thread_environment(force_client, fixed_cadence):
    app, client, _ = force_client
    if fixed_cadence:
        with app.state.session_factory.begin() as session:
            repo = Repository(session)
            peer = repo.list_agents(enabled_only=True)[0]
            ada = repo.create_agent(handle="ada", provider="codex", model="gpt-6-astra", persona="Ada participant")
            run = autonomy.create_session(repo, agents=[ada, peer], continuous=False, cadence_mode=cadence.NAME)
            thread = session.scalar(select(Thread).where(Thread.run_id == run.id))
            rid, tid, ids = run.id, thread.id, [ada.id, peer.id]
    else:
        rid, tid, ids = prepared_session(app)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        old = repo.create_human_post(tid, "A historical anchor for this preview.").post
        cutoff = old.id
        repo.create_human_post(tid, "FUTURE_PRIVATE_CONTENT_THAT_MUST_NOT_LEAK")
        future_thread = repo.create_thread(run_id=rid, title="FUTURE_THREAD_TITLE_THAT_MUST_NOT_LEAK")
        repo.create_human_post(future_thread.id, "FUTURE_CROSS_THREAD_BODY_THAT_MUST_NOT_LEAK")
    before = all_counts(app)
    historical = await preview(client, tid, ids[0], at_post_id=cutoff)
    assert "FUTURE_" not in json.dumps(historical["context"])
    assert "FUTURE_" not in historical["prompt"]
    assert historical["context"]["posts"][-1]["id"] == cutoff
    current = await preview(client, tid, ids[0])
    assert "FUTURE_PRIVATE_CONTENT" in current["prompt"]
    assert "FUTURE_CROSS_THREAD_BODY" in current["prompt"]
    assert all_counts(app) == before


@pytest.mark.asyncio
async def test_participant_preview_validates_roster_and_cutoff_ownership(force_client):
    app, client, _ = force_client
    _, tid, ids = prepared_session(app)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        outsider = next(agent.id for agent in repo.list_agents() if agent.id not in ids)
        other = repo.create_thread(title="Different source")
        other_post = repo.create_human_post(other.id, "Different history").post.id
    before = all_counts(app)
    for params in ({"agent_id": outsider}, {"agent_id": ids[0], "at_post_id": other_post}):
        result = await client.get(f"/api/threads/{tid}/participant-view", params=params)
        assert result.status_code == 409, result.text
    assert all_counts(app) == before


@pytest.mark.asyncio
async def test_terminal_thread_keeps_read_only_participant_view(force_client):
    app, client, _ = force_client
    rid, tid, ids = prepared_session(app)
    with app.state.session_factory.begin() as session:
        Repository(session).set_run_state(rid, "completed")
    before = all_counts(app)
    view = await preview(client, tid, ids[0])
    assert view["context"]["posts"][0]["body"] == "An opening for the participants."
    assert all_counts(app) == before
