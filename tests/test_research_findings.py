"""Findings annotate the ledger without changing conversation history."""
from __future__ import annotations

import copy
import json

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from swarmboard import findings
from swarmboard.app import create_app
from swarmboard.models import Event, Post, Run, Stimulus, Thread, Turn
from swarmboard.repository import InvalidStateError, Repository
from tests.test_engine_acceptance import ScriptedGateway
from tests.test_research_forced import force_client, prepared_session


def conversation_counts(app):
    with app.state.session_factory() as session:
        return tuple(session.scalar(select(func.count()).select_from(model)) for model in (Run, Thread, Post, Stimulus, Turn))


def finding_count(app):
    with app.state.session_factory() as session:
        return session.scalar(select(func.count(Event.id)).where(Event.event_type.in_(findings.FINDING_EVENTS)))


@pytest.mark.asyncio
@pytest.mark.parametrize("session_type,terminal", [("collaboration", False), ("research", False),
                                                    ("collaboration", True), ("research", True)])
async def test_flags_notes_and_resolution_work_for_both_types_and_terminal_runs(force_client, session_type, terminal):
    app, client, _ = force_client
    rid, tid, ids = prepared_session(app, session_type=session_type)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        post = repo.create_agent_post(tid, ids[0], "An observed contribution.").post
        turn = repo.create_turn(run_id=rid, thread_id=tid, agent_id=ids[0])
        repo.finish_turn(turn.id, state="completed", resulting_post_id=post.id)
        pid, turn_id = post.id, turn.id
        if terminal:
            repo.set_run_state(rid, "completed")
    before = conversation_counts(app)
    flag_response = await client.post(f"/api/posts/{pid}/flags", json={
        "body": "  Exact researcher observation.\r\n", "tags": ["novel-freeform", "replication"], "idempotency_key": "post-flag"})
    assert flag_response.status_code == 201, flag_response.text
    flag = flag_response.json()
    assert flag["author"] == "researcher" and flag["target_type"] == "post"
    assert flag["post_id"] == pid and flag["turn_id"] == turn_id
    assert flag["body"] == "  Exact researcher observation.\r\n" and not flag["resolved"]
    turn_response = await client.post(f"/api/turns/{turn_id}/flags", json={
        "tags": ["different observation"], "idempotency_key": "turn-flag"})
    assert turn_response.status_code == 201, turn_response.text
    assert turn_response.json()["target_type"] == "turn"
    note = await client.post(f"/api/runs/{rid}/notes", json={
        "body": "Run-level context for reproducibility.", "tags": ["setup"], "idempotency_key": "run-note"})
    assert note.status_code == 201 and note.json()["author"] == "researcher"
    with app.state.session_factory() as session:
        original = copy.deepcopy(session.get(Event, flag["id"]).payload)
    resolved = await client.post(f'/api/flags/{flag["id"]}/resolve', json={
        "body": "Explained by the supplied input.", "idempotency_key": "resolve-flag"})
    assert resolved.status_code == 201, resolved.text
    assert resolved.json()["resolved"]
    assert resolved.json()["resolution"]["id"] != flag["id"]
    assert resolved.json()["resolution"]["author"] == "researcher"
    listed = await client.get(f"/api/runs/{rid}/findings")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["flags"]) == 2 and len(listed.json()["notes"]) == 1
    assert listed.json()["flags"][0] == resolved.json()
    assert conversation_counts(app) == before and finding_count(app) == 4
    with app.state.session_factory() as session:
        assert session.get(Event, flag["id"]).payload == original
        assert all(event.actor_type == "human" and event.actor_id == "researcher" for event in session.scalars(
            select(Event).where(Event.event_type.in_(findings.FINDING_EVENTS))))


@pytest.mark.asyncio
async def test_findings_retries_are_idempotent_and_conflicting_requests_add_no_findings(force_client):
    app, client, _ = force_client
    rid, tid, _ = prepared_session(app)
    with app.state.session_factory() as session:
        pid = session.scalar(select(Post.id).where(Post.thread_id == tid))
    payload = {"body": "Observation", "tags": ["custom"], "idempotency_key": "flag"}
    first = await client.post(f"/api/posts/{pid}/flags", json=payload)
    assert first.status_code == 201, first.text
    assert (await client.post(f"/api/posts/{pid}/flags", json=payload)).json() == first.json()
    assert finding_count(app) == 1
    before = conversation_counts(app)
    assert (await client.post(f"/api/posts/{pid}/flags", json={**payload, "body": "Changed"})).status_code == 409
    assert finding_count(app) == 1 and conversation_counts(app) == before
    flag_id = first.json()["id"]
    resolved = await client.post(f"/api/flags/{flag_id}/resolve", json={"idempotency_key": "resolve"})
    assert resolved.status_code == 201, resolved.text
    assert (await client.post(f"/api/flags/{flag_id}/resolve", json={"idempotency_key": "resolve"})).json() == resolved.json()
    assert (await client.post(f"/api/flags/{flag_id}/resolve", json={"idempotency_key": "another-resolution"})).status_code == 409
    note_payload = {"body": "Note", "idempotency_key": "note"}
    note = await client.post(f"/api/runs/{rid}/notes", json=note_payload)
    assert note.status_code == 201
    assert (await client.post(f"/api/runs/{rid}/notes", json=note_payload)).json() == note.json()
    assert (await client.post(f"/api/runs/{rid}/notes", json={**note_payload, "body": "Changed note"})).status_code == 409
    assert finding_count(app) == 3


