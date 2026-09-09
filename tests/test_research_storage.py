from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from swarmboard.database import init_db, make_engine, make_session_factory
from swarmboard.models import Event, Run, Turn, TurnState, utc_now
from swarmboard.repository import InvalidStateError, Repository
from swarmboard.run_policy import normalize_config


@pytest.fixture
def store(tmp_path: Path) -> Iterator[tuple[Engine, sessionmaker[Session]]]:
    engine = make_engine(f"sqlite:///{tmp_path / 'research-storage.db'}")
    init_db(engine)
    try:
        yield engine, make_session_factory(engine)
    finally:
        engine.dispose()


def test_run_policy_is_normalized_and_rejected_before_any_write(store) -> None:
    _, factory = store
    with factory.begin() as session:
        repo = Repository(session)
        run = repo.create_run(config={"retained": "value"})
        assert run.config == normalize_config({"retained": "value"})
        event = session.scalar(select(Event).where(Event.event_type == "run.created"))
        assert event.payload["session_type"] == "collaboration"
        assert event.payload["policy"] == run.config["policy"]
        before = session.scalar(select(func.count(Event.id)))
        with pytest.raises(InvalidStateError):
            repo.create_run(config={"session_type": "collaboration", "policy": {"profile": "permissive"}})
        assert session.scalar(select(func.count(Run.id))) == 1
        assert session.scalar(select(func.count(Event.id))) == before


def test_turn_policy_snapshot_survives_later_config_changes(store) -> None:
    engine, factory = store
    with factory.begin() as session:
        repo = Repository(session)
        run = repo.create_run(config={"session_type": "research", "policy": {"profile": "permissive"}})
        agent = repo.create_agent(handle="writer", persona="Test participant", model="test")
        thread = repo.create_thread(title="Research", run_id=run.id)
        turn = repo.create_turn(thread_id=thread.id, agent_id=agent.id)
        assert turn.session_type == "research"
        assert turn.policy_snapshot == run.config["policy"]
        original_policy = dict(turn.policy_snapshot)
        run.config = normalize_config({"session_type": "research", "policy": {"profile": "production"}})
        run_id, turn_id = run.id, turn.id
    init_db(engine)
    with factory() as session:
        turn = session.get_one(Turn, turn_id)
        assert turn.policy_snapshot == original_policy
        assert session.get_one(Run, run_id).config["policy"]["profile"] == "production"
        selected = session.scalar(select(Event).where(Event.event_type == "turn.selected"))
        assert selected.payload["session_type"] == "research"
        assert selected.payload["policy_snapshot"] == original_policy


@pytest.mark.parametrize(("state", "error", "outcome"), [
    (TurnState.COMPLETED, None, "executed"),
    (TurnState.PASSED, None, "passed"),
    (TurnState.FAILED, "policy rejected action: duplicate content", "rejected_by_policy"),
    (TurnState.FAILED, "invalid JSON at character 2: Expecting value", "invalid_output"),
    (TurnState.FAILED, "model request timed out", "provider_failure"),
])
def test_terminal_outcomes_and_raw_output_are_captured_exactly(store, state, error, outcome) -> None:
    _, factory = store
    raw = ' \r\n{"action":"unknown", "extra": {"verbatim":"café\\n雪"}}\t\n '
    parsed = {"action": "unknown", "extra": {"verbatim": "café\n雪"}}
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="writer", persona="Test participant", model="test")
        thread = repo.create_thread(title="Capture")
        turn = repo.create_turn(thread_id=thread.id, agent_id=agent.id)
        repo.finish_turn(turn.id, state=state, error=error, raw_output=raw, parsed_action=parsed)
        assert turn.outcome == outcome
        assert turn.rejection_reason == error
        turn_id = turn.id
        # Delivery retries cannot rewrite a terminal turn's original artifact.
        repo.finish_turn(turn.id, state=state, raw_output="replacement")
    with factory() as session:
        turn = session.get_one(Turn, turn_id)
        assert turn.raw_output.encode("utf-8") == raw.encode("utf-8")
        assert turn.parsed_action == parsed
        event = session.scalar(select(Event).where(Event.event_type == f"turn.{state}"))
        assert event.payload["outcome"] == outcome
        assert event.payload["rejection_reason"] == error
        assert event.payload["policy_snapshot"] == turn.policy_snapshot


