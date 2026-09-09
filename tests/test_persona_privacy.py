"""Public provenance hides server paths without changing captured artifacts."""
import io
import json
from zipfile import ZipFile

import pytest

from swarmboard import research, sessions
from swarmboard.credentials import redact
from swarmboard.models import Agent, Event, Turn
from swarmboard.repository import Repository
from .test_response_security import secured_client


@pytest.mark.parametrize("source", ["/srv/private owner/café/persona", r"C:\private\café\persona"])
def test_snapshot_sources_are_hidden_in_nested_serialized_captures(source):
    snapshot = {"version": 1, "source": source, "instructions": "Exact instructions.\r\n", "memory": "Exact memory."}
    original = json.dumps([{"content": json.dumps({"persona_harness": snapshot})}], indent=2)
    cleaned = redact(original)
    decoded = json.loads(json.loads(cleaned)[0]["content"])["persona_harness"]
    assert decoded == {**snapshot, "source": "server-managed"}
    assert redact({"source": source, "note": "authored citation"})["source"] == source
    override = {**snapshot, "source": "session-override:abc-123"}
    assert redact(override) == override
    assert json.loads(json.loads(original)[0]["content"])["persona_harness"] == snapshot


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("escaping", ["ordinary", "slashes", "unicode"])
def test_source_masking_handles_json_string_layers_and_alternate_escapes(depth, escaping):
    source = "/srv/private/persona"
    snapshot = {"version": 1, "source": source, "instructions": "Test.", "memory": "Test memory."}
    encoded = json.dumps(snapshot)
    if escaping == "slashes":
        encoded = encoded.replace("/", r"\/")
    elif escaping == "unicode":
        encoded = encoded.replace("/srv", r"\u002f\u0073\u0072\u0076")
    for _ in range(depth - 1):
        encoded = json.dumps(encoded)
    decoded = redact(encoded)
    for _ in range(depth):
        decoded = json.loads(decoded)
    assert decoded == {**snapshot, "source": "server-managed"}


@pytest.mark.asyncio
async def test_persona_paths_hidden_in_state_history_and_exports(secured_client):
    app, client = secured_client
    source = "/srv/private-owner/persona"
    snapshot = {"version": 1, "source": source, "instructions": "Test instructions.", "memory": "Test memory."}
    prompt = json.dumps([{"role": "system", "content": json.dumps({"persona_harness": snapshot})}])
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="privacy_ada", model="gpt-6-astra", provider="codex", persona="Test",
                                  settings={"persona_harness": snapshot})
        run = sessions.create_session(repo, agents=[agent], title="Privacy test", body="Opening",
                                      continuous=False, author_handle="collaborator", session_type="research")
        thread = repo.list_threads(run_id=run.id)[0]
        opening = repo.list_posts(thread.id)[0]
        turn = repo.create_turn(thread_id=thread.id, agent_id=agent.id, prompt=prompt,
                               context_post_ids=[opening.id], context_snapshot={"persona_harness": snapshot})
        repo.finish_turn(turn.id, state="passed", raw_output="Test capture", parsed_action={"action": "pass"})
        event = repo.add_event("provider.response", run_id=run.id, thread_id=thread.id, agent_id=agent.id,
                               payload={"turn_id": turn.id, "metadata": {"snapshot": snapshot}})
        research.fork(repo, thread_id=thread.id, at_post_id=opening.id, author="collaborator")
        run_id, thread_id, turn_id, agent_id, event_id = run.id, thread.id, turn.id, agent.id, event.id
    for path in ["/api/state", f"/api/turns/{turn_id}", f"/api/runs/{run_id}/replay",
                 f"/api/sessions/{run_id}", f"/api/sessions/{run_id}/export", "/api/events?once=true",
                 f"/api/threads/{thread_id}/forks", f"/api/runs/{run_id}/events.jsonl",
                 f"/api/runs/{run_id}/export.jsonl?include_prompts=true",
                 f"/api/runs/{run_id}/export.zip?include_prompts=true"]:
        response = await client.get(path)
        assert response.status_code == 200, path
        if "export.zip" in path:
            with ZipFile(io.BytesIO(response.content)) as archive:
                body = "\n".join(archive.read(name).decode() for name in archive.namelist())
        else:
            body = response.text
        assert source not in body, path
        assert response.headers["referrer-policy"] == "no-referrer"
    with app.state.session_factory() as session:
        assert session.get(Agent, agent_id).settings["persona_harness"] == snapshot
        assert session.get(Turn, turn_id).prompt == prompt
        assert session.get(Turn, turn_id).context_snapshot["persona_harness"] == snapshot
        assert session.get(Event, event_id).payload["metadata"]["snapshot"] == snapshot
    assert app.state.engine.gateway.calls == []


@pytest.mark.asyncio
async def test_reload_failure_hides_private_directory(secured_client, monkeypatch):
    _, client = secured_client
    source = "/srv/private-owner/persona/AGENTS.md"

    def cannot_load(_):
        raise FileNotFoundError(2, "Missing test file", source)

    monkeypatch.setattr("swarmboard.app.load_persona", cannot_load)
    response = await client.post("/api/personas/ada/reload")
    assert response.status_code == 409
    assert source not in response.text and "private-owner" not in response.text
    assert "could not be loaded" in response.json()["detail"]
