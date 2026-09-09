from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from swarmboard import autonomy, sessions
from swarmboard.app import create_app
from swarmboard.codex_gateway import CodexGateway
from swarmboard.engine import SwarmEngine
from swarmboard.gateways import AgentAction, GatewayError, OpenAICompatibleGateway
from swarmboard.models import Agent, Event, Experiment, Post, Run, Scenario, Stimulus, Thread, Turn
from swarmboard.repository import InvalidStateError, Repository
from .test_engine_acceptance import ScriptedGateway, BlockingGateway, make_database, wait_until
from .test_harness import persona_directory


def seed_session(factory, **kwargs):
    with factory.begin() as session:
        repo = Repository(session)
        ada = repo.create_agent(handle="ada", provider="codex", model="gpt-6-astra", persona="Ada persona")
        peer = repo.create_agent(handle="peer", provider="openai_compatible", model="open-model", persona="Peer persona")
        run = autonomy.create_session(repo, agents=[ada, peer], body="@ada, choose a direction.", **kwargs)
        return run.id, ada.id, peer.id


@pytest.mark.asyncio
async def test_structured_codex_safety_error_is_classified_without_diagnostics(monkeypatch):
    event = {"type": "turn.failed", "error": {"code": "misalignment_policy_violation", "message": "private diagnostic"}}
    process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(json.dumps(event).encode(), b"private stderr")))
    monkeypatch.setattr("swarmboard.codex_gateway.asyncio.create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(GatewayError) as err:
        await CodexGateway().complete(model="gpt-6-astra", messages=[{"role": "system", "content": "test"}, {"role": "user", "content": "test"}])
    assert err.value.category == "safety_block" and not err.value.retryable and "private" not in str(err.value)


@pytest.mark.asyncio
async def test_openrouter_content_filter_and_auth_are_not_retried():
    for status, payload, category in [(403, {"error": {"code": "misalignment_policy_violation"}}, "safety_block"),
                                      (401, {"error": {"code": "invalid_api_key"}}, "authentication"),
                                      (200, {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]}, "safety_block")]:
        gateway = OpenAICompatibleGateway("https://openrouter.ai/api/v1", transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload)))
        with pytest.raises(GatewayError) as err:
            await gateway.complete(model="test", messages=[{"role": "user", "content": "test"}])
        assert err.value.category == category and not err.value.retryable


@pytest.mark.asyncio
async def test_safety_block_stops_session_and_cannot_restart():
    db, factory = make_database(); rid, _, _ = seed_session(factory)
    gateway = ScriptedGateway(GatewayError("provider safety block", category="safety_block", retryable=True))
    engine = SwarmEngine(factory, gateway=gateway)
    await engine.step(rid); await engine.step(rid)
    with factory() as session:
        result = sessions.activity(Repository(session), rid)
        assert result["state"] == "failed" and result["stop_reason"].startswith("safety_block:")
        assert result["metrics"]["failed_turns"] == 1 and len(gateway.calls) == 1
        assert not list(session.scalars(select(Stimulus).where(Stimulus.run_id == rid, Stimulus.state.in_(["pending", "claimed", "processing"]))))
    with pytest.raises(InvalidStateError, match="cannot be retried"):
        await engine.rerun(rid)
    await engine.shutdown(); db.dispose()


@pytest.mark.asyncio
async def test_stop_fences_inflight_board_action_but_retains_failed_turn():
    db, factory = make_database(); rid, _, _ = seed_session(factory)
    gateway = BlockingGateway(AgentAction(action="new_thread", title="Late thread", body="Too late.", intent="clarify"))
    engine = SwarmEngine(factory, gateway=gateway)
    task = asyncio.create_task(engine.step(rid))
    await asyncio.wait_for(gateway.entered.wait(), timeout=2)
    await engine.stop(rid); gateway.release.set(); await task
    with factory() as session:
        result = sessions.activity(Repository(session), rid)
        assert result["state"] == "stopped" and result["metrics"]["threads"] == 1
        assert len(result["actions"]) == 1 and result["actions"][0]["state"] == "failed"
        assert result["actions"][0]["resulting_post_id"] is None
        assert len(list(session.scalars(select(Post)))) == 1
    await engine.shutdown(); db.dispose()


