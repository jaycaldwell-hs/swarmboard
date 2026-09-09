from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier

import pytest
from sqlalchemy import Engine, delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from swarmboard.database import init_db, make_engine, make_session_factory
from swarmboard.models import (
    Agent,
    Event,
    Post,
    RunState,
    StimulusKind,
    StimulusState,
    Thread,
    ThreadStatus,
    TurnState,
    utc_now,
)
from swarmboard.repository import (
    IdempotencyConflictError,
    InvalidStateError,
    Repository,
    RepositoryError,
)


@pytest.fixture
def sqlite_store(
    tmp_path: Path,
) -> Iterator[tuple[Engine, sessionmaker[Session], Path]]:
    path = tmp_path / "swarmboard-test.db"
    engine = make_engine(f"sqlite+pysqlite:///{path}")
    init_db(engine)
    factory = make_session_factory(engine)
    try:
        yield engine, factory, path
    finally:
        engine.dispose()


def test_post_and_event_roll_back_as_one_atomic_write(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    engine, factory, _ = sqlite_store
    with factory.begin() as session:
        thread_id = Repository(session).create_thread(title="Atomicity").id

    # Cause only the audit insert to fail. If post and event were separate
    # transactions, this would leave an orphan post (and consume a sequence).
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER reject_post_created_for_test
            BEFORE INSERT ON events
            WHEN NEW.event_type = 'post.created'
            BEGIN
                SELECT RAISE(ABORT, 'reject post event for atomicity test');
            END
            """
        )

    with pytest.raises(IntegrityError, match="reject post event for atomicity test"):
        with factory.begin() as session:
            Repository(session).create_human_post(thread_id, "This must roll back")

    with factory() as session:
        thread = session.get_one(Thread, thread_id)
        assert thread.current_sequence == 0
        assert list(session.scalars(select(Post).where(Post.thread_id == thread_id))) == []
        assert list(
            session.scalars(
                select(Event).where(
                    Event.thread_id == thread_id,
                    Event.event_type == "post.created",
                )
            )
        ) == []


def test_event_rows_reject_updates_and_deletes(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    _, factory, _ = sqlite_store
    with factory.begin() as session:
        write = Repository(session).create_human_thread(title="Audit", body="Keep this")
        event_id = write.event.id
        original_uuid = write.event.uuid

    with pytest.raises(IntegrityError, match="events are immutable"):
        with factory.begin() as session:
            session.execute(
                update(Event).where(Event.id == event_id).values(event_type="tampered")
            )

    with pytest.raises(IntegrityError, match="events are immutable"):
        with factory.begin() as session:
            session.execute(delete(Event).where(Event.id == event_id))

    with factory() as session:
        event = session.get_one(Event, event_id)
        assert event.uuid == original_uuid
        assert event.event_type == "post.created"


def test_agent_post_retry_is_idempotent(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    _, factory, _ = sqlite_store
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="critic", persona="Find flaws.", model="qwen3:8b")
        thread = repo.create_thread(title="Retry")
        stimulus = repo.add_stimulus(
            thread_id=thread.id,
            kind=StimulusKind.MANUAL_STEP,
            target_agent_id=agent.id,
            dedupe_key="retry-stimulus",
        )
        agent_id, thread_id, stimulus_id = agent.id, thread.id, stimulus.id

    with factory.begin() as session:
        first = Repository(session).create_agent_post(
            thread_id,
            agent_id,
            "One durable answer.",
            intent="synthesize",
            stimulus_id=stimulus_id,
        )
        first_post_id, first_event_id = first.post.id, first.event.id
        assert first.created is True

    # A new session models a redelivery after the first transaction committed.
    with factory.begin() as session:
        retry = Repository(session).create_agent_post(
            thread_id,
            agent_id,
            "One durable answer.",
            intent="synthesize",
            stimulus_id=stimulus_id,
        )
        assert retry.created is False
        assert retry.post.id == first_post_id
        assert retry.event.id == first_event_id

    with factory() as session:
        posts = list(session.scalars(select(Post).where(Post.thread_id == thread_id)))
        events = list(
            session.scalars(
                select(Event).where(
                    Event.thread_id == thread_id,
                    Event.event_type == "post.created",
                )
            )
        )
        assert [post.id for post in posts] == [first_post_id]
        assert [event.id for event in events] == [first_event_id]
        assert session.get_one(Thread, thread_id).current_sequence == 1


def test_human_post_cannot_preempt_an_agent_turn_delivery_key(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    _, factory, _ = sqlite_store
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="critic", persona="Check.", model="qwen3:8b")
        thread = repo.create_thread(title="Collision")
        stimulus = repo.add_stimulus(
            thread_id=thread.id,
            kind=StimulusKind.MANUAL_STEP,
            target_agent_id=agent.id,
            dedupe_key="collision-stimulus",
        )
        turn_key = f"turn:{stimulus.id}:{agent.id}"
        human = repo.create_human_post(
            thread.id,
            "A human deliberately used a visible turn key.",
            idempotency_key=turn_key,
        )
        agent_id, thread_id, stimulus_id = agent.id, thread.id, stimulus.id
        human_post_id = human.post.id

    with factory.begin() as session:
        with pytest.raises(IdempotencyConflictError, match="different post operation"):
            Repository(session).create_agent_post(
                thread_id,
                agent_id,
                "This must never alias the human post.",
                idempotency_key=turn_key,
                stimulus_id=stimulus_id,
            )

    with factory() as session:
        posts = list(session.scalars(select(Post).where(Post.idempotency_key == turn_key)))
        assert [post.id for post in posts] == [human_post_id]
        assert posts[0].author_type == "human"
        assert session.get_one(Agent, agent_id).last_spoke_at is None


def test_human_key_collision_cannot_leave_an_agent_thread_or_event(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    _, factory, _ = sqlite_store
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="proposer", persona="Propose.", model="qwen3:8b")
        source_thread = repo.create_thread(title="Human source")
        stimulus = repo.add_stimulus(
            thread_id=source_thread.id,
            kind=StimulusKind.MANUAL_STEP,
            target_agent_id=agent.id,
            dedupe_key="new-thread-collision-stimulus",
        )
        turn_key = f"turn:{stimulus.id}:{agent.id}"
        repo.create_human_post(
            source_thread.id,
            "Human ownership wins; the agent must fail safely.",
            idempotency_key=turn_key,
        )
        agent_id, stimulus_id = agent.id, stimulus.id

    with factory() as session:
        baseline_thread_ids = set(session.scalars(select(Thread.id)))
        baseline_thread_event_ids = set(
            session.scalars(
                select(Event.id).where(Event.event_type == "thread.created")
            )
        )

    with factory.begin() as session:
        with pytest.raises(IdempotencyConflictError, match="different post operation"):
            Repository(session).create_agent_thread(
                title="Must not survive",
                agent_id=agent_id,
                body="Must not alias the human post.",
                idempotency_key=turn_key,
                stimulus_id=stimulus_id,
            )

    with factory() as session:
        assert set(session.scalars(select(Thread.id))) == baseline_thread_ids
        assert set(
            session.scalars(
                select(Event.id).where(Event.event_type == "thread.created")
            )
        ) == baseline_thread_event_ids
        assert len(list(session.scalars(select(Post)))) == 1


def test_agent_delivery_key_requires_matching_agent_stimulus_and_operation(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    _, factory, _ = sqlite_store
    with factory.begin() as session:
        repo = Repository(session)
        first_agent = repo.create_agent(handle="first", persona="First.", model="qwen3:8b")
        second_agent = repo.create_agent(handle="second", persona="Second.", model="qwen3:8b")
        thread = repo.create_thread(title="Provenance")
        first = repo.create_agent_post(
            thread.id,
            first_agent.id,
            "Owned by one agent and stimulus.",
            idempotency_key="agent-owned-key",
            stimulus_id="stimulus-one",
        )
        first_agent_id, second_agent_id, thread_id = (
            first_agent.id,
            second_agent.id,
            thread.id,
        )
        post_id = first.post.id

    with factory.begin() as session:
        repo = Repository(session)
        retry = repo.create_agent_post(
            thread_id,
            first_agent_id,
            "Exact retry.",
            idempotency_key="agent-owned-key",
            stimulus_id="stimulus-one",
        )
        assert retry.created is False
        assert retry.post.id == post_id
        with pytest.raises(IdempotencyConflictError):
            repo.create_agent_post(
                thread_id,
                second_agent_id,
                "Wrong agent.",
                idempotency_key="agent-owned-key",
                stimulus_id="stimulus-one",
            )
        with pytest.raises(IdempotencyConflictError):
            repo.create_agent_post(
                thread_id,
                first_agent_id,
                "Wrong stimulus.",
                idempotency_key="agent-owned-key",
                stimulus_id="stimulus-two",
            )
        with pytest.raises(IdempotencyConflictError):
            repo.create_agent_thread(
                title="Wrong operation",
                agent_id=first_agent_id,
                body="A reply cannot become a new thread.",
                idempotency_key="agent-owned-key",
                stimulus_id="stimulus-one",
            )


def test_global_post_idempotency_key_preserves_null_multiplicity_and_migrates(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    engine, factory, _ = sqlite_store

    # Model an existing database created before the global index was added.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_posts_idempotency_key")
    init_db(engine)
    init_db(engine)  # The migration itself is safe to run on every startup.

    with engine.connect() as connection:
        indexes = connection.exec_driver_sql("PRAGMA index_list('posts')").mappings().all()
        global_indexes = [row for row in indexes if row["name"] == "uq_posts_idempotency_key"]
        assert len(global_indexes) == 1
        assert global_indexes[0]["unique"] == 1

    with factory.begin() as session:
        repo = Repository(session)
        first_thread = repo.create_thread(title="First")
        second_thread = repo.create_thread(title="Second")
        first = repo.create_human_post(
            first_thread.id,
            "Globally identified",
            idempotency_key="one-logical-delivery",
        )
        with pytest.raises(IdempotencyConflictError):
            repo.create_human_post(
                second_thread.id,
                "A different operation cannot claim the delivery.",
                idempotency_key="one-logical-delivery",
            )
        null_one = repo.create_human_post(first_thread.id, "No delivery key one")
        null_two = repo.create_human_post(second_thread.id, "No delivery key two")

        assert first.post.thread_id == first_thread.id
        assert null_one.post.id != null_two.post.id

    with factory() as session:
        assert len(
            list(
                session.scalars(
                    select(Post).where(Post.idempotency_key == "one-logical-delivery")
                )
            )
        ) == 1
        null_key_posts = list(
            session.scalars(select(Post).where(Post.idempotency_key.is_(None)))
        )
        assert len(null_key_posts) == 2


def test_init_db_adds_turn_claim_token_to_an_existing_database(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    engine, factory, _ = sqlite_store
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE turns DROP COLUMN claim_token")
        old_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('turns')")
        }
        assert "claim_token" not in old_columns

    init_db(engine)
    init_db(engine)  # Repeated process startups must remain safe.

    with engine.connect() as connection:
        columns = {
            row["name"]: row
            for row in connection.exec_driver_sql("PRAGMA table_info('turns')").mappings()
        }
        assert columns["claim_token"]["type"] == "VARCHAR(36)"
        assert columns["claim_token"]["notnull"] == 0

    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="lease-owner", persona="Own work.", model="qwen3:8b")
        thread = repo.create_thread(title="Migrated turn")
        turn = repo.create_turn(
            thread_id=thread.id,
            agent_id=agent.id,
            claim_token="persisted-lease-token",
        )
        turn_id = turn.id

    with factory() as session:
        assert Repository(session).get_turn(turn_id).claim_token == "persisted-lease-token"


def test_agent_handles_are_normalized_for_create_upsert_and_lookup(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    _, factory, _ = sqlite_store
    with factory.begin() as session:
        repo = Repository(session)
        created = repo.create_agent(
            handle="  @CrItIc  ",
            persona="Original.",
            model="qwen3:8b",
        )
        assert created.handle == "critic"
        assert repo.get_agent_by_handle("@CRITIC").id == created.id
        updated = repo.upsert_agent(
            handle="cRiTiC",
            persona="Updated.",
            model="qwen3:8b",
        )
        assert updated.id == created.id
        assert updated.persona == "Updated."
        with pytest.raises(RepositoryError, match="already exists"):
            repo.create_agent(handle="CRITIC", persona="Duplicate.", model="qwen3:8b")


def test_concurrent_case_variant_agent_handles_have_one_owner(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    _, factory, _ = sqlite_store
    rendezvous = Barrier(2)

    def create(handle: str) -> tuple[str, str]:
        rendezvous.wait(timeout=5)
        try:
            with factory.begin() as session:
                agent = Repository(session).create_agent(
                    handle=handle,
                    persona="Concurrent.",
                    model="qwen3:8b",
                )
                return "created", agent.id
        except RepositoryError as exc:
            return "conflict", str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["Synth", "sYnTh"]))

    assert sorted(result[0] for result in results) == ["conflict", "created"]
    with factory() as session:
        agents = list(session.scalars(select(Agent)))
        created_events = list(
            session.scalars(select(Event).where(Event.event_type == "agent.created"))
        )
        assert [agent.handle for agent in agents] == ["synth"]
        assert len(created_events) == 1


def test_handle_migration_preserves_and_audits_legacy_case_collisions(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    engine, factory, _ = sqlite_store
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_agents_handle_nocase")

    older_at = utc_now() - timedelta(hours=1)
    newer_at = utc_now()
    with factory.begin() as session:
        older = Agent(
            handle="Critic",
            persona="Older.",
            model="qwen3:8b",
            created_at=older_at,
            updated_at=older_at,
        )
        newer = Agent(
            handle="CRITIC",
            persona="Newer.",
            model="qwen3:8b",
            created_at=newer_at,
            updated_at=newer_at,
        )
        session.add_all([older, newer])
        session.flush()
        older_id, newer_id = older.id, newer.id

    init_db(engine)
    init_db(engine)

    with factory() as session:
        agents = list(session.scalars(select(Agent).order_by(Agent.created_at, Agent.id)))
        assert [agent.id for agent in agents] == [older_id, newer_id]
        assert agents[0].handle == "critic"
        assert agents[1].handle == f"critic-legacy-{newer_id[:8]}"
        migration_events = list(
            session.scalars(
                select(Event)
                .where(Event.event_type == "agent.handle_migrated")
                .order_by(Event.id)
            )
        )
        assert len(migration_events) == 2
        assert all(set(event.payload) == {"old_handle", "new_handle"} for event in migration_events)
        assert {event.agent_id for event in migration_events} == {older_id, newer_id}

    with pytest.raises(IntegrityError):
        with factory.begin() as session:
            session.add(Agent(handle="cRiTiC", persona="Blocked.", model="qwen3:8b"))


def test_global_idempotency_migration_preserves_legacy_duplicate_posts(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    engine, factory, _ = sqlite_store
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX uq_posts_idempotency_key")

    older_at = utc_now() - timedelta(hours=1)
    newer_at = utc_now()
    with factory.begin() as session:
        repo = Repository(session)
        older_thread = repo.create_thread(title="Older legacy delivery")
        newer_thread = repo.create_thread(title="Newer legacy delivery")
        older_thread.current_sequence = 1
        newer_thread.current_sequence = 1
        older = Post(
            thread_id=older_thread.id,
            author_type="human",
            author_handle="human",
            body="Preserve the original key here.",
            sequence=1,
            idempotency_key="formerly-thread-scoped",
            created_at=older_at,
        )
        newer = Post(
            thread_id=newer_thread.id,
            author_type="human",
            author_handle="human",
            body="Preserve this post under a migrated key.",
            sequence=1,
            idempotency_key="formerly-thread-scoped",
            created_at=newer_at,
        )
        session.add_all([older, newer])
        session.flush()
        repo.add_event("post.created", thread_id=older_thread.id, post_id=older.id)
        repo.add_event("post.created", thread_id=newer_thread.id, post_id=newer.id)
        older_id, newer_id = older.id, newer.id

    init_db(engine)
    with factory() as session:
        older_key = session.get_one(Post, older_id).idempotency_key
        newer_key = session.get_one(Post, newer_id).idempotency_key
        assert older_key == "formerly-thread-scoped"
        assert newer_key == f"legacy-duplicate:{newer_id}"
        assert len(list(session.scalars(select(Post)))) == 2
        migrated_post_events = list(
            session.scalars(select(Event).where(Event.post_id.in_([older_id, newer_id])))
        )
        assert len(migrated_post_events) == 2

    # Re-running startup is a no-op for already migrated rows and indexes.
    init_db(engine)
    with factory() as session:
        assert session.get_one(Post, older_id).idempotency_key == older_key
        assert session.get_one(Post, newer_id).idempotency_key == newer_key


def test_concurrent_agent_new_thread_delivery_creates_one_post_and_thread(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    _, factory, _ = sqlite_store
    with factory.begin() as session:
        agent = Repository(session).create_agent(
            handle="proposer",
            persona="Start useful discussions.",
            model="qwen3:8b",
        )
        agent_id = agent.id

    rendezvous = Barrier(2)

    class RacingRepository(Repository):
        """Force both deliveries to finish their initial read before either write."""

        def __init__(self, session: Session):
            super().__init__(session)
            self._rendezvoused = False

        def _post_result_for_idempotency_key(
            self, idempotency_key: str | None, **expectations: object
        ):
            result = super()._post_result_for_idempotency_key(
                idempotency_key, **expectations
            )
            if not self._rendezvoused:
                self._rendezvoused = True
                assert result is None
                rendezvous.wait(timeout=5)
            return result

    def deliver() -> tuple[bool, str, str]:
        with factory.begin() as session:
            result = RacingRepository(session).create_agent_thread(
                title="One logical topic",
                agent_id=agent_id,
                body="One logical opening post.",
                intent="challenge",
                idempotency_key="turn:one-new-thread-action",
            )
            return result.created, result.post.id, result.post.thread_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: deliver(), range(2)))

    assert sorted(created for created, _, _ in results) == [False, True]
    assert len({post_id for _, post_id, _ in results}) == 1
    assert len({thread_id for _, _, thread_id in results}) == 1
    with factory() as session:
        assert len(list(session.scalars(select(Post)))) == 1
        assert len(list(session.scalars(select(Thread)))) == 1
        assert len(
            list(session.scalars(select(Event).where(Event.event_type == "thread.created")))
        ) == 1


def test_file_database_enables_wal_and_safety_pragmas(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    engine, _, path = sqlite_store
    assert path.exists()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5_000
        assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1


def test_restart_requeues_a_claimed_stimulus(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    first_engine, first_factory, path = sqlite_store
    with first_factory.begin() as session:
        repo = Repository(session)
        run = repo.create_run(seed=91)
        thread = repo.create_thread(title="Survive restart", run_id=run.id)
        stimulus = repo.add_stimulus(
            run_id=run.id,
            thread_id=thread.id,
            kind=StimulusKind.MANUAL_STEP,
            max_attempts=3,
            dedupe_key="restart-stimulus",
        )
        run_id, stimulus_id = run.id, stimulus.id

    with first_factory.begin() as session:
        claimed = Repository(session).claim_stimuli(run_id=run_id, limit=1)
        assert [item.id for item in claimed] == [stimulus_id]
        assert claimed[0].state == StimulusState.CLAIMED.value
        assert claimed[0].claim_token is not None

    # Rebuild the engine/session factory from the same file, as a new process
    # would. No in-memory queue participates in recovery.
    first_engine.dispose()
    restarted_engine = make_engine(f"sqlite+pysqlite:///{path}")
    init_db(restarted_engine)
    restarted_factory = make_session_factory(restarted_engine)
    try:
        with restarted_factory.begin() as session:
            recovered = Repository(session).recover_inflight_work(stale_before=utc_now())
            assert recovered.stimuli_requeued == 1
            assert recovered.stimuli_failed == 0

        with restarted_factory() as session:
            stimulus = Repository(session).get_stimulus(stimulus_id)
            assert stimulus.state == StimulusState.PENDING.value
            assert stimulus.claim_token is None
            assert stimulus.claimed_at is None
            assert stimulus.attempts == 1
            recovered_events = list(
                session.scalars(
                    select(Event).where(
                        Event.stimulus_id == stimulus_id,
                        Event.event_type == "stimulus.recovered",
                    )
                )
            )
            assert len(recovered_events) == 1

        with restarted_factory.begin() as session:
            reclaimed = Repository(session).claim_stimuli(run_id=run_id, limit=1)
            assert [item.id for item in reclaimed] == [stimulus_id]
            assert reclaimed[0].attempts == 2
    finally:
        restarted_engine.dispose()


@pytest.mark.parametrize(
    "terminal_state",
    [
        RunState.STOPPED,
        RunState.COMPLETED,
        RunState.EMERGENCY_STOPPED,
        RunState.FAILED,
    ],
)
def test_terminal_run_allows_final_close_but_rejects_conversation_mutation(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
    terminal_state: RunState,
) -> None:
    _, factory, _ = sqlite_store
    with factory.begin() as session:
        repo = Repository(session)
        run = repo.create_run(seed=41)
        thread = repo.create_thread(title="Frozen history", run_id=run.id)
        post = repo.create_human_post(
            thread.id,
            "The final accepted post.",
            idempotency_key=f"frozen-post:{terminal_state.value}",
        )
        stimulus = repo.add_stimulus(
            run_id=run.id,
            thread_id=thread.id,
            kind=StimulusKind.HUMAN_POST,
            source_post_id=post.post.id,
            dedupe_key=f"frozen-stimulus:{terminal_state.value}",
        )
        repo.set_run_state(run.id, terminal_state, reason="test terminal freeze")
        assert stimulus.state == "cancelled"
        assert stimulus.completed_at is not None
        assert stimulus.claim_token is None
        run_id, thread_id = run.id, thread.id
        post_id, stimulus_id = post.post.id, stimulus.id

    with factory() as session:
        repo = Repository(session)
        baseline_event_ids = [event.id for event in repo.list_events(run_id=run_id)]
        baseline_thread_ids = set(session.scalars(select(Thread.id)))
        assert repo.get_run(run_id).posts_used == 1

    with factory.begin() as session:
        repo = Repository(session)
        post_retry = repo.create_human_post(
            thread_id,
            "The final accepted post.",
            idempotency_key=f"frozen-post:{terminal_state.value}",
        )
        stimulus_retry = repo.add_stimulus(
            run_id=run_id,
            thread_id=thread_id,
            kind=StimulusKind.HUMAN_POST,
            source_post_id=post_id,
            dedupe_key=f"frozen-stimulus:{terminal_state.value}",
        )
        assert post_retry.created is False
        assert post_retry.post.id == post_id
        assert stimulus_retry.id == stimulus_id
        assert repo.set_thread_status(thread_id, ThreadStatus.ACTIVE).status == "active"
        with pytest.raises(InvalidStateError, match="rerun or create a fresh thread"):
            repo.set_thread_status(thread_id, ThreadStatus.DORMANT)
        assert repo.set_thread_status(thread_id, ThreadStatus.CLOSED).status == "closed"
        with pytest.raises(InvalidStateError, match="rerun or create a fresh thread"):
            repo.set_thread_status(thread_id, ThreadStatus.ACTIVE)
        with pytest.raises(InvalidStateError, match="rerun or create a fresh thread"):
            repo.create_human_post(
                thread_id,
                "This must not extend terminal history.",
                idempotency_key=f"new-terminal-post:{terminal_state.value}",
            )
        with pytest.raises(InvalidStateError, match="rerun or create a fresh thread"):
            repo.add_stimulus(
                run_id=run_id,
                thread_id=thread_id,
                kind=StimulusKind.NEW_EVIDENCE,
                source_post_id=post_id,
                dedupe_key=f"new-terminal-stimulus:{terminal_state.value}",
            )
        with pytest.raises(InvalidStateError, match="rerun or create a fresh thread"):
            repo.create_human_thread(
                title="Must not attach",
                body="Terminal runs cannot gain a thread.",
                run_id=run_id,
                idempotency_key=f"new-terminal-thread:{terminal_state.value}",
            )

    with factory() as session:
        repo = Repository(session)
        assert repo.get_run(run_id).posts_used == 1
        events = repo.list_events(run_id=run_id)
        assert [event.id for event in events[:-1]] == baseline_event_ids
        assert events[-1].event_type == "thread.closed"
        assert events[-1].thread_id == thread_id
        assert [post.id for post in repo.list_posts(thread_id)] == [post_id]
        assert set(session.scalars(select(Thread.id))) == baseline_thread_ids


def test_recovery_can_be_scoped_to_one_run(
    sqlite_store: tuple[Engine, sessionmaker[Session], Path],
) -> None:
    _, factory, _ = sqlite_store
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="recoverer", persona="Recover.", model="qwen3:8b")
        ids: list[tuple[str, str, str]] = []
        for index in range(2):
            run = repo.create_run(seed=index)
            thread = repo.create_thread(title=f"Run {index}", run_id=run.id)
            stimulus = repo.add_stimulus(
                run_id=run.id,
                thread_id=thread.id,
                kind=StimulusKind.MANUAL_STEP,
                target_agent_id=agent.id,
                max_attempts=3,
                dedupe_key=f"scoped-recovery-{index}",
            )
            ids.append((run.id, thread.id, stimulus.id))
        agent_id = agent.id

    turn_ids: list[str] = []
    stale_at = utc_now() - timedelta(minutes=5)
    fresh_at = utc_now()
    with factory.begin() as session:
        repo = Repository(session)
        for index, (run_id, thread_id, stimulus_id) in enumerate(ids):
            claimed = repo.claim_stimuli(run_id=run_id, limit=1)[0]
            turn = repo.create_turn(
                run_id=run_id,
                thread_id=thread_id,
                agent_id=agent_id,
                stimulus_id=stimulus_id,
                claim_token=claimed.claim_token,
            )
            repo.mark_turn_calling(turn.id)
            claimed.updated_at = stale_at if index == 0 else fresh_at
            # Deliberately make both turns old.  The second is protected by its
            # fresh stimulus lease rather than by its own start timestamp.
            turn.started_at = stale_at
            turn_ids.append(turn.id)

    with factory.begin() as session:
        recovered = Repository(session).recover_inflight_work(
            run_id=ids[0][0],
            stale_before=utc_now() - timedelta(minutes=2),
        )
        assert recovered.stimuli_requeued == 1
        assert recovered.stimuli_failed == 0
        assert recovered.turns_failed == 1

    with factory() as session:
        repo = Repository(session)
        assert repo.get_stimulus(ids[0][2]).state == StimulusState.PENDING.value
        assert repo.get_turn(turn_ids[0]).state == TurnState.FAILED.value
        assert repo.get_stimulus(ids[1][2]).state == StimulusState.CLAIMED.value
        assert repo.get_turn(turn_ids[1]).state == TurnState.CALLING.value

    with factory.begin() as session:
        fresh_recovery = Repository(session).recover_inflight_work(
            run_id=ids[1][0],
            stale_before=utc_now() - timedelta(minutes=2),
        )
        assert fresh_recovery.stimuli_requeued == 0
        assert fresh_recovery.stimuli_failed == 0
        assert fresh_recovery.turns_failed == 0
