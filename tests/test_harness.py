from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from swarmboard.app import create_app
from swarmboard.gateways import AgentAction, ChatMessage, OpenAICompatibleGateway
from swarmboard.harness import register_persona, start_discussion
from swarmboard.models import Run, Stimulus, Turn
from swarmboard.persona_context import ADA_BOARD_DELIVERY, HARNESS_PROMPT_VERSION, PersonaSnapshot, delivery_prompt_version, harness_prompt, load_persona

from .test_engine_acceptance import ScriptedGateway, wait_until


def persona_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "persona 1"
    directory.mkdir()
    (directory / "AGENTS.md").write_text("Read memory.md in full. Act on the user's behalf.", encoding="utf-8")
    # Exceed the ordinary Agent.persona API limit to catch silent truncation or
    # accidental reliance on that short biography field.
    (directory / "memory.md").write_text("The user values curiosity.\n" * 900, encoding="utf-8")
    return directory


def test_ada_fixed_environment_uses_files_without_adding_personality(tmp_path: Path) -> None:
    snapshot = load_persona(persona_directory(tmp_path))
    before = snapshot.model_dump_json()
    ada_prompt = harness_prompt(snapshot, handle="ada")
    other_prompt = harness_prompt(snapshot, handle="another_persona")

    assert ADA_BOARD_DELIVERY not in ada_prompt
    assert "Your handle on this board is @ada." in ada_prompt
    assert "supplied in full and unchanged. Follow these files." in ada_prompt
    assert "This environment is fixed: board actions do not modify the files." in ada_prompt
    assert ada_prompt.index("Board interface:") > ada_prompt.rindex("</persona_file>")
    assert "Return one JSON action matching the supplied schema." in ada_prompt
    for imposed in ("Draw your personality", "Board-post delivery:", "social-media-brusque",
                    "Choose your own agenda", "conversational style", "required consensus"):
        assert imposed not in ada_prompt
    assert "Draw your personality, voice, priorities, and interaction style" in other_prompt
    assert "Swarmboard interface for @another_persona." in other_prompt
    assert ADA_BOARD_DELIVERY not in other_prompt
    for prompt in (ada_prompt, other_prompt):
        assert f'<persona_file name="AGENTS.md">\n{snapshot.instructions}\n</persona_file>' in prompt
        assert f'<persona_file name="memory.md">\n{snapshot.memory}\n</persona_file>' in prompt
    assert snapshot.model_dump_json() == before


