"""One-time, journaled import of an uploaded SQLite snapshot at hosted startup.

The caller must hold ``hosted.storage_lock`` and must not have opened the board
database yet. Inputs are standalone SQLite backups on the same persistent mount,
never a live database or a main database file copied without its WAL.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Any, Literal


_CORE_TABLES = {"agents", "runs", "threads", "posts", "events", "turns"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_receipt(path: Path, receipt: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
        _sync_directory(path.parent)
    finally:
        staged.unlink(missing_ok=True)


def _read_receipt(path: Path, database: Path) -> dict[str, Any]:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(receipt, dict) or receipt.get("version") != 1
                or receipt.get("state") not in {"prepared", "completed"}
                or receipt.get("target") != database.name):
            raise ValueError
        for key in ("source_sha256", "staged_sha256"):
            if not isinstance(receipt.get(key), str) or not _SHA256.fullmatch(receipt[key]):
                raise ValueError
        previous = receipt.get("previous_sha256")
        if previous is not None and (not isinstance(previous, str) or not _SHA256.fullmatch(previous)):
            raise ValueError
        name = receipt.get("staged_file")
        if (not isinstance(name, str) or Path(name).name != name
                or not name.startswith(f".{database.name}.import-") or not name.endswith(".db")
                or path.name != f"{database.name}.{receipt['source_sha256']}.json"):
            raise ValueError
        return receipt
    except (OSError, ValueError, TypeError, UnicodeError):
        raise RuntimeError("Database import receipt is invalid; preserve it and inspect the migration state") from None


def _finish_import(database: Path, path: Path, receipt: dict[str, Any]) -> None:
    """Resume a prepared import without repeating one that was already installed."""
    staged = database.parent / receipt["staged_file"]
    current_digest = _digest(database) if database.is_file() else None
    if staged.exists():
        if _digest(staged) != receipt["staged_sha256"] or current_digest != receipt["previous_sha256"]:
            raise RuntimeError("Database import state changed; preserve the source, backup, and receipt for inspection")
        # WAL/journal state cannot be attached to a replacement main file. The
        # target was checkpointed before preparing; any new sidecar is external
        # activity and must block recovery instead of silently dropping writes.
        if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise RuntimeError("Database import found active SQLite sidecars; stop all database users before retrying")
        os.replace(staged, database)
        _sync_directory(database.parent)
    elif current_digest != receipt["staged_sha256"]:
        raise RuntimeError("Interrupted database import cannot be verified; preserve the source, backup, and receipt")
    receipt = {**receipt, "state": "completed"}
    _write_receipt(path, receipt)


def _stage_snapshot(source: Path, database: Path) -> Path:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{database.name}.import-", suffix=".db", dir=database.parent)
    os.close(descriptor)
    staged = Path(temporary)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as incoming:
            if incoming.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("Import snapshot failed SQLite integrity validation")
            tables = {row[0] for row in incoming.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not _CORE_TABLES.issubset(tables):
                raise ValueError("Import snapshot is missing required Swarmboard tables")
            with closing(sqlite3.connect(staged)) as destination:
                incoming.backup(destination)
                destination.execute("PRAGMA journal_mode=DELETE")
                if destination.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("Staged import failed SQLite integrity validation")
        _sync_file(staged)
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def import_database(database: Path, source: Path | None = None) -> Literal["imported", "already_imported", "recovered"] | None:
    """Import ``SWARMBOARD_IMPORT_DB_PATH`` exactly once per source SHA-256.

    Call under the storage lock, before backup/schema initialization/recovery.
    The upload must be within ``database.parent``, owned/readable by the service,
    and remain immutable through import. Source bytes and existing target backup
    are retained. Prepared receipts recover even after the env var is removed.
    """
    database = database.expanduser().resolve()
    configured = source or os.getenv("SWARMBOARD_IMPORT_DB_PATH")
    incoming = Path(configured).expanduser().resolve() if configured else None
    if incoming is not None:
        if incoming == database or (incoming.exists() and database.exists() and incoming.samefile(database)):
            raise ValueError("Import source must differ from the live database")
        if not incoming.is_relative_to(database.parent):
            raise ValueError("Import source must be uploaded inside the persistent database directory")
    receipts = database.parent / ".imports"
    pending = []
    if receipts.is_dir():
        for path in receipts.iterdir():
            if not path.name.startswith(database.name + ".") or not path.name.endswith(".json"):
                continue
            receipt = _read_receipt(path, database)
            if receipt["state"] == "prepared":
                pending.append((path, receipt))
    if len(pending) > 1:
        raise RuntimeError("Multiple incomplete database imports require inspection before startup")
    if pending:
        path, receipt = pending[0]
        if incoming is not None and (not incoming.is_file() or _digest(incoming) != receipt["source_sha256"]):
            raise RuntimeError("An incomplete database import exists for a different source; preserve its receipt")
        _finish_import(database, path, receipt)
        return "recovered"
    if incoming is None:
        return None
    if not incoming.is_file():
        raise ValueError("Import snapshot is missing; upload it before enabling SWARMBOARD_IMPORT_DB_PATH")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(incoming) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("Import requires a standalone SQLite backup without WAL or journal sidecars")
    source_digest = _digest(incoming)
    path = receipts / f"{database.name}.{source_digest}.json"
    if path.exists():
        _read_receipt(path, database)
        if not database.is_file():
            raise RuntimeError("Previously imported database is missing; restore it before startup")
        return "already_imported"

    database.parent.mkdir(parents=True, exist_ok=True)
    receipts.mkdir(mode=0o700, exist_ok=True)
    _sync_directory(database.parent)
    staged = _stage_snapshot(incoming, database)
    try:
        if _digest(incoming) != source_digest:
            raise RuntimeError("Import source changed while it was being copied; upload a stable SQLite backup")
        # Deferred import avoids a module cycle with the hosted entrypoint.
        from .hosted import backup_database
        backup = backup_database(database)
        if backup is not None:
            _sync_file(backup)
            _sync_directory(backup.parent)
            # There are no server connections yet. Fold the old WAL into the
            # old main file and remove its sidecars before replacing that file.
            with closing(sqlite3.connect(database)) as current:
                checkpoint = current.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint[0] != 0 or current.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise RuntimeError("Cannot import while another connection holds the database")
        receipt = {
            "version": 1, "state": "prepared", "target": database.name,
            "source_sha256": source_digest, "staged_file": staged.name,
            "staged_sha256": _digest(staged),
            "previous_sha256": _digest(database) if database.is_file() else None,
            "backup_file": str(backup.relative_to(database.parent)) if backup else None,
        }
        _sync_directory(database.parent)
        _write_receipt(path, receipt)
        _finish_import(database, path, receipt)
        return "imported"
    finally:
        # Once prepared, preserve staging for deterministic crash recovery.
        if not path.exists():
            staged.unlink(missing_ok=True)
