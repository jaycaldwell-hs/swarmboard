from __future__ import annotations

import base64
import copy
import json
from pathlib import Path
import sqlite3

import httpx
import pytest

from swarmboard.config import Settings
from swarmboard.gateways import GatewayError
from swarmboard.hosted import backup_database, create_hosted_app, prepare_persona, storage_lock

from .test_engine_acceptance import ScriptedGateway


def test_persona_deployment_preserves_exact_bytes(tmp_path, monkeypatch):
    files = {"AGENTS.md": "\ufeffRead memory.md.\r\n  ", "memory.md": "Café\r\nA preference.\n\n"}
    monkeypatch.setenv("SWARMBOARD_PERSONA_BUNDLE_B64", base64.b64encode(json.dumps(files).encode()).decode())
    directory = tmp_path / "persona"
    prepare_persona(directory)
    for name, text in files.items():
        assert (directory / name).read_bytes() == text.encode()
        assert (directory / name).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("bundle", ["not base64!", base64.b64encode(b'{"../escape":"text"}').decode()])
def test_invalid_persona_secret_does_not_create_files(tmp_path, monkeypatch, bundle):
    monkeypatch.setenv("SWARMBOARD_PERSONA_BUNDLE_B64", bundle)
    with pytest.raises(ValueError, match="complete UTF-8"):
        prepare_persona(tmp_path / "persona")
    assert not list(tmp_path.iterdir())


def test_hosted_database_rejects_second_scheduler(tmp_path):
    path = tmp_path / "board.db"
    with storage_lock(path):
        with pytest.raises(RuntimeError, match="exactly one"):
            with storage_lock(path):
                pytest.fail("second scheduler acquired database")
    with storage_lock(path):
        pass


def test_backup_includes_wal_and_never_overwrites(tmp_path):
    path = tmp_path / "board.db"
    assert backup_database(path) is None
    with sqlite3.connect(path) as source:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("CREATE TABLE evidence (body TEXT)")
        source.execute("INSERT INTO evidence VALUES ('durable turn')")
        source.commit()
        target = backup_database(path)
        with sqlite3.connect(target) as snapshot:
            assert snapshot.execute("SELECT body FROM evidence").fetchone()[0] == "durable turn"
        with pytest.raises(ValueError, match="new file"):
            backup_database(path, target)
        assert source.execute("SELECT count(*) FROM evidence").fetchone()[0] == 1


@pytest.fixture
def hosted_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr("swarmboard.config.load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("SWARMBOARD_REQUIRE_AUTH", "1")
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", json.dumps({"researcher": "test-password"}))
    monkeypatch.setenv("SWARMBOARD_CODEX_AUTH", "api_key")
    monkeypatch.setenv("SWARMBOARD_CODEX_API_KEY", "test-api-key-no-network")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("SWARMBOARD_DB_PATH", str(tmp_path / "board.db"))
    monkeypatch.setenv("SWARMBOARD_PERSONA_DIR", str(tmp_path / "persona"))
    files = {"AGENTS.md": "\ufeffFull test instructions.\r\n  ", "memory.md": "Café\r\nAn exact preference.\n\n"}
    monkeypatch.setenv("SWARMBOARD_PERSONA_BUNDLE_B64", base64.b64encode(json.dumps(files).encode()).decode())
    return Settings.from_env(), files


@pytest.mark.asyncio
async def test_hosted_startup_registers_ada_and_preserves_edits_across_restarts(hosted_configuration, monkeypatch):
    from swarmboard.app import create_app

    settings, files = hosted_configuration
    gateway = ScriptedGateway()
    monkeypatch.setattr("swarmboard.app.create_app", lambda **kwargs: create_app(gateway=gateway, **kwargs))
    app = create_hosted_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://board.test",
                                    auth=("researcher", "test-password")) as client:
            state = (await client.get("/api/state")).json()
            ada_agents = [agent for agent in state["agents"] if agent["handle"] == "ada"]
            assert len(ada_agents) == 1
            assert len(state["agents"]) == 7
            ada = ada_agents[0]
            assert ada["provider"] == "codex"
            assert ada["model"] == "gpt-6-astra"
            snapshot = ada["settings"]["persona_harness"]
            assert snapshot["instructions"] == files["AGENTS.md"]
            assert snapshot["memory"] == files["memory.md"]
            assert snapshot["source"] == str(settings.persona_dir)
            assert "test-api-key-no-network" not in json.dumps(state)

            edited_settings = copy.deepcopy(ada["settings"])
            edited_settings["sampling"]["reasoning_effort"] = "high"
            edited_settings["persona_harness"]["memory"] += "Researcher's saved edit.\r\n"
            edited = await client.patch(f"/api/agents/{ada['id']}", json={
                "settings": edited_settings, "cooldown_seconds": 17,
                "persona": "A deliberately edited participant description.",
            })
            assert edited.status_code == 200, edited.text
            created = await client.post("/api/sessions", json={
                "agent_ids": [ada["id"]], "continuous": False,
                "title": "Persist this session", "body": "Durable opening",
                "idempotency_key": "restart-regression",
            })
            assert created.status_code == 201, created.text
            session = created.json()
            intervention = await client.post(f"/api/threads/{session['thread_id']}/posts",
                json={"body": "An intervention before restart."})
            assert intervention.status_code == 201
            saved_post_id = intervention.json()["post"]["id"]

    # A new deployment may materialize different source files. Existing captured
    # agent snapshots and researcher edits must survive until an explicit reload.
    changed_files = {name: contents + "New deployment source." for name, contents in files.items()}
    monkeypatch.setenv("SWARMBOARD_PERSONA_BUNDLE_B64", base64.b64encode(json.dumps(changed_files).encode()).decode())
    restarted = create_hosted_app(settings)
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="https://board.test",
                                    auth=("researcher", "test-password")) as client:
            state = (await client.get("/api/state")).json()
            ada_agents = [agent for agent in state["agents"] if agent["handle"] == "ada"]
            assert len(ada_agents) == 1
            persisted = ada_agents[0]
            assert persisted["id"] == ada["id"]
            assert persisted["settings"] == edited_settings
            assert persisted["cooldown_seconds"] == 17
            assert persisted["persona"] == "A deliberately edited participant description."
            exported = await client.get(f"/api/sessions/{session['run_id']}/export")
            assert exported.status_code == 200
            posts = exported.json()["posts"]
            assert [post["body"] for post in posts] == ["Durable opening", "An intervention before restart."]
            assert posts[-1]["id"] == saved_post_id
            assert all(post["author_handle"] == "researcher" for post in posts)
    assert gateway.calls == []
    for name, contents in changed_files.items():
        assert (settings.persona_dir / name).read_bytes() == contents.encode()


@pytest.mark.parametrize("missing", ["SWARMBOARD_AUTH_USERS", "SWARMBOARD_CODEX_API_KEY"])
def test_hosted_startup_rejects_missing_secrets_before_materializing_persona(hosted_configuration, monkeypatch, missing):
    settings, _ = hosted_configuration
    monkeypatch.delenv(missing)
    expected = ValueError if missing == "SWARMBOARD_AUTH_USERS" else GatewayError
    with pytest.raises(expected):
        create_hosted_app(settings)
    assert not settings.persona_dir.exists()
    assert not (settings.persona_dir.parent / "board.db").exists()