@pytest.mark.asyncio
async def test_persona_and_peer_exchange_autonomously_with_captured_files(tmp_path: Path) -> None:
    directory = persona_directory(tmp_path)
    snapshot = load_persona(directory)

    def persona_reply(agent, messages):
        assert agent.handle == "ada"
        assert snapshot.instructions in messages[0].content
        assert snapshot.memory in messages[0].content
        assert "not an assistant, panelist" not in messages[0].content
        assert messages[0].content.startswith(harness_prompt(snapshot, handle="ada"))
        assert ADA_BOARD_DELIVERY not in messages[0].content
        assert "Draw your personality" not in messages[0].content
        assert "Choose your own agenda" not in messages[0].content
        assert "INVENTED_PERSONALITY" not in messages[0].content
        assert "INVENTED_ROLE" not in messages[0].content
        context = json.loads(messages[1].content.split("\n", 1)[1])
        assert {peer["handle"] for peer in context["participants"]} == {"ada", "wintermute"}
        assert next(peer for peer in context["participants"] if peer["handle"] == "ada") == {"handle": "ada"}
        assert context["persona_snapshot"]["sha256"] == snapshot.digest
        assert context["persona_snapshot"]["files"] == snapshot.file_manifest
        assert context["persona_snapshot"]["prompt_version"] == delivery_prompt_version(HARNESS_PROMPT_VERSION, handle="ada", file_backed=True)
        return AgentAction(action="reply", body="@wintermute, what would you explore first?", intent="clarify")

    def peer_reply(agent, messages):
        assert agent.handle == "wintermute"
        assert snapshot.memory not in messages[0].content
        assert ADA_BOARD_DELIVERY not in messages[0].content
        assert "@wintermute, what would you explore first?" in messages[1].content
        context = json.loads(messages[1].content.split("\n", 1)[1])
        parent = next(post["id"] for post in context["posts"] if post["author_handle"] == "ada")
        # A direct threaded reply must hand back to Ada without an @mention.
        return AgentAction(action="reply", parent_post_id=parent, body="Ada, I would compare two possible weekend plans.", intent="support")

    def persona_pass(agent, messages):
        assert agent.handle == "ada"
        assert "compare two possible weekend plans" in messages[1].content
        # Even if source files changed, this registration keeps its captured text.
        assert snapshot.memory in messages[0].content
        assert ADA_BOARD_DELIVERY not in messages[0].content
        assert "Changed on disk" not in messages[0].content
        return AgentAction(action="pass")

    gateway = ScriptedGateway(persona_reply, peer_reply, persona_pass)
    app = create_app(database_url=f"sqlite:///{tmp_path / 'board.db'}", gateway=gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            agent, source = await register_persona(client, snapshot, model_from="dr_benway", cooldown_seconds=0)
            assert source == "dr_benway"
            assert agent["role"] == "participant"
            assert agent["settings"]["expertise"] == []
            assert "scheduler_role" not in agent["settings"]
            # Ordinary metadata must not inject a second personality into the
            # file-backed prompt, even if a caller edits it through the API.
            updated = await client.patch(f"/api/agents/{agent['id']}", json={
                "persona": "INVENTED_PERSONALITY", "role": "INVENTED_ROLE",
            })
            assert updated.status_code == 200
            (directory / "memory.md").write_text("Changed on disk", encoding="utf-8")
            thread, run = await start_discussion(client, agent, peers=["wintermute"], max_rounds=3)

            def completed():
                with app.state.session_factory() as session:
                    return session.get(Run, run["id"]).state == "completed"

            await wait_until(completed, timeout=5)
            assert len(gateway.calls) == 3
            detail = (await client.get(f"/api/threads/{thread['thread_id']}")).json()
            assert [post["author_handle"] for post in detail["posts"]] == ["human", "ada", "wintermute"]
            with app.state.session_factory() as session:
                turns = list(session.scalars(select(Turn).where(Turn.agent_id == agent["id"])))
                assert len(turns) == 2
                assert all(snapshot.memory in json.loads(turn.prompt)[0]["content"] for turn in turns)
                assert all(ADA_BOARD_DELIVERY not in json.loads(turn.prompt)[0]["content"] for turn in turns)
                assert all(turn.prompt_version == delivery_prompt_version(HARNESS_PROMPT_VERSION, handle="ada", file_backed=True) for turn in turns)
            rerun = (await client.post(f"/api/runs/{run['id']}/rerun")).json()
            # Counterfactual reruns preserve participant selection too.
            with app.state.session_factory() as session:
                assert session.get(Run, rerun["run"]["id"]).config["agent_ids"] == run["config"]["agent_ids"]
            await client.post(f"/api/runs/{rerun['run']['id']}/stop")


@pytest.mark.asyncio
async def test_reload_updates_only_the_harness_participant(tmp_path: Path) -> None:
    directory = persona_directory(tmp_path)
    app = create_app(database_url=f"sqlite:///{tmp_path / 'board.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            original = (await client.get("/api/state")).json()["agents"]
            first, _ = await register_persona(client, load_persona(directory))
            assert (first["provider"], first["model"]) == ("codex", "gpt-6-astra")
            await client.patch(f"/api/agents/{first['id']}", json={"model": "custom-codex-model"})
            (directory / "memory.md").write_text("A revised preference.", encoding="utf-8")
            second, _ = await register_persona(client, load_persona(directory))
            assert first["id"] == second["id"]
            assert (second["provider"], second["model"]) == ("codex", "custom-codex-model")
            assert second["settings"]["persona_harness"]["memory"] == "A revised preference."
            with pytest.raises(ValueError, match="already belongs"):
                await register_persona(client, load_persona(directory), handle="wintermute")
            with pytest.raises(ValueError, match="missing or disabled"):
                await start_discussion(client, second, peers=["not-a-participant"])
            final = (await client.get("/api/state")).json()
            assert len(final["agents"]) == len(original) + 1
            assert not final["threads"]
            assert [agent for agent in final["agents"] if agent["id"] != first["id"]] == original


def test_persona_requires_both_nonempty_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Required persona file"):
        load_persona(tmp_path)
    directory = persona_directory(tmp_path)
    (directory / "memory.md").write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        load_persona(directory)


@pytest.mark.asyncio
async def test_persona_file_bytes_survive_snapshot_and_provider_payload(tmp_path: Path) -> None:
    originals = {
        "AGENTS.md": b"\xef\xbb\xbfRead memory.md.\r\nKeep every byte.\rTrailing spaces.  \n\n",
        "memory.md": "\ufeffAn unabridged voice: curious, blunt, emphatic.\r\nCaf\u00e9 \u2014 \u2603\r\t  ".encode("utf-8"),
    }
    for filename, original in originals.items():
        (tmp_path / filename).write_bytes(original)
    loaded = load_persona(tmp_path)
    # Agent settings are stored and transported as JSON before turns use them.
    snapshot = PersonaSnapshot.model_validate_json(loaded.model_dump_json())
    assert snapshot.instructions.encode("utf-8") == originals["AGENTS.md"]
    assert snapshot.memory.encode("utf-8") == originals["memory.md"]
    assert snapshot.file_manifest == {
        filename: {"bytes": len(original), "sha256": hashlib.sha256(original).hexdigest()}
        for filename, original in originals.items()
    }
    prompt = harness_prompt(snapshot, handle="ada")
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        sent = json.loads(request.content)["messages"][0]
        assert sent == {"role": "system", "content": prompt}
        for filename, original in originals.items():
            section = f'<persona_file name="{filename}">\n'.encode("utf-8") + original + b"\n</persona_file>"
            assert section in sent["content"].encode("utf-8")
            assert (tmp_path / filename).read_bytes() == original
        return httpx.Response(200, json={
            "choices": [{"message": {"content": AgentAction(action="pass").model_dump_json()}}],
        })

    gateway = OpenAICompatibleGateway("http://provider.test/v1", transport=httpx.MockTransport(handler))
    result = await gateway.complete(model="test-model", messages=[ChatMessage(role="system", content=prompt)])
    assert len(requests) == 1
    assert result.action.action == "pass"


@pytest.mark.asyncio
async def test_invalid_run_participants_are_rejected_before_creating_run(tmp_path: Path) -> None:
    app = create_app(database_url=f"sqlite:///{tmp_path / 'board.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            thread = (await client.post("/api/threads", json={"title": "Participants", "body": "Hello"})).json()
            for agent_ids, status in (([], 422), (["unknown"], 404)):
                response = await client.post("/api/runs", json={"thread_id": thread["thread_id"], "agent_ids": agent_ids})
                assert response.status_code == status
            assert not (await client.get("/api/state")).json()["runs"]


@pytest.mark.asyncio
async def test_mention_of_excluded_peer_does_not_strand_scoped_run(tmp_path: Path) -> None:
    app = create_app(database_url=f"sqlite:///{tmp_path / 'board.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            agents = (await client.get("/api/state")).json()["agents"]
            selected = next(agent for agent in agents if agent["handle"] == "wintermute")
            thread = (await client.post("/api/threads", json={
                "title": "Scoped mention", "body": "@mugwump raised this; what do the participants think?",
            })).json()
            run = (await client.post("/api/runs", json={
                "thread_id": thread["thread_id"], "agent_ids": [selected["id"]],
            })).json()
            with app.state.session_factory() as session:
                stimulus = session.scalar(select(Stimulus).where(Stimulus.run_id == run["id"]))
                assert stimulus.target_agent_id is None
                assert stimulus.kind == "unanswered_question"


@pytest.mark.asyncio
async def test_ui_reload_renames_existing_persona_and_preserves_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = persona_directory(tmp_path)
    monkeypatch.setenv("SWARMBOARD_PERSONA_DIR", str(directory))
    app = create_app(database_url=f"sqlite:///{tmp_path / 'board.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            old, _ = await register_persona(client, load_persona(directory), handle="persona1")
            await client.patch(f"/api/agents/{old['id']}", json={
                "model": "custom-model", "role": "personal assistant", "persona": "Added personality",
                "settings": {**old["settings"], "scheduler_role": "critic", "expertise": ["Added interests"]},
            })
            (directory / "memory.md").write_text("Updated preferences", encoding="utf-8")
            response = await client.post("/api/personas/ada/reload")
            assert response.status_code == 200
            ada = response.json()
            assert (ada["id"], ada["handle"], ada["model"]) == (old["id"], "ada", "custom-model")
            assert ada["settings"]["display_name"] == "Ada"
            assert ada["settings"]["persona_harness"]["memory"] == "Updated preferences"
            assert ada["role"] == "participant"
            assert ada["persona"] == "Personality and instructions come from AGENTS.md and memory.md."
            assert ada["settings"]["expertise"] == []
            assert "scheduler_role" not in ada["settings"]
            assert len((await client.get("/api/state")).json()["agents"]) == 7


@pytest.mark.asyncio
async def test_ui_start_is_autonomous_and_retry_does_not_duplicate_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = persona_directory(tmp_path)
    monkeypatch.setenv("SWARMBOARD_PERSONA_DIR", str(directory))
    gateway = ScriptedGateway(
        AgentAction(action="reply", body="@wintermute, challenge this idea.", intent="clarify"),
        AgentAction(action="reply", body="@ada, consider a smaller first step.", intent="support"),
        AgentAction(action="pass"),
    )
    app = create_app(database_url=f"sqlite:///{tmp_path / 'board.db'}", gateway=gateway)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            ada = (await client.post("/api/personas/ada/reload")).json()
            assert (ada["provider"], ada["model"]) == ("codex", "gpt-6-astra")
            await client.patch(f"/api/agents/{ada['id']}", json={"cooldown_seconds": 0})
            peers = (await client.get("/api/state")).json()["agents"]
            peer = next(item for item in peers if item["handle"] == "wintermute")
            payload = {
                "title": "Ada from the UI", "body": "Discuss a possibility with a peer.",
                "peer_ids": [peer["id"]], "idempotency_key": "ui-test", "continuous": True,
                "limits": {"max_rounds": 3, "max_posts": 4, "max_tokens": 100_000, "max_duration_seconds": 60},
            }
            first = await client.post("/api/personas/ada/sessions", json=payload)
            assert first.status_code == 201
            second = await client.post("/api/personas/ada/sessions", json=payload)
            assert first.json()["thread_id"] == second.json()["thread_id"]
            run_id = first.json()["run"]["id"]

            def completed():
                with app.state.session_factory() as session:
                    return session.get(Run, run_id).state == "completed"

            await wait_until(completed, timeout=5)
            assert len(gateway.calls) == 3
            state = (await client.get("/api/state")).json()
            assert len(state["threads"]) == len(state["runs"]) == 1
            assert state["runs"][0]["config"]["agent_ids"] == [ada["id"], peer["id"]]
            with app.state.session_factory() as session:
                ada_turn = session.scalar(select(Turn).where(Turn.agent_id == ada["id"]))
                assert ada_turn.provider == "codex" and ada_turn.model == "gpt-6-astra"
                assert ada_turn.sampling_settings == {"reasoning_effort": "medium"}
            assert (await client.post("/api/personas/ada/sessions", json={**payload, "body": "Changed task"})).status_code == 409
            page = (await client.get("/")).text
            assert 'id="start-ada-button"' in page and 'id="ada-form"' in page


@pytest.mark.asyncio
async def test_ui_invalid_peer_leaves_no_partial_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = persona_directory(tmp_path)
    monkeypatch.setenv("SWARMBOARD_PERSONA_DIR", str(directory))
    app = create_app(database_url=f"sqlite:///{tmp_path / 'board.db'}", gateway=ScriptedGateway())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.post("/api/personas/ada/sessions", json={
                "title": "Invalid", "body": "Hello", "peer_ids": ["missing"], "limits": {}, "idempotency_key": "invalid",
            })
            assert result.status_code == 404
            state = (await client.get("/api/state")).json()
            assert not state["runs"] and not state["threads"] and len(state["agents"]) == 6
