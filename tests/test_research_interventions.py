from __future__ import annotations

import hashlib
from copy import deepcopy

import httpx
import pytest
from sqlalchemy import func, select

from swarmboard import interventions, sessions
from swarmboard.app import create_app
from swarmboard.models import Agent, Event, Memory, Post, Run, Stimulus, Thread, Turn
from swarmboard.repository import InvalidStateError, Repository
from .test_engine_acceptance import ScriptedGateway, make_database


@pytest.fixture
def store():
    engine, factory = make_database()
    try:
        yield factory
    finally:
        engine.dispose()


def setup(repo, *, session_type="collaboration"):
    first = repo.create_agent(handle="target", persona="Original target persona", model="qwen/test")
    second = repo.create_agent(handle="peer", persona="Other participant", model="deepseek/test")
    run = sessions.create_session(repo, agents=[first, second], title="Interventions", body="Opening",
                                  continuous=False, author_handle="researcher", session_type=session_type)
    thread = repo.list_threads(run_id=run.id)[0]
    return run, thread, first, second


def event_rows(session):
    return deepcopy(list(session.execute(Event.__table__.select().order_by(Event.id)).mappings()))


@pytest.mark.parametrize("session_type", ["collaboration", "research"])
def test_instruction_creation_and_revocation_are_attributed_and_immutable(store, session_type):
    with store.begin() as session:
        repo = Repository(session)
        run, _, first, second = setup(repo, session_type=session_type)
        body = " \r\nKeep this private instruction exactly.\t "
        instruction = interventions.create_instruction(repo, run_id=run.id, agent_id=first.id,
            body=body, author="alice", idempotency_key="create-instruction")
        assert instruction["author"] == "alice"
        assert instruction["body"] == body
        assert instruction["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
        assert interventions.active_instructions(repo, run.id, second.id) == []
        assert interventions.active_instructions(repo, run.id, first.id)[0]["id"] == instruction["id"]
        assert interventions.active_instructions(repo, run.id, first.id, instruction["id"] - 1) == []
        before = event_rows(session)
        assert interventions.create_instruction(repo, run_id=run.id, agent_id=first.id,
            body=body, author="alice", idempotency_key="create-instruction") == instruction
        assert event_rows(session) == before
        revoked = interventions.revoke_instruction(repo, instruction_id=instruction["id"], author="bob", idempotency_key="revoke")
        assert revoked["author"] == "bob" and revoked["revoked"]
        assert event_rows(session)[:len(before)] == before
        assert interventions.active_instructions(repo, run.id, first.id) == []
        assert interventions.active_instructions(repo, run.id, first.id, instruction["id"])[0]["body"] == body
        historical = interventions.list_interventions(repo, run.id)["instructions"][0]
        assert historical["body"] == body and historical["revoked"]
        assert historical["revocation"]["author"] == "bob"


def test_memory_versions_and_deactivation_preserve_every_original_row(store):
    with store.begin() as session:
        repo = Repository(session)
        run, _, first, second = setup(repo)
        original = interventions.seed_memory(repo, run_id=run.id, agent_id=first.id,
            body="  First source text  ", tags=["source", "source"], author="alice", idempotency_key="memory-1")
        assert original["version"] == 1 and original["body"] == "First source text"
        assert original["body_sha256"] == hashlib.sha256(original["body"].encode()).hexdigest()
        saved = session.get_one(Memory, original["id"])
        original_fields = {column.key: deepcopy(getattr(saved, column.key)) for column in Memory.__table__.columns}
        replacement = interventions.seed_memory(repo, run_id=run.id, agent_id=first.id,
            body="A revised source text", author="bob", replaces_memory_id=original["id"], idempotency_key="memory-2")
        assert replacement["id"] != original["id"] and replacement["version"] == 2
        assert original["id"] in interventions.inactive_memory_ids(repo, run.id)
        assert replacement["id"] not in interventions.inactive_memory_ids(repo, run.id)
        assert original["id"] not in interventions.inactive_memory_ids(repo, run.id, original["event_id"])
        assert replacement["id"] in interventions.inactive_memory_ids(repo, run.id, original["event_id"])
        deactivated = interventions.deactivate_memory(repo, memory_id=replacement["id"], author="alice", idempotency_key="off")
        assert not deactivated["active"]
        assert replacement["id"] in interventions.inactive_memory_ids(repo, run.id)
        assert {column.key: getattr(saved, column.key) for column in Memory.__table__.columns} == original_fields
        assert session.get_one(Memory, replacement["id"]).active is True
        projected = interventions.list_interventions(repo, run.id)["memories"]
        assert [item["version"] for item in projected] == [1, 2]
        assert all(not item["active"] for item in projected)
        inactive = interventions.seed_memory(repo, run_id=run.id, agent_id=second.id,
            body="Seed disabled from the start", active=False, author="alice", idempotency_key="memory-inactive")
        assert inactive["id"] in interventions.inactive_memory_ids(repo, run.id)


@pytest.mark.parametrize("session_type", ["collaboration", "research"])
def test_configuration_overrides_are_session_scoped_versioned_and_repair_unavailability(store, session_type):
    with store.begin() as session:
        repo = Repository(session)
        run, _, first, second = setup(repo, session_type=session_type)
        run.config = {**run.config, "unavailable_agent_ids": [first.id, second.id]}
        original = {column.key: deepcopy(getattr(first, column.key)) for column in Agent.__table__.columns}
        changed = interventions.change_configuration(repo, run_id=run.id, agent_id=first.id,
            persona="Updated session persona", model="qwen/alternate", author="alice", idempotency_key="config-1")
        assert changed["author"] == "alice" and changed["session_type"] == session_type
        assert changed["before"]["model"] == "qwen/test"
        assert changed["after"]["model"] == "qwen/alternate"
        assert changed["persona_sha256"] == hashlib.sha256(b"Updated session persona").hexdigest()
        assert changed["persona_version"] == 2
        assert run.config["agent_overrides"][first.id]["model"] == "qwen/alternate"
        assert run.config["unavailable_agent_ids"] == [second.id]
        assert {column.key: getattr(first, column.key) for column in Agent.__table__.columns} == original
        second_edit = interventions.change_configuration(repo, run_id=run.id, agent_id=first.id,
            model="qwen/another", author="alice", idempotency_key="config-2")
        assert second_edit["persona_version"] == 2
        assert second_edit["after"]["persona"] == "Updated session persona"
        assert second_edit["before"]["model"] == "qwen/alternate"


def test_ada_persona_override_preserves_authored_instructions_and_global_registration(store):
    with store.begin() as session:
        repo = Repository(session)
        captured = {"version": 1, "source": "test fixture", "instructions": "Authored instructions\r\n",
                    "memory": "Original authored memory\r\n"}
        ada = repo.create_agent(handle="ada", persona="Generic unused field", provider="codex", model="gpt-6-astra",
                                settings={"persona_harness": captured})
        run = sessions.create_session(repo, agents=[ada], continuous=False)
        result = interventions.change_configuration(repo, run_id=run.id, agent_id=ada.id,
            persona="Session-specific authored memory\r\n", author="alice", idempotency_key="ada-persona")
        harness = result["after"]["settings"]["persona_harness"]
        assert harness["instructions"] == captured["instructions"]
        assert harness["memory"] == "Session-specific authored memory\r\n"
        assert harness["source"] == f"session-override:{run.id}"
        assert run.config["agent_overrides"][ada.id]["settings"]["persona_harness"]["source"] == f"session-override:{run.id}"
        assert ada.settings["persona_harness"] == captured
        assert ada.persona == "Generic unused field"


@pytest.mark.asyncio
async def test_session_settings_roundtrip_preserves_registered_and_overridden_persona_sources(tmp_path, monkeypatch):
    monkeypatch.setattr("swarmboard.config.load_dotenv", lambda **kwargs: None)
    for name in ("SWARMBOARD_AUTH_USERS", "SWARMBOARD_REQUIRE_AUTH", "RENDER"):
        monkeypatch.delenv(name, raising=False)
    app = create_app(database_url=f"sqlite:///{tmp_path / 'persona-source.db'}", gateway=ScriptedGateway())
    original_source = "/Users/fixture/private/persona"
    captured = {"version": 1, "source": original_source,
                "instructions": "Exact instructions\r\n", "memory": "Exact memory\r\n"}
    async with app.router.lifespan_context(app):
        with app.state.session_factory.begin() as stored:
            repo = Repository(stored)
            ada = repo.create_agent(handle="ada", provider="codex", model="gpt-6-astra",
                                   persona="Captured persona", settings={"persona_harness": captured})
            run = sessions.create_session(repo, agents=[ada], continuous=False)
            agent_id, run_id = ada.id, run.id
        path = f"/api/runs/{run_id}/agents/{agent_id}/configuration"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            state = (await client.get("/api/state")).json()
            visible = next(agent for agent in state["agents"] if agent["id"] == agent_id)
            assert visible["settings"]["persona_harness"]["source"] == "server-managed"
            request_settings = deepcopy(visible["settings"])
            request_settings["sampling"] = {"reasoning_effort": "high"}
            payload = {"settings": request_settings, "idempotency_key": "source-roundtrip"}
            first = await client.patch(path, json=payload)
            assert first.status_code == 200, first.text
            assert (await client.patch(path, json=payload)).json() == first.json()
            with app.state.session_factory() as stored:
                run = stored.get_one(Run, run_id)
                persisted = run.config["agent_overrides"][agent_id]["settings"]["persona_harness"]
                assert persisted == captured
                assert stored.get_one(Agent, agent_id).settings["persona_harness"] == captured
            changed = await client.patch(path, json={"persona": "Revised session memory\r\n", "idempotency_key": "source-new-persona"})
            assert changed.status_code == 200, changed.text
            ledger = (await client.get(f"/api/runs/{run_id}/interventions")).json()
            request_settings = deepcopy(ledger["config_changes"][-1]["after"]["settings"])
            assert request_settings["persona_harness"]["source"] == f"session-override:{run_id}"
            # An opaque source marker must resolve against this session's
            # effective override, including safe session provenance labels.
            request_settings["persona_harness"]["source"] = "server-managed"
            request_settings["sampling"] = {"reasoning_effort": "medium"}
            last = await client.patch(path, json={"settings": request_settings, "idempotency_key": "source-session-roundtrip"})
            assert last.status_code == 200, last.text
            assert last.json()["persona_sha256"] == changed.json()["persona_sha256"]
            with app.state.session_factory() as stored:
                run = stored.get_one(Run, run_id)
                persisted = run.config["agent_overrides"][agent_id]["settings"]["persona_harness"]
                assert persisted["source"] == f"session-override:{run_id}"
                assert persisted["instructions"] == captured["instructions"]
                assert persisted["memory"] == "Revised session memory\r\n"
                assert stored.get_one(Agent, agent_id).settings["persona_harness"] == captured


def test_configuration_version_continues_registered_persona_history(store):
    with store.begin() as session:
        repo = Repository(session)
        run, _, first, _ = setup(repo)
        first.persona_version = 7
        result = interventions.change_configuration(repo, run_id=run.id, agent_id=first.id,
            persona="New session revision", author="alice", idempotency_key="next-version")
        assert result["before"]["persona_version"] == 7
        assert result["after"]["persona_version"] == 8
        assert first.persona_version == 7


def test_intervention_projection_uses_one_historical_event_cutoff(store):
    with store.begin() as session:
        repo = Repository(session)
        run, thread, first, _ = setup(repo, session_type="research")
        instruction = interventions.create_instruction(repo, run_id=run.id, agent_id=first.id,
            body="Original instruction", author="alice", idempotency_key="history-instruction")
        memory = interventions.seed_memory(repo, run_id=run.id, agent_id=first.id,
            body="Original memory", author="alice", idempotency_key="history-memory")
        change = interventions.change_configuration(repo, run_id=run.id, agent_id=first.id,
            model="qwen/historical", author="alice", idempotency_key="history-config")
        post = interventions.research_post(repo, thread_id=thread.id, as_handle=first.handle,
            body="Original displayed post", author="alice", idempotency_key="history-post")
        cutoff = session.scalar(select(func.max(Event.id)))
        before = interventions.list_interventions(repo, run.id, at_event_id=cutoff)
        assert before["instructions"][0]["id"] == instruction["id"]
        assert before["memories"][0]["id"] == memory["id"]
        assert before["config_changes"][0]["id"] == change["id"]
        assert before["impersonations"][0]["post_id"] == post["post_id"]
        interventions.revoke_instruction(repo, instruction_id=instruction["id"], author="bob", idempotency_key="history-revoke")
        interventions.seed_memory(repo, run_id=run.id, agent_id=first.id, body="Replacement memory",
            replaces_memory_id=memory["id"], author="bob", idempotency_key="history-replace")
        interventions.change_configuration(repo, run_id=run.id, agent_id=first.id,
            model="qwen/current", author="bob", idempotency_key="history-config-new")
        interventions.research_post(repo, thread_id=thread.id, system_author=True,
            body="New notice", author="bob", idempotency_key="history-post-new")
        assert interventions.list_interventions(repo, run.id, at_event_id=cutoff) == before
        assert before["memories"][0]["active"] is True
        current = interventions.list_interventions(repo, run.id)
        assert current["instructions"][0]["revoked"] is True
        assert current["memories"][0]["active"] is False
        assert all(len(current[key]) == 2 for key in ("memories", "config_changes", "impersonations"))


def test_research_posts_record_true_human_author_and_imitate_reply_routing(store):
    with store.begin() as session:
        repo = Repository(session)
        run, thread, first, second = setup(repo, session_type="research")
        parent = repo.create_agent_post(thread.id, second.id, "A peer contribution")
        previous_cooldowns = {agent.id: agent.last_spoke_at for agent in (first, second)}
        previous_usage = (run.rounds_used, run.tokens_used, run.model_calls, run.posts_used)
        repo.set_thread_status(thread.id, "dormant")
        posted = interventions.research_post(repo, thread_id=thread.id, as_handle=first.handle,
            parent_post_id=parent.post.id, body="Explain your choice.", author="alice", idempotency_key="impersonated")
        post = repo.get_post(posted["post_id"])
        assert post.author_type == "human" and post.author_handle == "alice" and post.author_agent_id is None
        assert post.metadata_json["author_human"] == "alice"
        assert post.metadata_json["displayed_as_agent"] == first.handle
        assert post.metadata_json["displayed_as_agent_id"] == first.id and post.metadata_json["is_impersonation"]
        assert thread.status == "active"
        assert (run.rounds_used, run.tokens_used, run.model_calls) == previous_usage[:3]
        assert run.posts_used == previous_usage[3] + 1
        assert {agent.id: agent.last_spoke_at for agent in (first, second)} == previous_cooldowns
        stimuli = [repo.get_stimulus(stimulus_id) for stimulus_id in posted["stimulus_ids"]]
        assert len(stimuli) == 1 and stimuli[0].target_agent_id == second.id
        assert stimuli[0].payload["reason"] == "reply_to_author"
        assert "author_human" not in stimuli[0].payload
        domain_event = session.scalar(select(Event).where(Event.post_id == post.id, Event.event_type == "post.created"))
        assert domain_event.actor_type == "human" and domain_event.actor_id == "alice"
        assert domain_event.payload["displayed_as_agent_id"] == first.id
        before = event_rows(session)
        assert interventions.research_post(repo, thread_id=thread.id, as_handle=first.handle,
            parent_post_id=parent.post.id, body="Explain your choice.", author="alice", idempotency_key="impersonated") == posted
        assert event_rows(session) == before
        notice = interventions.research_post(repo, thread_id=thread.id, body="Board closes soon", system_author=True,
                                             author="alice", idempotency_key="notice")
        assert notice["post"]["metadata"]["is_system_notice"]
        assert notice["post"]["metadata"]["displayed_author_type"] == "system"
        assert not notice["post"]["metadata"]["is_impersonation"]


@pytest.mark.parametrize("system_author", [False, True])
def test_collaboration_rejects_disguised_posts_without_domain_side_effects(store, system_author):
    with store.begin() as session:
        repo = Repository(session)
        _, thread, first, _ = setup(repo)
        before = event_rows(session)
        count = session.scalar(select(func.count(Post.id)))
        with pytest.raises(InvalidStateError, match="research session"):
            interventions.research_post(repo, thread_id=thread.id, body="Disguised content", author="alice",
                as_handle=None if system_author else first.handle, system_author=system_author, idempotency_key="disguised")
        assert event_rows(session) == before
        assert session.scalar(select(func.count(Post.id))) == count


@pytest.mark.parametrize("operation", ["post", "instruction", "revoke", "configuration", "memory", "deactivate"])
def test_terminal_sessions_reject_new_interventions_while_reads_remain_available(store, operation):
    with store.begin() as session:
        repo = Repository(session)
        run, thread, first, _ = setup(repo, session_type="research")
        instruction = interventions.create_instruction(repo, run_id=run.id, agent_id=first.id, body="Before stop",
            author="alice", idempotency_key="before-instruction")
        memory = interventions.seed_memory(repo, run_id=run.id, agent_id=first.id, body="Before stop",
            author="alice", idempotency_key="before-memory")
        repo.control_run(run.id, "stop")
        before = event_rows(session)
        with pytest.raises(InvalidStateError, match="terminal"):
            if operation == "post":
                interventions.research_post(repo, thread_id=thread.id, body="After stop", as_handle=first.handle, author="alice", idempotency_key="after")
            elif operation == "instruction":
                interventions.create_instruction(repo, run_id=run.id, agent_id=first.id, body="After stop", author="alice", idempotency_key="after")
            elif operation == "revoke":
                interventions.revoke_instruction(repo, instruction_id=instruction["id"], author="alice", idempotency_key="after")
            elif operation == "configuration":
                interventions.change_configuration(repo, run_id=run.id, agent_id=first.id, model="qwen/new", author="alice", idempotency_key="after")
            elif operation == "memory":
                interventions.seed_memory(repo, run_id=run.id, agent_id=first.id, body="After stop", author="alice", idempotency_key="after")
            else:
                interventions.deactivate_memory(repo, memory_id=memory["id"], author="alice", idempotency_key="after")
        assert event_rows(session) == before
        assert interventions.list_interventions(repo, run.id)["instructions"][0]["id"] == instruction["id"]


@pytest.mark.asyncio
async def test_authenticated_intervention_routes_attribute_operator_and_scope_request_keys(tmp_path, monkeypatch):
    monkeypatch.setattr("swarmboard.config.load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", '{"alice":"alice-password","bob":"bob-password"}')
    app = create_app(database_url=f"sqlite:///{tmp_path / 'intervention-auth.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        with app.state.session_factory.begin() as session:
            run, _, first, _ = setup(Repository(session))
            path = f"/api/runs/{run.id}/agents/{first.id}/instructions"
            run_id = run.id
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://board.test") as client:
            payload = {"body": "Keep the attribution", "idempotency_key": "same-client-key"}
            anonymous = await client.post(path, json=payload)
            assert anonymous.status_code == 401
            first = await client.post(path, json=payload, auth=("alice", "alice-password"))
            assert first.status_code == 201, first.text
            assert first.json()["author"] == "alice"
            retried = await client.post(path, json=payload, auth=("alice", "alice-password"))
            assert retried.json() == first.json()
            other = await client.post(path, json=payload, auth=("bob", "bob-password"))
            assert other.status_code == 201 and other.json()["author"] == "bob"
            assert other.json()["id"] != first.json()["id"]
            conflict = await client.post(path, json={**payload, "body": "different content"}, auth=("alice", "alice-password"))
            assert conflict.status_code == 409
            projected = await client.get(f"/api/runs/{run_id}/interventions", auth=("alice", "alice-password"))
            assert {instruction["author"] for instruction in projected.json()["instructions"]} == {"alice", "bob"}