@pytest.mark.asyncio
async def test_start_and_resume_drive_continuous_session():
    db, factory = make_database(); rid, _, _ = seed_session(factory, limits={"max_rounds": 2})
    gateway = BlockingGateway(AgentAction(action="pass"), AgentAction(action="pass"))
    engine = SwarmEngine(factory, gateway=gateway)
    await engine.start(rid)
    await asyncio.wait_for(gateway.entered.wait(), timeout=2)
    await engine.pause(rid); gateway.release.set()
    def completed_turn():
        with factory() as session: return session.get(Run, rid).rounds_used == 1
    await wait_until(completed_turn)
    with factory() as session: assert session.get(Run, rid).state == "paused"
    await engine.resume(rid)
    def completed_run():
        with factory() as session: return session.get(Run, rid).state == "completed"
    await wait_until(completed_run)
    assert len(gateway.calls) == 2
    await engine.shutdown(); db.dispose()


@pytest.mark.asyncio
async def test_runtime_versions_are_not_probed_during_session_creation_or_execution(monkeypatch):
    def unexpected_probe(*args, **kwargs):
        pytest.fail("session execution must not run version probes")
    monkeypatch.setattr("subprocess.run", unexpected_probe)
    db, factory = make_database(); rid, _, _ = seed_session(factory)
    gateway = ScriptedGateway(AgentAction(action="pass"))
    engine = SwarmEngine(factory, gateway=gateway)
    await engine.step(rid)
    with factory() as session:
        assert session.get(Run, rid).rounds_used == 1
        assert "runtime" not in session.get(Experiment, rid).manifest
        assert list(session.scalars(select(Scenario))) == []
    assert len(gateway.calls) == 1
    await engine.shutdown(); db.dispose()


def test_peer_migration_is_idempotent_and_preserves_personas():
    from swarmboard.peer_models import migrate_peers
    db, factory = make_database(); _, ada_id, peer_id = seed_session(factory)
    with factory.begin() as session:
        repo = Repository(session)
        ada = repo.get_agent(ada_id)
        before = (ada.model, ada.persona, copy.deepcopy(ada.settings), dict(ada.permissions))
        peer = repo.get_agent(peer_id)
        identity = (peer.id, peer.persona, peer.role, dict(peer.permissions))
        assert len(migrate_peers(repo)) == 1
        assert migrate_peers(repo) == []
        assert (ada.model, ada.persona, ada.settings, ada.permissions) == before
        assert (peer.id, peer.persona, peer.role, peer.permissions) == identity
    db.dispose()


@pytest.mark.asyncio
async def test_live_configuration_updates_future_turns_not_recorded_prompts():
    db, factory = make_database(); rid, ada_id, peer_id = seed_session(factory)
    seen = []
    def respond(agent, messages):
        seen.append((agent.model, agent.persona))
        return AgentAction(action="reply", body="@peer, continue.", intent="clarify")
    engine = SwarmEngine(factory, gateway=ScriptedGateway(respond, respond))
    await engine.step(rid)
    with factory.begin() as session:
        old_turn = session.scalar(select(Turn).where(Turn.run_id == rid))
        prior_prompt, prior_id = old_turn.prompt, old_turn.id
        peer = session.get(Agent, peer_id)
        peer.model = "updated-model"; peer.persona = "Updated peer persona."
        session.get(Agent, ada_id).persona = "Updated Ada persona."
    await engine.step(rid)
    with factory.begin() as session:
        assert session.get(Turn, prior_id).prompt == prior_prompt
        assert seen == [("gpt-6-astra", "Ada persona"), ("updated-model", "Updated peer persona.")]
        clone = sessions.restart(Repository(session), session.get(Run, rid))
        assert clone.config["agent_ids"] == [ada_id, peer_id]
        assert clone.continuous is False and clone.rounds_used == 0
        assert len(list(session.scalars(select(Post).join(Thread).where(Thread.run_id == clone.id)))) == 1
        session.get(Agent, peer_id).permissions = {"speak": False}
        with pytest.raises(InvalidStateError, match="speaking permission"):
            sessions.restart(Repository(session), session.get(Run, rid))
    await engine.shutdown(); db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["created", "running", "paused", "completed"])
