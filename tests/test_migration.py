from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from swarmboard import migration
from swarmboard.hosted import storage_lock
from swarmboard.migration import import_database


def snapshot(path: Path, body: str) -> None:
    with closing(sqlite3.connect(path)) as connection:
        for name in ("agents", "runs", "threads", "posts", "events", "turns"):
            connection.execute(f"CREATE TABLE {name} (body TEXT)")
        connection.execute("INSERT INTO events VALUES (?)", (body,))
        connection.commit()


def events(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as connection:
        return [row[0] for row in connection.execute("SELECT body FROM events")]


@pytest.fixture(autouse=True)
def isolated_import_env(monkeypatch):
    monkeypatch.delenv("SWARMBOARD_IMPORT_DB_PATH", raising=False)


def test_import_backs_up_target_and_retains_source_and_new_history(tmp_path, monkeypatch):
    source, target = tmp_path / "incoming.db", tmp_path / "swarmboard.db"
    snapshot(source, "local history")
    snapshot(target, "hosted history before migration")
    original_source = source.read_bytes()
    monkeypatch.setenv("SWARMBOARD_IMPORT_DB_PATH", str(source))
    with storage_lock(target):
        assert import_database(target) == "imported"
    assert events(target) == ["local history"]
    assert source.read_bytes() == original_source
    receipts = list((tmp_path / ".imports").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["state"] == "completed"
    assert receipt["source_sha256"] == hashlib.sha256(original_source).hexdigest()
    assert events(tmp_path / receipt["backup_file"]) == ["hosted history before migration"]
    assert target.stat().st_mode & 0o777 == 0o600
    assert receipts[0].stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("INSERT INTO events VALUES ('new hosted history')")
        connection.commit()
    with storage_lock(target):
        assert import_database(target) == "already_imported"
    assert events(target) == ["local history", "new hosted history"]
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_import_into_empty_mount_and_no_configuration(tmp_path):
    source, target = tmp_path / "incoming.db", tmp_path / "swarmboard.db"
    assert import_database(target) is None
    assert not target.exists()
    snapshot(source, "local history")
    assert import_database(target, source) == "imported"
    assert events(target) == ["local history"]
    assert not (tmp_path / "backups").exists()


@pytest.mark.parametrize("invalid", ["corrupt", "wrong_schema", "same_target", "hardlink_target", "outside_mount", "wal", "missing"])
def test_invalid_import_preserves_live_database(tmp_path, invalid):
    directory = tmp_path / "data"
    directory.mkdir()
    source, target = directory / "incoming.db", directory / "swarmboard.db"
    snapshot(target, "keep existing evidence")
    original_target = target.read_bytes()
    if invalid == "corrupt":
        source.write_bytes(b"not a sqlite database")
    elif invalid == "wrong_schema":
        with closing(sqlite3.connect(source)) as connection:
            connection.execute("CREATE TABLE unrelated (id INTEGER)")
    elif invalid == "same_target":
        source = target
    elif invalid == "hardlink_target":
        source.hardlink_to(target)
    elif invalid == "outside_mount":
        source = tmp_path / "elsewhere.db"
        snapshot(source, "outside")
    elif invalid == "wal":
        snapshot(source, "incomplete file copy")
        Path(str(source) + "-wal").write_bytes(b"uncheckpointed data")
    with pytest.raises((ValueError, sqlite3.DatabaseError)):
        import_database(target, source)
    assert target.read_bytes() == original_target
    assert events(target) == ["keep existing evidence"]
    assert not list(directory.glob(".swarmboard.db.import-*.db"))


@pytest.mark.parametrize("after_replace", [False, True])
def test_import_recovers_crashes_before_and_after_atomic_replace(tmp_path, monkeypatch, after_replace):
    source, target = tmp_path / "incoming.db", tmp_path / "swarmboard.db"
    snapshot(source, "imported evidence")
    snapshot(target, "previous evidence")
    original_write = migration._write_receipt
    original_replace = migration.os.replace

    def fail_completion(path, receipt):
        if receipt["state"] == "completed":
            raise OSError("simulated crash after replace")
        original_write(path, receipt)

    def fail_replace(source_path, target_path):
        if Path(target_path) == target:
            raise OSError("simulated crash before replace")
        original_replace(source_path, target_path)

    if after_replace:
        monkeypatch.setattr(migration, "_write_receipt", fail_completion)
    else:
        monkeypatch.setattr(migration.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated crash"):
        import_database(target, source)
    receipt_path = next((tmp_path / ".imports").glob("*.json"))
    assert json.loads(receipt_path.read_text())["state"] == "prepared"
    assert events(target) == ["imported evidence" if after_replace else "previous evidence"]
    monkeypatch.setattr(migration, "_write_receipt", original_write)
    monkeypatch.setattr(migration.os, "replace", original_replace)
    # Recovery needs no env var or source read once a durable stage exists.
    assert import_database(target) == "recovered"
    assert events(target) == ["imported evidence"]
    assert json.loads(receipt_path.read_text())["state"] == "completed"
    assert import_database(target, source) == "already_imported"
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_pending_import_refuses_external_target_changes(tmp_path, monkeypatch):
    source, target = tmp_path / "incoming.db", tmp_path / "swarmboard.db"
    snapshot(source, "incoming")
    snapshot(target, "initial")
    original_finish = migration._finish_import
    monkeypatch.setattr(migration, "_finish_import", lambda *args: (_ for _ in ()).throw(OSError("stop after prepare")))
    with pytest.raises(OSError):
        import_database(target, source)
    monkeypatch.setattr(migration, "_finish_import", original_finish)
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("INSERT INTO events VALUES ('external new evidence')")
        connection.commit()
    with pytest.raises(RuntimeError, match="state changed"):
        import_database(target)
    assert events(target) == ["initial", "external new evidence"]


def test_existing_target_wal_is_backed_up_before_import(tmp_path):
    source, target = tmp_path / "incoming.db", tmp_path / "swarmboard.db"
    snapshot(source, "incoming history")
    snapshot(target, "previous history")
    # A stopped/crashed previous service can leave committed data in its WAL.
    subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute("INSERT INTO events VALUES ('previous WAL history')")
connection.commit()
os._exit(0)
""", str(target)], check=True)
    assert Path(str(target) + "-wal").stat().st_size > 0
    assert import_database(target, source) == "imported"
    backup = next((tmp_path / "backups").glob("*.db"))
    assert events(backup) == ["previous history", "previous WAL history"]
    assert events(target) == ["incoming history"]
    assert not Path(str(target) + "-wal").exists()


def test_completed_receipt_never_silently_recreates_a_missing_database(tmp_path):
    source, target = tmp_path / "incoming.db", tmp_path / "swarmboard.db"
    snapshot(source, "history")
    import_database(target, source)
    target.unlink()
    with pytest.raises(RuntimeError, match="Previously imported database is missing"):
        import_database(target, source)
    assert not target.exists()