def test_explicit_invalid_output_outcome_and_reason_are_preserved(store) -> None:
    _, factory = store
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="writer", persona="Test participant", model="test")
        thread = repo.create_thread(title="Invalid output")
        turn = repo.create_turn(thread_id=thread.id, agent_id=agent.id)
        with pytest.raises(ValueError, match="unknown turn outcome"):
            repo.finish_turn(turn.id, state=TurnState.FAILED, outcome="invented")
        assert turn.state == "selected"
        raw = '  {"unexpected": true}\r\n'
        repo.finish_turn(
            turn.id, state=TurnState.FAILED, raw_output=raw,
            parsed_action={"unexpected": True}, outcome="invalid_output",
            rejection_reason="missing action", error="schema rejected",
        )
        assert turn.raw_output == raw
        assert turn.outcome == "invalid_output"
        assert turn.rejection_reason == "missing action"


@pytest.mark.parametrize("operation", ["stop", "recovery"])
def test_turns_terminalized_without_a_model_result_keep_outcomes(store, operation) -> None:
    _, factory = store
    with factory.begin() as session:
        repo = Repository(session)
        run = repo.create_run(config={"session_type": "research", "policy": "permissive"})
        agent = repo.create_agent(handle="writer", persona="Test participant", model="test")
        thread = repo.create_thread(title="Interrupted", run_id=run.id)
        stimulus = repo.add_stimulus(thread_id=thread.id, kind="manual_step")
        claimed = repo.claim_stimuli(run_id=run.id)[0]
        turn = repo.create_turn(
            thread_id=thread.id, agent_id=agent.id,
            stimulus_id=stimulus.id, claim_token=claimed.claim_token,
        )
        repo.mark_turn_calling(turn.id)
        if operation == "stop":
            repo.control_run(run.id, "stop", reason="researcher stopped the run")
            expected = "rejected_by_policy"
            event_type = "turn.failed"
        else:
            repo.recover_inflight_work(run_id=run.id, stale_before=utc_now())
            expected = "provider_failure"
            event_type = "turn.recovered_failed"
        assert turn.state == "failed"
        assert turn.outcome == expected
        assert turn.rejection_reason == turn.error
        assert turn.raw_output is None
        event = session.scalar(select(Event).where(Event.event_type == event_type))
        assert event.payload["outcome"] == expected
        assert event.payload["session_type"] == "research"
        assert event.payload["policy_snapshot"] == turn.policy_snapshot


def test_repeatable_legacy_upgrade_preserves_events_and_raw_output(store) -> None:
    engine, factory = store
    cases = [
        ("completed", None, "executed"),
        ("passed", None, "passed"),
        ("failed", "policy rejected action: duplicate content", "rejected_by_policy"),
        ("failed", "invalid agent action: extra field", "invalid_output"),
        ("failed", "ollama request timed out", "provider_failure"),
        ("calling", None, None),
    ]
    raw = ' \r\n{"unrecognized": "snow 雪"}\t '
    turn_ids = []
    with factory.begin() as session:
        repo = Repository(session)
        run = repo.create_run(config={"legacy_key": [1, 2]})
        agent = repo.create_agent(handle="writer", persona="Test participant", model="test")
        thread = repo.create_thread(title="Legacy", run_id=run.id)
        for state, error, _ in cases:
            turn = repo.create_turn(thread_id=thread.id, agent_id=agent.id)
            turn.state, turn.error, turn.raw_output = state, error, raw
            turn_ids.append(turn.id)
        run_id = run.id
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE runs SET config = ? WHERE id = ?",
            (json.dumps({"legacy_key": [1, 2]}), run_id),
        )
        for column in ("session_type", "policy_snapshot", "outcome", "rejection_reason"):
            connection.exec_driver_sql(f"ALTER TABLE turns DROP COLUMN {column}")
        original_events = list(connection.exec_driver_sql("SELECT * FROM events ORDER BY id"))
        original_output = list(connection.exec_driver_sql("SELECT id, raw_output FROM turns ORDER BY id"))
        original_triggers = list(connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
        ))
    for _ in range(2):
        init_db(engine)
        with engine.connect() as connection:
            assert list(connection.exec_driver_sql("SELECT * FROM events ORDER BY id")) == original_events
            assert list(connection.exec_driver_sql("SELECT id, raw_output FROM turns ORDER BY id")) == original_output
            assert list(connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' ORDER BY name"
            )) == original_triggers
        with factory() as session:
            run = session.get_one(Run, run_id)
            assert run.config == normalize_config({"legacy_key": [1, 2]})
            for turn_id, (_, error, expected_outcome) in zip(turn_ids, cases):
                turn = session.get_one(Turn, turn_id)
                assert turn.session_type == "collaboration"
                assert turn.policy_snapshot == run.config["policy"]
                assert turn.outcome == expected_outcome
                assert turn.rejection_reason == error
                assert turn.raw_output == raw