async def test_legacy_scripted_history_survives_retirement_and_remains_read_only(state):
    db, factory = make_database()
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="archived_peer", provider="ollama", model="old-model", persona="Old persona")
        run = repo.create_run(continuous=True, config={"experiment": True, "interaction_mode": "scripted"})
        thread = repo.create_thread(title="Old discussion", run_id=run.id)
        post = repo.create_post(thread_id=thread.id, body="Old opening", author_type="system", author_handle="SIMULATOR")
        repo.add_stimulus(thread_id=thread.id, run_id=run.id, kind="new_evidence", source_post_id=post.post.id)
        manifest = {"scenario": {"name": "Old discussion", "opening": "Old opening"},
                    "participants": [{"id": agent.id, "handle": agent.handle, "provider": agent.provider, "model": agent.model}]}
        session.add(Experiment(run_id=run.id, manifest_sha256=sessions.digest(manifest), manifest=manifest, world={"credits": 7}))
        if state == "completed": repo.set_run_state(run.id, state)
        else: run.state = state
        rid, post_id = run.id, post.post.id
        event_ids = set(session.scalars(select(Event.id)))
    gateway = ScriptedGateway()
    app = create_app(session_factory=factory, gateway=gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            report = (await client.get(f"/api/sessions/{rid}")).json()
            assert report["archived"] and report["state"] == ("completed" if state == "completed" else "stopped")
            for control in ("start", "resume", "step", "rerun"):
                assert (await client.post(f"/api/runs/{rid}/{control}")).status_code == 409
            exported = (await client.get(f"/api/sessions/{rid}/export")).json()
            assert exported["setup"] == manifest and exported["world"] == {"credits": 7}
            replay = (await client.get(f"/api/runs/{rid}/replay")).json()
            assert replay["posts"][0]["id"] == post_id
            with factory() as session:
                assert event_ids <= set(session.scalars(select(Event.id)))
                assert not list(session.scalars(select(Stimulus).where(Stimulus.run_id == rid, Stimulus.state.in_(["pending", "claimed", "processing"]))))
    assert not gateway.calls
    db.dispose()


@pytest.mark.asyncio
async def test_old_autonomous_sessions_remain_runnable_and_restartable():
    db, factory = make_database()
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="peer", provider="openai_compatible", model="open-model", persona="Participant")
        run = repo.create_run(config={"experiment": True, "interaction_mode": "autonomous", "cadence": "free", "agent_ids": [agent.id]})
        thread = repo.create_thread(title="Earlier session", run_id=run.id)
        post = repo.create_human_post(thread.id, "@peer, continue.")
        manifest = {"scenario": {"name": thread.title, "opening": post.post.body}, "participants": [
            {"id": agent.id, "handle": agent.handle, "provider": agent.provider, "model": agent.model}]}
        session.add(Experiment(run_id=run.id, manifest_sha256=sessions.digest(manifest), manifest=manifest, world={}))
        session.flush()
        autonomy.seed_invitation(repo, run, thread, post.post, key="old-session")
        rid = run.id
    engine = SwarmEngine(factory, gateway=ScriptedGateway(AgentAction(action="pass")))
    await engine.step(rid)
    with factory.begin() as session:
        repo = Repository(session)
        assert not sessions.activity(repo, rid)["archived"]
        clone = sessions.restart(repo, repo.get_run(rid))
        assert clone.config["collaboration"] and not clone.config.get("experiment")
    await engine.shutdown(); db.dispose()


