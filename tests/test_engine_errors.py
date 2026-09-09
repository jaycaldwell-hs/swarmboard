"""Filesystem diagnostics never become operator-visible turn traces."""
from __future__ import annotations

import errno
import json

import pytest
from sqlalchemy import select

from swarmboard.engine import SwarmEngine
from swarmboard.models import Event, Turn
from tests.test_engine_acceptance import ScriptedGateway, make_database, seed_conversation


@pytest.mark.parametrize("error", [
    OSError("private server diagnostic"),
    FileNotFoundError(errno.ENOENT, "missing file", "/private/server/credential-file"),
    PermissionError(errno.EACCES, "access denied", "/private/server/credential-file"),
    IsADirectoryError(errno.EISDIR, "wrong file type", "/private/server/credential-file"),
])
def test_filesystem_errors_preserve_only_the_exception_category(error):
    assert SwarmEngine._safe_error(error) == f"{type(error).__name__}: server operation failed"


def test_ordinary_errors_keep_the_existing_message_and_length_limit():
    assert SwarmEngine._safe_error(ValueError("invalid action")) == "ValueError: invalid action"
    assert SwarmEngine._safe_error(RuntimeError("x" * 2100)) == "RuntimeError: " + "x" * 2000


@pytest.mark.asyncio
async def test_filesystem_failure_in_provider_does_not_persist_server_paths(tmp_path):
    database, factory = make_database(f"sqlite:///{tmp_path / 'failure.db'}")
    run_id, _, _, _ = seed_conversation(factory)
    private_path = "/private/server/planted-credential-file"
    engine = SwarmEngine(factory, gateway=ScriptedGateway(FileNotFoundError(errno.ENOENT, "missing", private_path)))
    try:
        result = await engine.step(run_id)
        with factory() as session:
            turn = session.scalar(select(Turn).where(Turn.run_id == run_id))
            assert turn is not None and turn.error == "FileNotFoundError: server operation failed"
            events = list(session.scalars(select(Event).where(Event.run_id == run_id)))
            assert private_path not in json.dumps([event.payload for event in events])
            assert private_path not in str(result)
    finally:
        await engine.shutdown()
        database.dispose()
