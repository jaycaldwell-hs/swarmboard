from __future__ import annotations

from contextlib import closing
import base64
import gzip
import hashlib
import json
import lzma
from pathlib import Path
import sqlite3
import subprocess
import sys
import zlib

import pytest

from swarmboard import migration
from swarmboard.hosted import storage_lock
from swarmboard.migration import import_database, materialize_import_bundle


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
    monkeypatch.delenv("SWARMBOARD_IMPORT_BUNDLE_FILE", raising=False)


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


def bundle_files(directory: Path, raw: bytes, compression: str | None = None) -> Path:
    directory.mkdir()
    compressed = lzma.compress(raw, preset=6) if compression == "xz" else gzip.compress(raw, mtime=0)
    encoded = base64.b64encode(compressed).decode()
    # Split in the middle of a base64 quantum to exercise concatenation.
    chunks = [encoded[:17], encoded[17:]]
    names = [f"swarmboard-import-{index:03d}.b64" for index in range(len(chunks))]
    for name, chunk in zip(names, chunks):
        (directory / name).write_text(chunk)
    manifest = directory / "swarmboard-import.json"
    body = {"version": 1, "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw), "parts": names}
    if compression is not None:
        body["compression"] = compression
    manifest.write_text(json.dumps(body))
    return manifest


@pytest.mark.parametrize("compression", [None, "gzip", "xz"])
def test_private_bundle_preserves_snapshot_bytes_and_reuses_verified_file(tmp_path, monkeypatch, compression):
    original = tmp_path / "original.db"
    snapshot(original, "complete private evidence café\r\n")
    original_bytes = original.read_bytes()
    manifest = bundle_files(tmp_path / "secrets", original_bytes, compression)
    database = tmp_path / "data" / "swarmboard.db"
    monkeypatch.setenv("SWARMBOARD_IMPORT_BUNDLE_FILE", str(manifest))
    with storage_lock(database):
        incoming = materialize_import_bundle(database)
        assert incoming is not None
        assert incoming.read_bytes() == original_bytes
        assert incoming.stat().st_mode & 0o777 == 0o600
        assert import_database(database, incoming) == "imported"
    assert events(database) == ["complete private evidence café\r\n"]
    original_mtime = incoming.stat().st_mtime_ns
    # Already verified snapshots do not require the chunks to remain mounted.
    for part in (tmp_path / "secrets").glob("*.b64"):
        part.unlink()
    with storage_lock(database):
        assert materialize_import_bundle(database) == incoming
        assert incoming.stat().st_mtime_ns == original_mtime
        assert import_database(database, incoming) == "already_imported"


@pytest.mark.parametrize("fault", [
    "invalid_json", "unsafe_path", "duplicate_part", "symlink_escape", "invalid_base64",
    "missing_part", "wrong_hash", "wrong_size", "oversized_raw", "encoded_limit", "gzip_bomb", "corrupt_gzip",
    "invalid_compression", "xz_bomb", "xz_memory", "xz_truncated", "xz_trailing",
])
def test_private_bundle_rejects_invalid_or_unbounded_input(tmp_path, monkeypatch, fault):
    secret_dir = tmp_path / "secrets"
    manifest_path = bundle_files(secret_dir, b"bounded test bytes")
    manifest = json.loads(manifest_path.read_text())
    if fault == "unsafe_path":
        manifest["parts"] = ["../outside.b64"]
    elif fault == "duplicate_part":
        manifest["parts"] *= 2
    elif fault == "wrong_hash":
        manifest["sha256"] = "0" * 64
    elif fault == "wrong_size":
        manifest["size_bytes"] += 1
    elif fault == "oversized_raw":
        manifest["size_bytes"] = 128 * 1024 * 1024 + 1
    elif fault == "encoded_limit":
        monkeypatch.setattr(migration, "_MAX_ENCODED_BUNDLE", 16)
    elif fault == "gzip_bomb":
        (secret_dir / manifest["parts"][0]).write_bytes(base64.b64encode(gzip.compress(b"x" * 1_000_000)))
        (secret_dir / manifest["parts"][1]).write_text("")
    elif fault == "corrupt_gzip":
        (secret_dir / manifest["parts"][0]).write_bytes(base64.b64encode(b"invalid gzip contents"))
        (secret_dir / manifest["parts"][1]).write_text("")
    elif fault == "invalid_compression":
        manifest["compression"] = "zip"
    elif fault.startswith("xz_"):
        manifest["compression"] = "xz"
        raw = b"x" * 1_000_000 if fault == "xz_bomb" else b"bounded test bytes"
        compressed = lzma.compress(raw, preset=0)
        if fault == "xz_memory":
            # Modify the valid LZMA2 block header to request a 4GiB dictionary,
            # without allocating a large encoder just to test the decoder cap.
            header_size = (compressed[12] + 1) * 4
            header = bytearray(compressed[12:12 + header_size])
            assert header[1:4] == b"\x00\x21\x01"
            header[4] = 40
            header[-4:] = zlib.crc32(header[:-4]).to_bytes(4, "little")
            compressed = compressed[:12] + header + compressed[12 + header_size:]
            with pytest.raises(lzma.LZMAError, match="Memory usage limit"):
                lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=64 * 1024 * 1024).decompress(compressed)
        elif fault == "xz_truncated":
            compressed = compressed[:-8]
        elif fault == "xz_trailing":
            compressed += b"unexpected extra data"
        (secret_dir / manifest["parts"][0]).write_bytes(base64.b64encode(compressed))
        (secret_dir / manifest["parts"][1]).write_text("")
    elif fault == "invalid_base64":
        (secret_dir / manifest["parts"][0]).write_text("not!base64")
    elif fault == "missing_part":
        (secret_dir / manifest["parts"][0]).unlink()
    elif fault == "symlink_escape":
        outside = tmp_path / "outside.b64"
        outside.write_text("private outside secret")
        first = secret_dir / manifest["parts"][0]
        first.unlink()
        first.symlink_to(outside)
    manifest_path.write_text("not json" if fault == "invalid_json" else json.dumps(manifest))
    monkeypatch.setenv("SWARMBOARD_IMPORT_BUNDLE_FILE", str(manifest_path))
    database = tmp_path / "data" / "swarmboard.db"
    with pytest.raises(ValueError):
        materialize_import_bundle(database)
    assert not database.exists()
    assert not list(database.parent.glob("*.db"))
    assert not list(database.parent.glob(".incoming-bundle-*"))


def test_bundle_requires_unambiguous_input_and_refuses_changed_cached_snapshot(tmp_path, monkeypatch):
    database = tmp_path / "data" / "swarmboard.db"
    assert materialize_import_bundle(database) is None
    manifest = bundle_files(tmp_path / "secrets", b"verified snapshot")
    monkeypatch.setenv("SWARMBOARD_IMPORT_BUNDLE_FILE", str(manifest))
    monkeypatch.setenv("SWARMBOARD_IMPORT_DB_PATH", str(tmp_path / "incoming.db"))
    with pytest.raises(ValueError, match="only one"):
        materialize_import_bundle(database)
    monkeypatch.delenv("SWARMBOARD_IMPORT_DB_PATH")
    cached = materialize_import_bundle(database)
    cached.write_bytes(b"different existing content")
    with pytest.raises(ValueError, match="does not match"):
        materialize_import_bundle(database)
    assert cached.read_bytes() == b"different existing content"