@pytest.mark.asyncio
async def test_findings_reject_missing_or_unowned_targets_and_invalid_payloads_without_domain_writes(force_client):
    app, client, _ = force_client
    rid, tid, _ = prepared_session(app)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        outside_thread = repo.create_thread(title="Unowned thread")
        unowned = repo.create_human_post(outside_thread.id, "Unowned history").post.id
        pid = session.scalar(select(Post.id).where(Post.thread_id == tid))
        nonflag = session.scalar(select(Event.id).where(Event.run_id == rid))
    before = conversation_counts(app)
    for path, payload, status in [
        ("/api/posts/missing/flags", {"body": "note", "idempotency_key": "missing"}, 404),
        (f"/api/posts/{unowned}/flags", {"tags": ["free"], "idempotency_key": "unowned"}, 409),
        (f"/api/flags/{nonflag}/resolve", {"idempotency_key": "wrong-event"}, 409),
        (f"/api/posts/{pid}/flags", {"tags": [3], "idempotency_key": "bad-tag"}, 422),
        (f"/api/posts/{pid}/flags", {"tags": [" "], "idempotency_key": "blank-tag"}, 409),
        (f"/api/posts/{pid}/flags", {"body": "note", "author": "forged", "idempotency_key": "forged"}, 422),
        (f"/api/runs/{rid}/notes", {"body": " ", "idempotency_key": "empty"}, 409),
    ]:
        response = await client.post(path, json=payload)
        assert response.status_code == status, response.text
        assert conversation_counts(app) == before and finding_count(app) == 0


@pytest.mark.asyncio
async def test_suggested_tags_are_configuration_only_and_returns_are_redacted(force_client, monkeypatch):
    app, client, _ = force_client
    monkeypatch.setenv("SWARMBOARD_SUGGESTED_FINDING_TAGS", '["replication", "unexpected"]')
    rid, tid, _ = prepared_session(app)
    with app.state.session_factory() as session:
        pid = session.scalar(select(Post.id).where(Post.thread_id == tid))
    settings = await client.get("/api/research/settings")
    assert settings.json()["suggested_tags"] == ["replication", "unexpected"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "planted-export-secret-13579")
    result = await client.post(f"/api/posts/{pid}/flags", json={
        "body": "Observed planted-export-secret-13579", "tags": ["outside suggestions"], "idempotency_key": "redact"})
    assert result.status_code == 201, result.text
    assert "planted-export-secret-13579" not in result.text
    listed = await client.get(f"/api/runs/{rid}/findings")
    assert "planted-export-secret-13579" not in listed.text
    assert listed.json()["flags"][0]["tags"] == ["outside suggestions"]


@pytest.mark.asyncio
async def test_finding_events_are_database_immutable(force_client):
    app, _, _ = force_client
    rid, _, _ = prepared_session(app)
    with app.state.session_factory.begin() as session:
        record = findings.note(Repository(session), run_id=rid, author="researcher", body="Immutable finding", idempotency_key="immutable")
    for sql in ("UPDATE events SET payload='{}' WHERE id=:id", "DELETE FROM events WHERE id=:id"):
        with pytest.raises(DBAPIError):
            with app.state.session_factory.begin() as session:
                session.execute(text(sql), {"id": record["id"]})
    with app.state.session_factory() as session:
        assert session.get(Event, record["id"]).payload["body"] == "Immutable finding"


@pytest.mark.asyncio
async def test_shared_findings_use_authenticated_identity_and_separate_idempotency_keys(tmp_path, monkeypatch):
    for name in ("SWARMBOARD_REQUIRE_AUTH", "SWARMBOARD_HOSTED", "RENDER", "SWARMBOARD_LOCAL_OPERATOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", json.dumps({"alice": "alice-password", "bob": "bob-password"}))
    app = create_app(database_url=f"sqlite:///{tmp_path / 'shared-findings.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        rid, _, _ = prepared_session(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            records = []
            for person in ("alice", "bob"):
                response = await client.post(f"/api/runs/{rid}/notes", json={
                    "body": "Shared observation", "idempotency_key": "same-client-key"}, auth=(person, f"{person}-password"))
                assert response.status_code == 201, response.text
                records.append(response.json())
            assert records[0]["id"] != records[1]["id"]
            assert [record["author"] for record in records] == ["alice", "bob"]
            assert finding_count(app) == 2


def test_invalid_suggested_tags_are_rejected_and_no_taxonomy_is_imposed(monkeypatch):
    for bad in ('{"tag":"value"}', '["ok", false]', 'not-json'):
        monkeypatch.setenv("SWARMBOARD_SUGGESTED_FINDING_TAGS", bad)
        with pytest.raises(InvalidStateError):
            findings.suggested_tags()
    monkeypatch.setenv("SWARMBOARD_SUGGESTED_FINDING_TAGS", '[" unusual ", "unusual", "user-defined"]')
    assert findings.suggested_tags() == ["unusual", "user-defined"]
