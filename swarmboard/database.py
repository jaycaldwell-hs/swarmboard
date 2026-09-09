"""Synchronous SQLite setup and transaction helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Engine, create_engine, event as sqlalchemy_event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import Base, infer_turn_outcome, normalize_agent_handle
from .run_policy import normalize_config


def default_database_url() -> str:
    configured_url = os.getenv("SWARMBOARD_DATABASE_URL")
    if configured_url:
        return configured_url
    configured_path = Path(os.getenv("SWARMBOARD_DB_PATH", "./swarmboard.db")).expanduser()
    return f"sqlite:///{configured_path.resolve()}"


def make_engine(database_url: str | None = None, *, echo: bool = False) -> Engine:
    """Create a SQLite engine with durable/concurrent local defaults.

    Tests can pass ``sqlite+pysqlite:///:memory:``.  A ``StaticPool`` keeps that
    in-memory database visible across sessions and threads.
    """

    url = make_url(database_url or default_database_url())
    if url.get_backend_name() != "sqlite":
        raise ValueError("Swarmboard currently supports SQLite database URLs only")

    connect_args: dict[str, object] = {"check_same_thread": False, "timeout": 30.0}
    engine_kwargs: dict[str, object] = {
        "connect_args": connect_args,
        "echo": echo,
        "future": True,
        "pool_pre_ping": True,
    }
    if url.database in (None, "", ":memory:"):
        engine_kwargs["poolclass"] = StaticPool

    db_engine = create_engine(url, **engine_kwargs)

    @sqlalchemy_event.listens_for(db_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: object, connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            # In-memory SQLite reports ``memory`` here; file-backed databases use WAL.
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    return db_engine


def make_session_factory(bind: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=bind, class_=Session, autoflush=False, expire_on_commit=False)


engine = make_engine()
SessionLocal = make_session_factory(engine)


def init_db(bind: Engine | None = None) -> None:
    """Create tables and install idempotent SQLite invariants.

    ``create_all`` does not add newly declared indexes to a table which already
    exists.  The explicit statement below therefore doubles as the migration
    from the original per-thread post-idempotency constraint.  SQLite unique
    indexes allow any number of NULL values while rejecting duplicate non-NULL
    delivery keys.
    """

    target = bind or engine
    Base.metadata.create_all(target)
    # SQLite has no table-level append-only declaration.  Triggers make the
    # contract explicit even if future code accidentally mutates an Event ORM
    # instance or issues a bulk DELETE.
    with target.begin() as connection:
        agent_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('agents')")
        }
        if "persona_version" not in agent_columns:
            connection.exec_driver_sql(
                "ALTER TABLE agents ADD COLUMN persona_version INTEGER NOT NULL DEFAULT 1"
            )
            # Earlier intervention builds kept this counter in settings. Move
            # its meaning into a dedicated column without rewriting settings
            # or replaying stale counters on subsequent application starts.
            for row in connection.exec_driver_sql("SELECT id, settings FROM agents").mappings():
                settings = json.loads(row["settings"]) if row["settings"] else {}
                version = settings.get("persona_version") if isinstance(settings, dict) else None
                if type(version) is int and 0 < version < 2**63:
                    connection.exec_driver_sql(
                        "UPDATE agents SET persona_version = ? WHERE id = ?", (version, row["id"])
                    )

        # ``create_all`` does not add columns to an existing table.  Turn lease
        # ownership was introduced after the first schema shipped, so upgrade
        # older SQLite files in place before the ORM attempts to load a Turn.
        turn_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('turns')")
        }
        if "claim_token" not in turn_columns:
            connection.exec_driver_sql(
                "ALTER TABLE turns ADD COLUMN claim_token VARCHAR(36)"
            )

        for column, declaration in {
            "session_type": "VARCHAR(24) NOT NULL DEFAULT 'collaboration'",
            "policy_snapshot": "JSON NOT NULL DEFAULT '{}'",
            "outcome": "VARCHAR(32)",
            "rejection_reason": "TEXT",
        }.items():
            if column not in turn_columns:
                connection.exec_driver_sql(
                    f"ALTER TABLE turns ADD COLUMN {column} {declaration}"
                )

        # Upgrade the materialized records only. Historical events are immutable
        # facts and must never be rewritten to resemble newly captured traces.
        run_configs: dict[str, dict[str, object]] = {}
        for row in connection.exec_driver_sql("SELECT id, config FROM runs").mappings():
            prior_config = json.loads(row["config"])
            normalized_config = normalize_config(prior_config)
            run_configs[row["id"]] = normalized_config
            if normalized_config != prior_config:
                connection.exec_driver_sql(
                    "UPDATE runs SET config = ? WHERE id = ?",
                    (json.dumps(normalized_config), row["id"]),
                )
        default_config = normalize_config(None)
        historical_turns = list(connection.exec_driver_sql(
            "SELECT id, run_id, state, error, policy_snapshot, outcome, rejection_reason "
            "FROM turns WHERE outcome IS NULL OR policy_snapshot IS NULL "
            "OR policy_snapshot = '{}'"
        ).mappings())
        for row in historical_turns:
            prior_policy = json.loads(row["policy_snapshot"]) if row["policy_snapshot"] else None
            if not prior_policy:
                config = run_configs.get(row["run_id"], default_config)
                connection.exec_driver_sql(
                    "UPDATE turns SET session_type = ?, policy_snapshot = ? WHERE id = ?",
                    (config["session_type"], json.dumps(config["policy"]), row["id"]),
                )
            outcome = row["outcome"] or infer_turn_outcome(row["state"], row["error"])
            if row["outcome"] is None and outcome is not None:
                connection.exec_driver_sql(
                    "UPDATE turns SET outcome = ?, rejection_reason = ? WHERE id = ?",
                    (
                        outcome,
                        row["rejection_reason"] or (
                            row["error"] if outcome not in {"executed", "passed"} else None
                        ),
                        row["id"],
                    ),
                )

        # Mentions use case-folded handles, so persistence must use the same
        # identity.  Preserve legacy case-colliding agents by keeping the first
        # `(created_at, id)` owner of the canonical name and deterministically
        # suffixing later records.  A temporary phase avoids the old binary
        # UNIQUE constraint blocking case-only swaps during migration.
        legacy_agents = list(
            connection.exec_driver_sql(
                "SELECT id, handle FROM agents ORDER BY created_at, id"
            ).mappings()
        )
        migrated_handles: list[tuple[str, str, str]] = []
        final_handles: set[str] = set()
        for row in legacy_agents:
            agent_id = str(row["id"])
            old_handle = str(row["handle"])
            try:
                base_handle = normalize_agent_handle(old_handle)
            except ValueError:
                base_handle = f"agent-{agent_id[:8].casefold()}"
            new_handle = base_handle
            suffix_number = 1
            while new_handle.casefold() in final_handles:
                suffix = f"-legacy-{agent_id[:8].casefold()}"
                if suffix_number > 1:
                    suffix += f"-{suffix_number}"
                new_handle = f"{base_handle[: 80 - len(suffix)]}{suffix}"
                suffix_number += 1
            final_handles.add(new_handle.casefold())
            if old_handle != new_handle:
                migrated_handles.append((agent_id, old_handle, new_handle))

        occupied_handles = {str(row["handle"]).casefold() for row in legacy_agents}
        temporary_handles: dict[str, str] = {}
        for agent_id, _, _ in migrated_handles:
            temporary = f"migration-{agent_id.casefold()}"
            suffix_number = 1
            while temporary.casefold() in occupied_handles:
                temporary = f"migration-{agent_id.casefold()}-{suffix_number}"
                suffix_number += 1
            occupied_handles.add(temporary.casefold())
            temporary_handles[agent_id] = temporary
            connection.exec_driver_sql(
                "UPDATE agents SET handle = ? WHERE id = ?",
                (temporary, agent_id),
            )

        for agent_id, old_handle, new_handle in migrated_handles:
            connection.exec_driver_sql(
                "UPDATE agents SET handle = ? WHERE id = ?",
                (new_handle, agent_id),
            )
            connection.exec_driver_sql(
                """
                INSERT INTO events (
                    uuid, event_type, agent_id, actor_type, actor_id, payload, created_at
                ) VALUES (?, 'agent.handle_migrated', ?, 'system',
                          'schema_migration', ?, CURRENT_TIMESTAMP)
                """,
                (
                    str(uuid4()),
                    agent_id,
                    json.dumps(
                        {"old_handle": old_handle, "new_handle": new_handle},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )

        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_handle_nocase
            ON agents (handle COLLATE NOCASE)
            """
        )

        # Older releases allowed the same key once per thread.  Preserve every
        # post during the global-index migration: the earliest post keeps the
        # original delivery key and later collisions receive a deterministic,
        # globally unique legacy key based on their immutable post UUID.
        duplicate_keys = list(
            connection.exec_driver_sql(
                """
                SELECT idempotency_key
                FROM posts
                WHERE idempotency_key IS NOT NULL
                GROUP BY idempotency_key
                HAVING COUNT(*) > 1
                ORDER BY idempotency_key
                """
            ).scalars()
        )
        used_keys = set(
            connection.exec_driver_sql(
                "SELECT idempotency_key FROM posts WHERE idempotency_key IS NOT NULL"
            ).scalars()
        )
        for duplicate_key in duplicate_keys:
            duplicate_post_ids = list(
                connection.exec_driver_sql(
                    """
                    SELECT id
                    FROM posts
                    WHERE idempotency_key = ?
                    ORDER BY created_at, id
                    """,
                    (duplicate_key,),
                ).scalars()
            )
            for post_id in duplicate_post_ids[1:]:
                base_key = f"legacy-duplicate:{post_id}"
                migrated_key = base_key
                suffix = 1
                while migrated_key in used_keys:
                    migrated_key = f"{base_key}:{suffix}"
                    suffix += 1
                connection.exec_driver_sql(
                    "UPDATE posts SET idempotency_key = ? WHERE id = ?",
                    (migrated_key, post_id),
                )
                used_keys.add(migrated_key)
        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_posts_idempotency_key
            ON posts (idempotency_key)
            """
        )
        connection.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS experiment_manifest_immutable BEFORE UPDATE OF manifest, manifest_sha256 ON experiments BEGIN SELECT RAISE(ABORT, 'experiment manifest is immutable'); END")
        connection.exec_driver_sql("CREATE TRIGGER IF NOT EXISTS scenario_immutable BEFORE UPDATE ON scenarios BEGIN SELECT RAISE(ABORT, 'scenario version is immutable'); END")
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS events_are_immutable_on_update
            BEFORE UPDATE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events are immutable');
            END
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TRIGGER IF NOT EXISTS events_are_immutable_on_delete
            BEFORE DELETE ON events
            BEGIN
                SELECT RAISE(ABORT, 'events are immutable');
            END
            """
        )


@contextmanager
def session_scope(
    factory: sessionmaker[Session] | None = None,
) -> Iterator[Session]:
    """Provide a transaction-scoped session for jobs and scripts."""

    db = (factory or SessionLocal)()
    try:
        with db.begin():
            yield db
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency: commit successful requests, roll back failures."""

    db = SessionLocal()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


# Conventional FastAPI dependency alias.
get_db = get_session


__all__ = [
    "SessionLocal",
    "default_database_url",
    "engine",
    "get_db",
    "get_session",
    "init_db",
    "make_engine",
    "make_session_factory",
    "session_scope",
]
