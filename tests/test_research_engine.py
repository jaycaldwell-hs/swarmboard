"""Research controls exercised through real SQLite and the engine boundary."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from swarmboard import autonomy, cadence, codex_gateway
from swarmboard.codex_gateway import CodexGateway
from swarmboard.engine import EngineConfig, SwarmEngine
from swarmboard.gateways import AgentAction, GatewayError, StructuredOutputError, TokenUsage
from swarmboard.models import Event, Post, Run, Stimulus, Thread, Turn, utc_now
from swarmboard.repository import Repository
from swarmboard.run_policy import PERMISSIVE, PRODUCTION
from tests.test_engine_acceptance import ScriptedGateway, make_database


def seed(factory, *, policy="permissive", handles=("peer",), body="@peer, start.", cadence_mode="free"):
    with factory.begin() as session:
        repo = Repository(session)
        agents = [repo.create_agent(
            handle=handle, persona=f"{handle} persona", cooldown_seconds=3600,
            provider="codex" if handle == "ada" else "openai_compatible",
            model="gpt-6-astra" if handle == "ada" else "qwen/qwen3.8-27b",
            settings={} if handle == "ada" else {
                "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"},
        ) for handle in handles]
        run = autonomy.create_session(repo, agents=agents, body=body, continuous=False,
                                      session_type="research", policy=policy, cadence_mode=cadence_mode,
                                      limits={"max_rounds": 20, "max_tokens": 20000})
        thread = session.scalar(select(Thread).where(Thread.run_id == run.id))
        return run.id, thread.id, [agent.id for agent in agents]


def engine_for(factory, gateway):
    return SwarmEngine(factory, gateway=gateway, config=EngineConfig(retry_backoff_seconds=0))


@pytest.mark.asyncio
async def test_permissive_duplicate_consecutive_and_cooldown_posts_commit_and_rerun_clones_policy(tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / 'duplicates.db'}")
    rid, tid, _ = seed(factory)
    action = AgentAction(action="reply", body="@peer, this repeats verbatim.", intent="clarify")
    gateway = ScriptedGateway(*[action for _ in range(6)])
    engine = engine_for(factory, gateway)
    for _ in range(6):
        result = await engine.step(rid)
        assert len(result.post_ids) == 1, result.as_dict()
    with factory() as session:
        posts = list(session.scalars(select(Post).where(Post.thread_id == tid).order_by(Post.sequence)))
        turns = list(session.scalars(select(Turn).where(Turn.run_id == rid)))
        assert len(posts) == 7 and {p.body for p in posts[1:]} == {action.body}
        assert len({p.idempotency_key for p in posts[1:]}) == 6
        assert all(turn.outcome == "executed" and turn.policy_snapshot == PERMISSIVE
                   and turn.session_type == "research" for turn in turns)
        assert all(turn.raw_output == action.model_dump_json() for turn in turns)
    rerun = await engine.rerun(rid)
    with factory() as session:
        clone = session.get(Run, rerun)
        assert clone.config["session_type"] == "research" and clone.config["policy"] == PERMISSIVE
        assert clone.rounds_used == 0 and clone.state == "created"
    assert len(gateway.calls) == 6
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,expected", [("permissive", 7), ("production", 4)])
async def test_ping_pong_policy_applies_at_selection_without_changing_output(profile, expected, tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / ('ping-' + profile + '.db')}")
    # Isolate loop policy from cooldown and dedup so each rejection is attributable.
    policy = {"profile": profile, "cooldowns": False, "dedup": False}
    rid, tid, _ = seed(factory, policy=policy, handles=("peer", "other"))
    def respond(participant, messages):
        target = "other" if participant.handle == "peer" else "peer"
        return AgentAction(action="reply", body=f"@{target}, continue.", intent="clarify")
    gateway = ScriptedGateway(*[respond for _ in range(7)])
    engine = engine_for(factory, gateway)
    for _ in range(7):
        await engine.step(rid)
    with factory() as session:
        assert len(list(session.scalars(select(Post).where(Post.thread_id == tid)))) == expected + 1
        if profile == "production":
            event = session.scalar(select(Event).where(Event.run_id == rid, Event.event_type == "scheduler.no_selection"))
            assert "ping-pong" in str(event.payload)
    assert len(gateway.calls) == expected
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_production_research_cooldown_defers_target_after_human_input(tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / 'cooldown.db'}")
    rid, tid, ids = seed(factory, policy="production")
    gateway = ScriptedGateway(AgentAction(action="reply", body="A new contribution.", intent="clarify"))
    engine = engine_for(factory, gateway)
    await engine.step(rid)
    with factory.begin() as session:
        repo = Repository(session)
        human = repo.create_human_post(tid, "@peer, another question?").post
        stimulus = repo.add_stimulus(run_id=rid, thread_id=tid, source_post_id=human.id,
                                     target_agent_id=ids[0], kind="mention", priority=100)
        sid = stimulus.id
    await engine.step(rid)
    with factory() as session:
        stimulus = session.get(Stimulus, sid)
        assert stimulus.state == "pending" and stimulus.attempts == 0
        assert stimulus.not_before > utc_now() + timedelta(minutes=50)
        assert len(list(session.scalars(select(Turn).where(Turn.run_id == rid)))) == 1
    assert len(gateway.calls) == 1
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_production_research_rejects_duplicate_and_retains_exact_output(tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / 'dedup.db'}")
    opening = "@peer, start."
    rid, tid, _ = seed(factory, policy="production", body=opening)
    action = AgentAction(action="reply", body=opening, intent="clarify")
    gateway = ScriptedGateway(action)
    engine = engine_for(factory, gateway)
    await engine.step(rid)
    with factory() as session:
        turn = session.scalar(select(Turn).where(Turn.run_id == rid))
        assert turn.outcome == "rejected_by_policy" and "duplicate" in turn.rejection_reason
        assert turn.raw_output == action.model_dump_json() and turn.parsed_action == action.model_dump()
        assert turn.resulting_post_id is None
        assert len(list(session.scalars(select(Post).where(Post.thread_id == tid)))) == 1
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_production_research_consecutive_cap_blocks_next_self_turn(tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / 'cap.db'}")
    rid, _, _ = seed(factory, policy={"cooldowns": False})
    gateway = ScriptedGateway(AgentAction(action="reply", body="@peer, continue.", intent="clarify"))
    engine = engine_for(factory, gateway)
    await engine.step(rid)
    await engine.step(rid)
    with factory() as session:
        event = session.scalar(select(Event).where(Event.run_id == rid, Event.event_type == "scheduler.no_selection"))
        assert "consecutive turn cap" in str(event.payload)
        assert len(list(session.scalars(select(Turn).where(Turn.run_id == rid)))) == 1
    assert len(gateway.calls) == 1
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_research_cap_larger_than_context_window_uses_complete_streak(tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / 'long-cap.db'}")
    rid, tid, ids = seed(factory, policy={"profile": "permissive", "consecutive_turn_cap": 150})
    with factory.begin() as session:
        repo = Repository(session)
        run = repo.get_run(rid)
        run.max_posts = 500
        run.per_agent_quota = 500
        run.per_thread_quota = 500
        for index in range(150):
            repo.create_agent_post(tid, ids[0], f"Existing contribution {index}.")
    gateway = ScriptedGateway()
    engine = engine_for(factory, gateway)
    await engine.step(rid)
    with factory() as session:
        event = session.scalar(select(Event).where(Event.run_id == rid, Event.event_type == "scheduler.no_selection"))
        assert "consecutive turn cap" in str(event.payload)
        assert session.scalar(select(Turn).where(Turn.run_id == rid)) is None
    assert gateway.calls == []
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_research_dedup_checks_latest_posts_beyond_first_page(tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / 'long-dedup.db'}")
    rid, tid, _ = seed(factory, policy="production")
    latest = "The late human contribution copied by the model."
    with factory.begin() as session:
        repo = Repository(session)
        repo.get_run(rid).max_posts = 1000
        for index in range(500):
            repo.create_human_post(tid, f"Earlier human observation {index}.")
        repo.create_human_post(tid, latest)
    gateway = ScriptedGateway(AgentAction(action="reply", body=latest, intent="clarify"))
    engine = engine_for(factory, gateway)
    await engine.step(rid)
    with factory() as session:
        turn = session.scalar(select(Turn).where(Turn.run_id == rid))
        assert turn.outcome == "rejected_by_policy" and "duplicate" in turn.rejection_reason
        assert turn.resulting_post_id is None
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["production", "permissive"])
async def test_fresh_commit_cooldown_consults_run_policy_after_another_thread_posts(profile, tmp_path, monkeypatch):
    db, factory = make_database(f"sqlite:///{tmp_path / ('commit-' + profile + '.db')}")
    rid, tid, ids = seed(factory, policy=profile)
    action = AgentAction(action="reply", body="The pending model contribution.", intent="clarify")
    gateway = ScriptedGateway(action)
    engine = engine_for(factory, gateway)
    original_commit = engine._commit_action
    async def intervening_commit(turn_id, **kwargs):
        # Represent a concurrent successful turn in another thread, after the
        # initial action check and before the commit transaction starts.
        with factory.begin() as session:
            repo = Repository(session)
            other = repo.create_thread(run_id=rid, title="Concurrent thread")
            generated = repo.create_agent_post(other.id, ids[0], "A concurrent contribution.").post
            side_turn = repo.create_turn(thread_id=other.id, run_id=rid, agent_id=ids[0])
            repo.finish_turn(side_turn.id, state="completed", resulting_post_id=generated.id)
        return await original_commit(turn_id, **kwargs)
    monkeypatch.setattr(engine, "_commit_action", intervening_commit)
    await engine.step(rid)
    with factory() as session:
        turn = session.scalar(select(Turn).where(Turn.thread_id == tid))
        assert turn.raw_output == action.model_dump_json()
        if profile == "production":
            assert turn.outcome == "rejected_by_policy" and "cooldown" in turn.rejection_reason
            assert turn.resulting_post_id is None
        else:
            assert turn.outcome == "executed" and turn.resulting_post_id is not None
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    '\r\n  {"action":"reply", "body":"Record this", "intent":"clarify", "unknown":{"nested":[1,2]}}  \r\n',
    "\r\n this is not JSON \t\r\n",
])
async def test_capture_invalid_output_is_verbatim_artifact_without_retry_post_or_disabling_participant(raw, tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / 'capture.db'}")
    rid, tid, ids = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, rid)
        run.config = {**run.config, "model_retries": 3}
    gateway = ScriptedGateway(StructuredOutputError("invalid schema output", raw_output=raw,
                                                    usage=TokenUsage(3, 4, 7), latency_ms=12))
    engine = engine_for(factory, gateway)
    await engine.step(rid)
    with factory() as session:
        turn = session.scalar(select(Turn).where(Turn.run_id == rid))
        assert turn.raw_output == raw and turn.outcome == "invalid_output"
        assert turn.rejection_reason == turn.error and "invalid schema" in turn.error
        assert turn.validated_action is None and turn.resulting_post_id is None
        assert turn.policy_snapshot == PERMISSIVE and turn.session_type == "research"
        assert turn.total_tokens == 7 and turn.latency_ms == 12
        assert len(turn.retry_history) == 1 and turn.retry_history[0]["raw_output"] == raw
        assert len(list(session.scalars(select(Post).where(Post.thread_id == tid)))) == 1
        assert ids[0] not in session.get(Run, rid).config.get("unavailable_agent_ids", [])
        assert turn.parsed_action == (json.loads(raw) if "unknown" in raw else None)
    assert len(gateway.calls) == 1
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_production_invalid_output_keeps_existing_retry_behavior_and_raw_attempt(tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / 'strict.db'}")
    rid, _, _ = seed(factory, policy="production")
    with factory.begin() as session:
        run = session.get(Run, rid)
        run.config = {**run.config, "model_retries": 2}
    invalid = ' \r\n{"action":"other", "unexpected":42}\r\n '
    valid = AgentAction(action="pass")
    gateway = ScriptedGateway(StructuredOutputError("unknown action", raw_output=invalid), valid)
    engine = engine_for(factory, gateway)
    await engine.step(rid)
    with factory() as session:
        turn = session.scalar(select(Turn).where(Turn.run_id == rid))
        assert turn.outcome == "passed" and turn.raw_output == valid.model_dump_json()
        assert turn.retry_history[0]["raw_output"] == invalid and turn.policy_snapshot == PRODUCTION
        assert session.get(Run, rid).model_calls == 2
    assert len(gateway.calls) == 2
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
async def test_provider_failure_is_a_recorded_turn_without_fabricated_raw_or_post(tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / 'failure.db'}")
    rid, tid, _ = seed(factory)
    gateway = ScriptedGateway(GatewayError("provider temporarily unavailable", retryable=False))
    engine = engine_for(factory, gateway)
    await engine.step(rid)
    with factory() as session:
        turn = session.scalar(select(Turn).where(Turn.run_id == rid))
        assert turn.outcome == "provider_failure" and turn.rejection_reason == turn.error
        assert turn.raw_output is None and turn.parsed_action is None and turn.resulting_post_id is None
        assert len(list(session.scalars(select(Post).where(Post.thread_id == tid)))) == 1
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed_cadence", [False, True])
async def test_dormancy_off_keeps_quiet_thread_active_in_free_and_cadenced_sessions(fixed_cadence, tmp_path):
    db, factory = make_database(f"sqlite:///{tmp_path / 'dormancy.db'}")
    rid, tid, _ = seed(factory, handles=("ada", "peer") if fixed_cadence else ("peer",),
                      cadence_mode=cadence.NAME if fixed_cadence else "free")
    gateway = ScriptedGateway(*[AgentAction(action="pass") for _ in range(3)])
    engine = engine_for(factory, gateway)
    await engine.step(rid)
    if fixed_cadence:
        await engine.step(rid)
    with factory() as session:
        assert session.get(Thread, tid).status == "active"
        if fixed_cadence:
            assert not session.get(Run, rid).config.get("cadence_quiet")
    if fixed_cadence:
        await engine.step(rid)
        assert len(gateway.calls) == 3
    await engine.shutdown()
    db.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [True, False])
async def test_codex_raw_capture_preserves_crlf_and_surrounding_whitespace(monkeypatch, valid):
    raw = '\r\n  {"action":"pass","parent_post_id":null,"title":null,"body":null,"intent":null}'
    if not valid:
        raw = raw[:-1] + ',"unknown": true}'
    raw += '  \r\n\t'
    async def launch(*args, **kwargs):
        output = Path(args[args.index("--output-last-message") + 1])
        output.write_bytes(raw.encode("utf-8"))
        async def communicate(prompt):
            return b'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n', b""
        return SimpleNamespace(returncode=0, communicate=communicate)
    monkeypatch.setattr(codex_gateway.asyncio, "create_subprocess_exec", launch)
    for key in ("SWARMBOARD_CODEX_AUTH", "SWARMBOARD_CODEX_API_KEY", "CODEX_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    gateway = CodexGateway()
    kwargs = {"model": "gpt-6-astra", "messages": [{"role": "system", "content": "Persona"}, {"role": "user", "content": "Context"}]}
    if valid:
        assert (await gateway.complete(**kwargs)).raw_output == raw
    else:
        with pytest.raises(StructuredOutputError) as failure:
            await gateway.complete(**kwargs)
        assert failure.value.raw_output == raw
