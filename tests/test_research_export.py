from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timezone
from zipfile import ZipFile

import httpx
import pytest
from sqlalchemy import select

from swarmboard import findings, research, research_export, sessions
from swarmboard.app import create_app
from swarmboard.models import Event, Post, Turn
from swarmboard.repository import Repository
from .test_engine_acceptance import ScriptedGateway


@pytest.fixture
async def export_client(tmp_path, monkeypatch):
    monkeypatch.setattr("swarmboard.config.load_dotenv", lambda **kwargs: None)
    for name in ("SWARMBOARD_AUTH_USERS", "SWARMBOARD_REQUIRE_AUTH", "SWARMBOARD_HOSTED", "RENDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(research_export, "PAGE_SIZE", 2)
    app = create_app(database_url=f"sqlite:///{tmp_path / 'export.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield app, client


def seed(factory):
    with factory.begin() as session:
        repo = Repository(session)
        agent = repo.create_agent(handle="export_peer", persona="A stable test persona", model="qwen/test")
        run = sessions.create_session(repo, agents=[agent], body="An opening without a generated turn",
                                       continuous=False, author_handle="researcher")
        thread = repo.list_threads(run_id=run.id)[0]
        opening = repo.list_posts(thread.id)[0]
        prompt = '[{"role":"system","content":"A stable test persona\\r\\n"}]'
        outcomes = ["executed", "passed", "invalid_output", "provider_failure", "rejected_by_policy", "passed", "passed"]
        ids = []
        for index, outcome in enumerate(outcomes):
            turn = repo.create_turn(thread_id=thread.id, agent_id=agent.id,
                                    context_post_ids=[opening.id], context_snapshot={"posts": [{"id": opening.id}]},
                                    prompt=prompt, provider=agent.provider, model=agent.model)
            turn.started_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
            raw = f' \r\n{{"artifact": {index}, "unknown": "retained", "unicode": "雪 café"}}\t '
            result_id = None
            if outcome == "executed":
                result_id = repo.create_agent_post(thread.id, agent.id, "The generated contribution", parent_post_id=opening.id).post.id
            error = "invalid agent action" if outcome == "invalid_output" else (
                "provider failed" if outcome == "provider_failure" else ("policy rejected" if outcome == "rejected_by_policy" else None))
            repo.finish_turn(turn.id, state="completed" if outcome == "executed" else "passed" if outcome == "passed" else "failed",
                              outcome=outcome, raw_output=raw, error=error, resulting_post_id=result_id,
                              parsed_action={"unknown": "preserved"} if outcome == "invalid_output" else None,
                              input_tokens=2, output_tokens=3, latency_ms=17)
            ids.append(turn.id)
        extra = repo.create_human_post(thread.id, "Evidence added after the model turns", author_handle="researcher")
        marked = findings.flag(repo, target_type="turn", target_id=ids[0], author="researcher",
                               tags=["interesting"], body="Inspect this contribution", idempotency_key="flag-executed")
        findings.resolve_flag(repo, flag_event_id=marked["id"], author="reviewer", body="Checked",
                               idempotency_key="resolve-executed")
        findings.flag(repo, target_type="post", target_id=extra.post.id, author="researcher",
                       tags=["evidence"], idempotency_key="flag-human")
        findings.note(repo, run_id=run.id, author="researcher", body="A run-level observation", idempotency_key="note-1")
        return run.id, thread.id, agent.id, opening.id, ids, prompt


def lines(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    return [json.loads(line) for line in value.splitlines()]


@pytest.mark.asyncio
async def test_jsonl_is_complete_across_keyset_pages_and_keeps_failure_artifacts(export_client):
    app, client = export_client
    run_id, _, _, _, turn_ids, prompt = seed(app.state.session_factory)
    response = await client.get(f"/api/runs/{run_id}/export.jsonl")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    exported = lines(response.content)
    assert exported[0]["record_type"] == "header"
    assert exported[0]["session_type"] == "collaboration"
    assert exported[0]["policy"]["profile"] == "production"
    assert exported[0]["roster"][0]["settings"]["api_key_env"] == "OPENROUTER_API_KEY"
    assert all(record["export_schema_version"] == 1 for record in exported)
    turns = [record for record in exported if record["record_type"] == "turn"]
    assert [record["turn_id"] for record in turns] == sorted(turn_ids)
    assert [record["turn_sequence"] for record in turns] == list(range(1, 8))
    assert len({record["turn_id"] for record in turns}) == len(turn_ids)
    assert {record["outcome"] for record in turns} == {"executed", "passed", "invalid_output", "provider_failure", "rejected_by_policy"}
    for turn in turns:
        assert turn["prompt"] == prompt
        assert turn["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
        assert turn["raw_output"].startswith(" \r\n") and turn["raw_output"].endswith("\t ")
        assert "雪 café" in turn["raw_output"]
        assert not turn["raw_output_redacted"] and not turn["prompt_redacted"]
        assert turn["total_tokens"] == 5 and turn["latency_ms"] == 17
        assert turn["notes"][0]["body"] == "A run-level observation"
    invalid = next(turn for turn in turns if turn["outcome"] == "invalid_output")
    assert invalid["parsed_action"] == {"unknown": "preserved"}
    assert invalid["resulting_post_id"] is None
    executed = next(turn for turn in turns if turn["outcome"] == "executed")
    assert executed["flags"][0]["resolved"] is True
    assert executed["flags"][0]["resolution"]["author"] == "reviewer"
    posts = [record for record in exported if record["record_type"] == "post"]
    assert len(posts) == 3
    assert {post["author_type"] for post in posts} == {"human", "agent"}
    assert any(post["flags"] for post in posts)
    assert app.state.engine.gateway.calls == []


@pytest.mark.asyncio
async def test_prompt_omission_retains_hash_and_reference_and_zip_has_complete_members(export_client):
    app, client = export_client
    run_id, _, _, _, turn_ids, prompt = seed(app.state.session_factory)
    response = await client.get(f"/api/runs/{run_id}/export.zip?include_prompts=false")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    with ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"turns.jsonl", "events.jsonl", "findings.jsonl"}
        turns = [record for record in lines(archive.read("turns.jsonl")) if record["record_type"] == "turn"]
        assert {turn["turn_id"] for turn in turns} == set(turn_ids)
        for turn in turns:
            assert "prompt" not in turn
            assert turn["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
            assert turn["prompt_ref"] == f"/api/turns/{turn['turn_id']}"
        event_ids = [record["id"] for record in lines(archive.read("events.jsonl"))]
        with app.state.session_factory() as session:
            expected = list(session.scalars(select(Event.id).where(Event.run_id == run_id).order_by(Event.id)))
        assert event_ids == expected
        captured = lines(archive.read("findings.jsonl"))
        assert [record["record_type"] for record in captured].count("flag") == 2
        assert [record["record_type"] for record in captured].count("note") == 1
    events = await client.get(f"/api/runs/{run_id}/events.jsonl")
    assert [record["id"] for record in lines(events.content)] == expected


@pytest.mark.asyncio
async def test_every_export_surface_redacts_credentials_without_mutating_captures(export_client, monkeypatch):
    app, client = export_client
    secret = 'planted-export-secret-quote"-newline\n-end'
    literal = "literal-credential-not-in-environment"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    run_id, _, agent_id, _, turn_ids, _ = seed(app.state.session_factory)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        run = repo.get_run(run_id)
        run.config = {**run.config, "nested": {"api_key": literal, "label": secret}}
        agent = repo.get_agent(agent_id)
        agent.settings = {**agent.settings, "sampling": {"label": secret}, "api_key": literal}
        turn = repo.get_turn(turn_ids[0])
        turn.raw_output = f" \r\nRaw output with {secret}\t "
        turn.prompt = json.dumps([{"role": "user", "content": secret}])
        turn.parsed_action = {"extra": {secret: secret, "api_key": literal}}
        turn.context_snapshot = {"nested": [secret, {"password": literal}]}
        turn.scheduler_scores = {"forced_by": secret, "candidates": [{"detail": secret}]}
        turn.rejection_reason = secret
        original_raw, original_prompt = turn.raw_output, turn.prompt
        repo.add_event("export.secret_fixture", run_id=run_id, payload={"nested": [{secret: secret, "api_key": literal}]})
        findings.note(repo, run_id=run_id, author=f"operator-{secret}", body=f"note-{secret}", tags=[secret], idempotency_key="secret-note")
        flag = findings.flag(repo, target_type="turn", target_id=turn.id, author="researcher",
                              body=secret, idempotency_key="secret-flag")
        findings.resolve_flag(repo, flag_event_id=flag["id"], author="reviewer", body=secret, idempotency_key="secret-resolution")
    for suffix in ("export.jsonl", "events.jsonl", "export.zip"):
        response = await client.get(f"/api/runs/{run_id}/{suffix}")
        assert response.status_code == 200, response.text
        if suffix.endswith("zip"):
            with ZipFile(io.BytesIO(response.content)) as archive:
                documents = [archive.read(name).decode() for name in archive.namelist()]
        else:
            documents = [response.text]
        for document in documents:
            assert secret not in document and json.dumps(secret, ensure_ascii=False)[1:-1] not in document
            assert literal not in document
            assert "[REDACTED]" in document
            assert any(record["redacted"] for record in lines(document))
    response = await client.get(f"/api/runs/{run_id}/export.jsonl")
    turn = next(record for record in lines(response.content) if record.get("turn_id") == turn_ids[0])
    assert turn["raw_output_redacted"] and turn["prompt_redacted"]
    assert turn["prompt_sha256"] == hashlib.sha256(original_prompt.encode()).hexdigest()
    assert turn["raw_output_sha256"] == hashlib.sha256(original_raw.encode()).hexdigest()
    with app.state.session_factory() as session:
        stored = session.get_one(Turn, turn_ids[0])
        assert stored.raw_output == original_raw and stored.prompt == original_prompt


@pytest.mark.asyncio
async def test_fork_and_resample_exports_preserve_lineage_inherited_context_and_forcing(export_client):
    app, client = export_client
    run_id, thread_id, agent_id, opening_id, turn_ids, prompt = seed(app.state.session_factory)
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        branch = research.fork(repo, thread_id=thread_id, at_post_id=opening_id, author="researcher")
        samples = research.resample(repo, turn_id=turn_ids[0], author="researcher", n=2, idempotency_key="export-samples")
        sample = samples["forks"][0]
        forced = repo.get_stimulus(sample["stimulus_id"])
        turn = repo.create_turn(thread_id=sample["thread_id"], agent_id=agent_id,
                                stimulus_id=forced.id, context_post_ids=[opening_id], prompt=prompt,
                                scheduler_scores={"forced": True, "forced_by": "researcher"})
        repo.finish_turn(turn.id, state="passed", raw_output='{"action":"pass"}')
        child_turn_id = turn.id
    response = await client.get(f"/api/runs/{branch['run_id']}/export.jsonl")
    records = lines(response.content)
    assert records[0]["lineage"]["parent_run_id"] == run_id
    assert records[0]["session_type"] == "research"
    inherited = [record for record in records if record["record_type"] == "post"]
    assert len(inherited) == 1 and inherited[0]["is_inherited"]
    assert inherited[0]["metadata"]["inherited_from_post_id"] == opening_id
    response = await client.get(f"/api/runs/{sample['run_id']}/export.jsonl")
    records = lines(response.content)
    assert records[0]["sibling_group_id"] == samples["sibling_group_id"]
    child_turn = next(record for record in records if record.get("turn_id") == child_turn_id)
    assert child_turn["source_turn_id"] == turn_ids[0]
    assert child_turn["forced"] and child_turn["forced_by"] == "researcher"
    assert child_turn["reuse_turn_id"] == turn_ids[0]
    assert child_turn["inherited_context_post_ids"] == [sample["post_id_map"][opening_id]]
    assert child_turn["context_post_id_map"][opening_id] == sample["post_id_map"][opening_id]


@pytest.mark.asyncio
async def test_stream_holds_one_snapshot_while_new_turns_commit(export_client):
    app, _ = export_client
    run_id, thread_id, agent_id, _, turn_ids, _ = seed(app.state.session_factory)
    stream = research_export.jsonl(app.state.session_factory, run_id)
    header = json.loads(next(stream))
    assert header["record_type"] == "header"
    with app.state.session_factory.begin() as session:
        late = Repository(session).create_turn(thread_id=thread_id, agent_id=agent_id)
        late_id = late.id
    original = [json.loads(line) for line in stream]
    assert {record["turn_id"] for record in original if record["record_type"] == "turn"} == set(turn_ids)
    later = [json.loads(line) for line in research_export.jsonl(app.state.session_factory, run_id)]
    assert late_id in {record["turn_id"] for record in later if record["record_type"] == "turn"}


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["export.jsonl", "events.jsonl", "export.zip"])
async def test_unknown_export_run_fails_before_streaming(export_client, suffix):
    _, client = export_client
    response = await client.get(f"/api/runs/does-not-exist/{suffix}")
    assert response.status_code == 404