@pytest.mark.asyncio
async def test_session_export_includes_complete_threads_and_late_human_interventions():
    db, factory = make_database()
    rid, _, _ = seed_session(factory, continuous=False)
    gateway = ScriptedGateway(AgentAction(action="new_thread", title="A second discussion",
                                          body="Let's explore this separately.", intent="clarify"))
    engine = SwarmEngine(factory, gateway=gateway)
    await engine.step(rid)
    with factory.begin() as session:
        repo = Repository(session)
        threads = list(session.scalars(select(Thread).where(Thread.run_id == rid).order_by(Thread.created_at)))
        assert len(threads) == 2
        opening_thread, second_thread = threads
        reply_to = session.scalar(select(Post).where(Post.thread_id == second_thread.id))
        first_note = repo.create_human_post(opening_thread.id, "Researcher note after the model turn.",
                                           author_handle="admin").post
        final_note = repo.create_human_post(second_thread.id, "Please reconsider the assumption.",
                                           author_handle="cat", parent_post_id=reply_to.id,
                                           metadata={"intervention": "clarification"}).post
        unrelated = repo.create_thread(title="Outside this session")
        excluded = repo.create_human_post(unrelated.id, "Unrelated private discussion.").post
        first_note_id, final_note_id, excluded_id = first_note.id, final_note.id, excluded.id
        thread_ids = [thread.id for thread in threads]
    app = create_app(session_factory=factory, gateway=gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/sessions/{rid}/export")
            assert response.status_code == 200, response.text
            exported = response.json()
            replay = (await client.get(f"/api/runs/{rid}/replay")).json()
            assert exported["threads"] == replay["threads"]
            assert exported["posts"] == replay["posts"]
            assert [thread["id"] for thread in exported["threads"]] == thread_ids
            assert len(exported["turns"]) == 1
            posts = {post["id"]: post for post in exported["posts"]}
            assert len(posts) == 4 and excluded_id not in posts
            assert posts[first_note_id]["body"] == "Researcher note after the model turn."
            assert posts[final_note_id]["body"] == "Please reconsider the assumption."
            assert posts[final_note_id]["author_handle"] == "cat"
            assert posts[final_note_id]["parent_post_id"] is not None
            assert posts[final_note_id]["metadata"]["intervention"] == "clarification"
            assert posts[final_note_id]["created_at"] > exported["turns"][0]["completed_at"]
    assert len(gateway.calls) == 1
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_session_api_generic_roster_limits_idempotency_and_removed_research_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMBOARD_PERSONA_DIR", str(persona_directory(tmp_path)))
    app = create_app(database_url=f"sqlite:///{tmp_path / 'api.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with app.state.session_factory.begin() as session:
                repo = Repository(session)
                ids = [repo.create_agent(handle=handle, provider="openai_compatible", model="same-model", persona="Participant").id for handle in ("z_first", "a_second")]
            payload = {"agent_ids": ids, "continuous": False, "idempotency_key": "generic-session", "max_rounds": 7,
                       "max_tokens": 1200, "max_duration_seconds": 80}
            created = await client.post("/api/sessions", json=payload)
            assert created.status_code == 201, created.text
            assert (await client.post("/api/sessions", json=payload)).json() == created.json()
            assert (await client.post("/api/sessions", json={**payload, "title": "Changed"})).status_code == 409
            rid = created.json()["run_id"]
            with app.state.session_factory() as session:
                run = session.get(Run, rid)
                assert (run.max_rounds, run.max_tokens, run.max_duration_seconds) == (7, 1200, 80)
                assert run.max_cascade_depth == 7 and run.per_agent_quota == 7
                assert run.config["agent_ids"] == ids and run.config["cadence"] == "free"
                assert not run.config.get("experiment") and session.scalar(select(Scenario)) is None
                assert session.scalar(select(Agent).where(Agent.handle == "ada")) is None
                opening = session.scalar(select(Post).join(Thread).where(Thread.run_id == rid))
                assert opening.author_type == "human"
            activity = (await client.get(f"/api/sessions/{rid}")).json()
            assert not {"scoring_enabled", "reviews", "world", "manifest", "runtime_matches"} & activity.keys()
            assert [p["id"] for p in activity["participants"]] == ids
            for field, value in [("seed", 41), ("scenario_id", "old"), ("treatment", "paired"), ("neutral_peers", True), ("max_rounds", 0)]:
                assert (await client.post("/api/sessions", json={**payload, field: value})).status_code == 422
            invalid = await client.post("/api/sessions", json={**payload, "cadence": "ada_round_robin", "idempotency_key": "invalid"})
            assert invalid.status_code == 409
            ada_session = await client.post("/api/sessions", json={"include_ada": True, "continuous": False, "idempotency_key": "ada-only"})
            assert ada_session.status_code == 201, ada_session.text
            assert len((await client.get(f'/api/sessions/{ada_session.json()["run_id"]}')).json()["participants"]) == 1
            assert (await client.post(f"/api/sessions/{rid}/discard")).json()["discarded"]
            assert (await client.post("/api/sessions", json=payload)).status_code == 409
            for path in ("/api/experiments", "/api/experiments/scenarios", f"/api/experiments/{rid}/reviews"):
                assert (await client.post(path, json={})).status_code == 404
            assert (await client.get("/api/experiments/scenarios")).status_code == 404
            for path in ("/", "/sessions"):
                page = (await client.get(path)).text
                assert "/sessions" in page and "Each peer, then Ada" in page
                assert not any(term in page for term in ("scripted-fields", "review-form", "scenario-editor", 'name="seed"', "Shutdown Threat"))
            old_page = await client.get(f"/experiments?run={rid}")
            assert old_page.status_code == 307 and old_page.headers["location"] == f"/sessions?run={rid}"
