"""Human levers alter participant perception while retaining attributed traces."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from sqlalchemy import func, select

from swarmboard import autonomy
from swarmboard.gateways import AgentAction
from swarmboard.harness import register_persona
from swarmboard.models import Agent, Event, Memory, Post, Run, Stimulus, Thread, Turn
from swarmboard.persona_context import load_persona
from swarmboard.repository import Repository
from tests.test_harness import persona_directory
from tests.test_research_forced import force, force_client, prepared_session


async def mutation(client, path, *, method="POST", **payload):
    response = await client.request(method, path, json=payload)
    assert response.status_code in (200, 201), response.text
    return response.json()


async def take_turn(app, client, gateway, rid, aid, key):
    gateway.script.append(AgentAction(action="pass"))
    queued = await force(client, rid, aid, override_cooldown=True, idempotency_key=key)
    await app.state.engine.step(rid)
    with app.state.session_factory() as session:
        turn = session.scalar(select(Turn).where(Turn.stimulus_id == queued["stimulus_id"]))
        return {"id": turn.id, "prompt": turn.prompt, "context": turn.context_snapshot,
                "memory_ids": list(turn.retrieved_memory_ids), "provider": turn.provider, "model": turn.model}


@pytest.mark.asyncio
@pytest.mark.parametrize("ada", [False, True])
async def test_private_instruction_is_target_only_includes_ada_and_revocation_stops_injection(force_client, tmp_path, ada):
    app, client, gateway = force_client
    if ada:
        participant, _ = await register_persona(client, load_persona(persona_directory(tmp_path)))
        with app.state.session_factory.begin() as session:
            repo = Repository(session)
            target = repo.get_agent(participant["id"])
            peer = next(agent for agent in repo.list_agents(enabled_only=True) if agent.id != target.id)
            run = autonomy.create_session(repo, agents=[target, peer], continuous=False)
            for stimulus in repo.claim_stimuli(run_id=run.id, limit=100):
                repo.complete_stimulus(stimulus.id, claim_token=stimulus.claim_token)
            rid, ids = run.id, [target.id, peer.id]
    else:
        rid, _, ids = prepared_session(app)
    instruction = await mutation(client, f"/api/runs/{rid}/agents/{ids[0]}/instructions",
                                 body="PRIVATE_TARGET_ONLY_GUIDANCE", idempotency_key="private")
    target_turn = await take_turn(app, client, gateway, rid, ids[0], "target")
    peer_turn = await take_turn(app, client, gateway, rid, ids[1], "peer")
    assert "PRIVATE_TARGET_ONLY_GUIDANCE" in target_turn["prompt"]
    assert "PRIVATE_TARGET_ONLY_GUIDANCE" not in peer_turn["prompt"]
    assert target_turn["context"]["interventions"]["private_instructions"][0]["id"] == instruction["id"]
    if ada:
        assert '<persona_file name=' in target_turn["prompt"]
    await mutation(client, f'/api/instructions/{instruction["id"]}/revoke', idempotency_key="revoke-private")
    later_turn = await take_turn(app, client, gateway, rid, ids[0], "later-target")
    assert "PRIVATE_TARGET_ONLY_GUIDANCE" not in later_turn["prompt"]
    assert later_turn["context"]["interventions"]["private_instructions"] == []
    with app.state.session_factory() as session:
        assert session.get(Turn, target_turn["id"]).prompt == target_turn["prompt"]
    exported = await client.get(f"/api/runs/{rid}/export.jsonl")
    assert exported.status_code == 200, exported.text
    records = [json.loads(line) for line in exported.text.splitlines()]
    assert records[0]["interventions"]["instructions"][0]["revoked"] is True
    turns = {record["turn_id"]: record for record in records if record["record_type"] == "turn"}
    assert turns[target_turn["id"]]["interventions"]["private_instructions"][0]["id"] == instruction["id"]
    assert turns[later_turn["id"]]["interventions"]["private_instructions"] == []
    if ada:
        assert type(turns[target_turn["id"]]["persona"]["version"]) is int
        assert turns[target_turn["id"]]["persona"]["schema_version"]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_type", ["collaboration", "research"])
async def test_seeded_memories_are_targeted_hashed_and_deactivated_without_changing_prior_capture(force_client, session_type):
    app, client, gateway = force_client
    rid, _, ids = prepared_session(app, session_type=session_type)
    body = "PRIVATE_SEEDED_CONTEXT_EXACT_TEXT"
    memory = await mutation(client, f"/api/runs/{rid}/agents/{ids[0]}/memories", body=body,
                            tags=["source material"], active=True, idempotency_key="seed")
    target = await take_turn(app, client, gateway, rid, ids[0], "target-memory")
    other = await take_turn(app, client, gateway, rid, ids[1], "other-memory")
    assert memory["id"] in target["memory_ids"] and memory["id"] not in other["memory_ids"]
    assert body in target["prompt"] and body not in other["prompt"]
    captured = next(item for item in target["context"]["memories"] if item["id"] == memory["id"])
    assert captured["sha256"] == hashlib.sha256(body.encode()).hexdigest()
    await mutation(client, f'/api/memories/{memory["id"]}/deactivate', idempotency_key="deactivate")
    later = await take_turn(app, client, gateway, rid, ids[0], "after-memory")
    assert memory["id"] not in later["memory_ids"] and body not in later["prompt"]
    with app.state.session_factory() as session:
        assert session.get(Memory, memory["id"]).claim == body
        assert session.get(Turn, target["id"]).prompt == target["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_type", ["collaboration", "research"])
async def test_hot_swap_is_next_turn_only_and_retains_provider_model_persona_identity_for_inflight_call(force_client, monkeypatch, session_type):
    app, client, gateway = force_client
    rid, _, ids = prepared_session(app, session_type=session_type)
    with app.state.session_factory() as session:
        original = session.get(Agent, ids[0])
        original_provider, original_model, original_persona = original.provider, original.model, original.persona
    entered, release = asyncio.Event(), asyncio.Event()
    original_complete = gateway.complete
    observed = []
    async def blocking_complete(agent, messages, **kwargs):
        observed.append((agent.provider, agent.model, agent.persona))
        if len(observed) == 1:
            entered.set()
            await release.wait()
        return await original_complete(agent, messages, **kwargs)
    monkeypatch.setattr(gateway, "complete", blocking_complete)
    gateway.script.append(AgentAction(action="pass"))
    first = await force(client, rid, ids[0], override_cooldown=True, idempotency_key="before-swap")
    task = asyncio.create_task(app.state.engine.step(rid))
    await asyncio.wait_for(entered.wait(), timeout=3)
    new_persona = "A VERSIONED REPLACEMENT PERSONA"
    try:
        await mutation(client, f"/api/runs/{rid}/agents/{ids[0]}/configuration", method="PATCH",
                       provider="codex", model="gpt-6-astra", persona=new_persona,
                       settings={"sampling": {"reasoning_effort": "medium"}}, idempotency_key="swap")
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=3)
    second = await take_turn(app, client, gateway, rid, ids[0], "after-swap")
    with app.state.session_factory() as session:
        before = session.scalar(select(Turn).where(Turn.stimulus_id == first["stimulus_id"]))
        before_persona = before.context_snapshot["agent_snapshot"]["persona"]
        after_persona = second["context"]["agent_snapshot"]["persona"]
        assert before.provider == original_provider and before.model == original_model
        assert second["provider"] == "codex" and second["model"] == "gpt-6-astra"
        assert before_persona["sha256"] == hashlib.sha256(original_persona.encode()).hexdigest()
        assert after_persona["sha256"] == hashlib.sha256(new_persona.encode()).hexdigest()
        assert after_persona["version"] > before_persona["version"]
        assert new_persona not in before.prompt and new_persona in second["prompt"]
        event = session.scalar(select(Event).where(Event.run_id == rid, Event.event_type == "agent.config_changed"))
        assert event is not None and event.actor_id == "researcher"
        # The override belongs to this session, not the registered global peer.
        assert session.get(Agent, ids[0]).model == original_model
    assert observed == [(original_provider, original_model, original_persona), ("codex", "gpt-6-astra", new_persona)]


@pytest.mark.asyncio
async def test_impersonation_participant_view_and_mentions_differ_from_attributed_ledger_without_cooldown_or_budget(force_client):
    app, client, gateway = force_client
    rid, tid, ids = prepared_session(app, session_type="research", policy="permissive")
    with app.state.session_factory() as session:
        impersonated = session.get(Agent, ids[0])
        recipient = session.get(Agent, ids[1])
        handle, recipient_handle = impersonated.handle, recipient.handle
        last_spoke = impersonated.last_spoke_at
        counters = (session.get(Run, rid).rounds_used, session.get(Run, rid).tokens_used)
    response = await mutation(client, f"/api/threads/{tid}/research-posts", body=f"@{recipient_handle}, PERCEIVED_AGENT_WORDS",
                              as_handle=handle, idempotency_key="impersonate")
    with app.state.session_factory() as session:
        post = session.get(Post, response["post_id"])
        assert post.author_type == "human" and post.author_handle == "researcher"
        assert post.metadata_json["is_impersonation"] is True
        assert post.metadata_json["author_human"] == "researcher"
        assert post.metadata_json["displayed_as_agent"] == handle
        assert session.get(Agent, ids[0]).last_spoke_at == last_spoke
        assert (session.get(Run, rid).rounds_used, session.get(Run, rid).tokens_used) == counters
        planned = list(session.scalars(select(Stimulus).where(Stimulus.source_post_id == post.id)))
        assert len(planned) == 1 and planned[0].target_agent_id == ids[1]
        event = session.scalar(select(Event).where(Event.post_id == post.id, Event.event_type == "post.created"))
        assert event.actor_type == "human" and event.actor_id == "researcher"
        assert event.payload["author_human"] == "researcher"
    preview = (await client.get(f"/api/threads/{tid}/participant-view", params={"agent_id": ids[1]})).json()
    perceived = next(post for post in preview["context"]["posts"] if post["id"] == response["post_id"])
    assert perceived["author_type"] == "agent" and perceived["author_handle"] == handle
    assert perceived["author_agent_id"] == ids[0]
    assert "author_human" not in preview["prompt"] and "is_impersonation" not in preview["prompt"]
    gateway.script.append(AgentAction(action="reply", parent_post_id=response["post_id"],
                                      body="A threaded response without an explicit mention.", intent="clarify"))
    await app.state.engine.step(rid)
    with app.state.session_factory() as session:
        turn = session.scalar(select(Turn).where(Turn.run_id == rid))
        assert turn.agent_id == ids[1]
        assert turn.context_snapshot["interventions"]["impersonations"][0]["author_human"] == "researcher"
        assert "author_human" not in turn.prompt and "PERCEIVED_AGENT_WORDS" in turn.prompt
        invitations = list(session.scalars(select(Stimulus).where(Stimulus.source_post_id == turn.resulting_post_id)))
        assert len(invitations) == 1 and invitations[0].target_agent_id == ids[0]
    exported = await client.get(f"/api/runs/{rid}/export.jsonl")
    records = [json.loads(line) for line in exported.text.splitlines()]
    assert records[0]["interventions"]["impersonations"][0]["author_human"] == "researcher"
    exported_turn = next(record for record in records if record["record_type"] == "turn")
    assert exported_turn["interventions"]["impersonations"][0]["post_id"] == response["post_id"]
    exported_post = next(record for record in records if record["record_type"] == "post" and record["id"] == response["post_id"])
    assert exported_post["author_type"] == "human" and exported_post["metadata"]["displayed_as_agent"] == handle
    human_reply = await mutation(client, f"/api/threads/{tid}/posts",
                                 body="An ordinary human reply without an explicit mention.",
                                 parent_post_id=response["post_id"], idempotency_key="human-reply-to-impersonation")
    with app.state.session_factory() as session:
        invitations = list(session.scalars(select(Stimulus).where(Stimulus.source_post_id == human_reply["post"]["id"])))
        assert len(invitations) == 1 and invitations[0].target_agent_id == ids[0]
        assert invitations[0].payload["reason"] == "reply_to_author"
        assert session.get(Agent, ids[0]).last_spoke_at == last_spoke


@pytest.mark.asyncio
@pytest.mark.parametrize("author_fields", [{"as_handle": "anyone"}, {"system_author": True}])
async def test_collaboration_rejects_research_author_levers_without_post_or_stimulus(force_client, author_fields):
    app, client, _ = force_client
    rid, tid, _ = prepared_session(app)
    with app.state.session_factory() as session:
        before = [session.scalar(select(func.count()).select_from(model)) for model in (Post, Stimulus)]
    response = await client.post(f"/api/threads/{tid}/research-posts", json={
        "body": "Disallowed author", "idempotency_key": "not-research", **author_fields})
    assert response.status_code == 409, response.text
    with app.state.session_factory() as session:
        assert [session.scalar(select(func.count()).select_from(model)) for model in (Post, Stimulus)] == before


@pytest.mark.asyncio
async def test_system_notice_has_distinct_participant_author_and_human_ledger(force_client):
    app, client, _ = force_client
    rid, tid, ids = prepared_session(app, session_type="research", policy="permissive")
    result = await mutation(client, f"/api/threads/{tid}/research-posts", body="The board will end after the remaining budget.",
                            system_author=True, idempotency_key="notice")
    preview = (await client.get(f"/api/threads/{tid}/participant-view", params={"agent_id": ids[0]})).json()
    post = next(post for post in preview["context"]["posts"] if post["id"] == result["post_id"])
    assert post["author_type"] == "system" and post["author_handle"] == "SYSTEM"
    with app.state.session_factory() as session:
        ledger = session.get(Post, result["post_id"])
        assert ledger.author_type == "human" and ledger.metadata_json["author_human"] == "researcher"
        assert ledger.metadata_json["is_system_notice"]


@pytest.mark.asyncio
async def test_configuration_is_pinned_between_selection_and_provider_dispatch(force_client, monkeypatch):
    app, client, gateway = force_client
    rid, _, ids = prepared_session(app, session_type="research", policy="permissive")
    with app.state.session_factory() as session:
        original_model = session.get(Agent, ids[0]).model
    observed = []
    def reply(participant, messages):
        observed.append((participant.model, messages[0].content))
        return AgentAction(action="pass")
    gateway.script.append(reply)
    original_execute = app.state.engine._execute_turn
    async def change_after_selection(turn_id, **kwargs):
        await mutation(client, f"/api/runs/{rid}/agents/{ids[0]}/configuration", method="PATCH",
                       model="qwen/next-selection-only", persona="NOT_THE_SELECTED_PERSONA", idempotency_key="between-selection-dispatch")
        return await original_execute(turn_id, **kwargs)
    monkeypatch.setattr(app.state.engine, "_execute_turn", change_after_selection)
    queued = await force(client, rid, ids[0], idempotency_key="selection-pin")
    await app.state.engine.step(rid)
    assert len(observed) == 1 and observed[0][0] == original_model
    assert "NOT_THE_SELECTED_PERSONA" not in observed[0][1]
    with app.state.session_factory() as session:
        turn = session.scalar(select(Turn).where(Turn.stimulus_id == queued["stimulus_id"]))
        assert turn.model == original_model
        assert turn.context_snapshot["agent_snapshot"]["configuration"]["model"] == original_model


@pytest.mark.asyncio
async def test_fork_after_session_override_captures_and_executes_effective_configuration(force_client):
    app, client, gateway = force_client
    rid, tid, ids = prepared_session(app)
    with app.state.session_factory() as session:
        opening = session.scalar(select(Post.id).where(Post.thread_id == tid))
    persona = "FORK_INHERITS_EFFECTIVE_SESSION_PERSONA"
    await mutation(client, f"/api/runs/{rid}/agents/{ids[0]}/configuration", method="PATCH",
                   provider="codex", model="gpt-6-astra", persona=persona, settings={}, idempotency_key="override-before-fork")
    fork = await mutation(client, f"/api/threads/{tid}/fork", at_post_id=opening,
                          policy="permissive", idempotency_key="fork-with-override")
    with app.state.session_factory.begin() as session:
        repo = Repository(session)
        cloned = repo.get_run(fork["run_id"])
        snapshot = next(participant for participant in cloned.config["fork_roster"] if participant["id"] == ids[0])
        assert snapshot["provider"] == "codex" and snapshot["model"] == "gpt-6-astra"
        assert snapshot["persona"] == persona
        for stimulus in repo.claim_stimuli(run_id=cloned.id, limit=100):
            repo.complete_stimulus(stimulus.id, claim_token=stimulus.claim_token)
    result = await take_turn(app, client, gateway, fork["run_id"], ids[0], "fork-turn")
    assert result["provider"] == "codex" and result["model"] == "gpt-6-astra"
    assert persona in result["prompt"]


@pytest.mark.asyncio
async def test_fork_can_add_registered_participant_outside_source_roster_without_modifying_source(force_client):
    app, client, gateway = force_client
    rid, tid, ids = prepared_session(app)
    with app.state.session_factory() as session:
        repo = Repository(session)
        outsider = next(agent for agent in repo.list_agents(enabled_only=True) if agent.id not in ids)
        outsider_id, outsider_model = outsider.id, outsider.model
        opening = session.scalar(select(Post.id).where(Post.thread_id == tid))
        source_config = json.loads(json.dumps(repo.get_run(rid).config))
        source_events = list(session.scalars(select(Event.id).where(Event.run_id == rid).order_by(Event.id)))
        source_posts = [(post.id, post.body, post.author_handle) for post in repo.list_posts(tid)]
    fork = await mutation(client, f"/api/threads/{tid}/fork", at_post_id=opening,
                          agent_ids=[*ids, outsider_id], policy="permissive", idempotency_key="fork-new-participant")
    result = await take_turn(app, client, gateway, fork["run_id"], outsider_id, "new-participant-turn")
    assert result["model"] == outsider_model
    assert [call["agent_id"] for call in gateway.calls] == [outsider_id]
    with app.state.session_factory() as session:
        repo = Repository(session)
        assert outsider_id in repo.get_run(fork["run_id"]).config["agent_ids"]
        assert repo.get_run(rid).config == source_config
        assert list(session.scalars(select(Event.id).where(Event.run_id == rid).order_by(Event.id))) == source_events
        assert [(post.id, post.body, post.author_handle) for post in repo.list_posts(tid)] == source_posts


@pytest.mark.asyncio
async def test_fork_config_overrides_reject_unvalidated_agent_settings_without_any_writes(force_client):
    app, client, _ = force_client
    _, tid, ids = prepared_session(app)
    with app.state.session_factory() as session:
        opening = session.scalar(select(Post.id).where(Post.thread_id == tid))
        before = [session.scalar(select(func.count()).select_from(model)) for model in (Run, Thread, Post, Stimulus, Event)]
    response = await client.post(f"/api/threads/{tid}/fork", json={
        "at_post_id": opening, "idempotency_key": "forbidden-agent-overrides",
        "config_overrides": {"agent_overrides": {ids[0]: {"provider": "openai_compatible",
            "settings": {"base_url": "http://127.0.0.1/private", "api_key_env": "SWARMBOARD_CODEX_API_KEY"}}}},
    })
    assert response.status_code == 409, response.text
    with app.state.session_factory() as session:
        assert [session.scalar(select(func.count()).select_from(model)) for model in (Run, Thread, Post, Stimulus, Event)] == before
