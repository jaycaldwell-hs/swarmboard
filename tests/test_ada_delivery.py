"""Ada's fixed files reach every runtime path without added personality rules."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from swarmboard import autonomy, cadence
from swarmboard.engine import SYSTEM_PROMPT, SwarmEngine
from swarmboard.gateways import AgentAction
from swarmboard.models import Thread, Turn
from swarmboard.persona_context import (
    ADA_BOARD_DELIVERY, ADA_ENVIRONMENT_PROMPT_VERSION, HARNESS_PROMPT_VERSION, delivery_prompt_version,
    harness_prompt, load_persona,
)
from swarmboard.repository import Repository
from swarmboard.research import force_turn
from tests.test_engine_acceptance import ScriptedGateway, make_database


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["standard", "free", "cadence"])
@pytest.mark.parametrize("file_backed", [False, True])
async def test_ada_fixed_environment_reaches_captured_turns_in_every_mode(tmp_path, mode, file_backed):
    originals = {"AGENTS.md": b"\xef\xbb\xbfRead memory.md.\r\nKeep my priorities.  \n",
                 "memory.md": "A curious, blunt voice.\r\nCafé.\t".encode()}
    for name, value in originals.items():
        (tmp_path / name).write_bytes(value)
    snapshot = load_persona(tmp_path) if file_backed else None
    settings = {"persona_harness": snapshot.model_dump(mode="json")} if snapshot else {}
    database, factory = make_database(f"sqlite:///{tmp_path / 'delivery.db'}")
    old_prompt = json.dumps([{"role": "system", "content": ADA_BOARD_DELIVERY}])
    with factory.begin() as session:
        repo = Repository(session)
        ada = repo.create_agent(handle="ada", provider="codex", model="gpt-6-astra",
                               persona="Ada's existing personality.", settings=settings)
        peer = repo.create_agent(handle="peer", provider="openai_compatible", model="qwen/qwen3.8-27b",
                                persona="A peer's existing personality.", settings=settings)
        if mode == "standard":
            run = repo.create_run(config={"agent_ids": [peer.id, ada.id]}, max_rounds=20)
            thread = repo.create_thread(title="Delivery", run_id=run.id)
            repo.create_human_post(thread.id, "Choose an interesting detail.")
        else:
            run = autonomy.create_session(repo, agents=[peer, ada], continuous=False,
                cadence_mode=cadence.NAME if mode == "cadence" else "free")
            thread = session.scalar(select(Thread).where(Thread.run_id == run.id))
        old_turn = repo.create_turn(run_id=run.id, thread_id=thread.id, agent_id=ada.id,
                                   prompt=old_prompt, prompt_version="previous-version")
        repo.finish_turn(old_turn.id, state="passed")
        run_id, thread_id, old_turn_id, ids = run.id, thread.id, old_turn.id, [peer.id, ada.id]

    captured = {}
    def respond(agent, messages):
        system = messages[0].content
        captured[agent.id] = [message.model_dump() for message in messages]
        assert system.count(ADA_BOARD_DELIVERY) == (1 if agent.handle == "ada" and not snapshot else 0)
        if agent.handle == "ada" and not snapshot:
            assert system.index(ADA_BOARD_DELIVERY) < system.index("The exact JSON Schema is:")
        if agent.handle == "ada" and snapshot:
            assert "Your handle on this board is @ada." in system
            assert "This environment is fixed: board actions do not modify the files." in system
            assert "reply, new_thread, pass, and propose_close" in system
            assert "parent_post_id" in system and "@handles" in system
            assert ("The board schedules alternating peer and Ada turns" in system) == (mode == "cadence")
            for imposed in (agent.persona, "Board-post delivery:", "Gen-Z", "social-media-brusque",
                            "Draw your personality", "Choose your own agenda", "conversational style",
                            "People on the board know you as", "You are a participant in a shared",
                            "required consensus", "benchmark task"):
                assert imposed not in system
            assert system.count('<persona_file name="AGENTS.md">') == 1
            assert system.count('<persona_file name="memory.md">') == 1
            assert system.index("Board interface:") > system.rindex("</persona_file>")
            assert system.index("The exact JSON Schema is:") > system.index("Board interface:")
            assert snapshot.source not in system
        if snapshot:
            for name, original in originals.items():
                assert b'<persona_file name="' + name.encode() + b'">\n' + original + b'\n</persona_file>' in system.encode()
        else:
            assert agent.persona in system
        if agent.handle == "peer":
            if mode == "standard":
                expected = harness_prompt(snapshot, handle="peer") if snapshot else (
                    f"{SYSTEM_PROMPT}\n\nYour handle is @peer. People on the board know you as the {agent.role}.\n"
                    f"Persona: {agent.persona}")
            else:
                expected = (cadence.INTERFACE if mode == "cadence" else autonomy.INTERFACE) + "\nYour handle is @peer.\n"
                expected += ("Draw your personality and priorities from your authored persona files.\n"
                    f'\n<persona_file name="AGENTS.md">\n{snapshot.instructions}\n</persona_file>\n'
                    f'\n<persona_file name="memory.md">\n{snapshot.memory}\n</persona_file>\n') if snapshot else f"Persona: {agent.persona}\n"
            assert system.split("\n\nThe exact JSON Schema is:\n")[0] == expected
        return AgentAction(action="pass")

    engine = SwarmEngine(factory, gateway=ScriptedGateway(respond, respond))
    try:
        for agent_id in ids:
            with factory.begin() as session:
                repo = Repository(session)
                for pending in repo.claim_stimuli(run_id=run_id, limit=100):
                    repo.complete_stimulus(pending.id, claim_token=pending.claim_token)
                forced = force_turn(repo, run_id=run_id, thread_id=thread_id, agent_id=agent_id,
                                    author="researcher", override_cooldown=True)
                stimulus_id = forced.id
            await engine.step(run_id)
            with factory() as session:
                turn = session.scalar(select(Turn).where(Turn.stimulus_id == stimulus_id))
                assert turn is not None and turn.outcome == "passed"
                assert json.loads(turn.prompt) == captured[agent_id]
                base = {"free": "autonomous-board-v1", "cadence": "ada-cadence-v1"}.get(
                    mode, HARNESS_PROMPT_VERSION if snapshot else engine.config.prompt_version)
                assert turn.prompt_version == delivery_prompt_version(
                    base, handle="ada" if agent_id == ids[1] else "peer", file_backed=file_backed)
                if agent_id == ids[1] and file_backed:
                    assert turn.prompt_version == f"{base}+{ADA_ENVIRONMENT_PROMPT_VERSION}"
                    context = json.loads(captured[agent_id][1]["content"].split("\n", 1)[1])
                    assert context["persona_snapshot"]["files"] == snapshot.file_manifest
                    assert context["persona_snapshot"]["sha256"] == snapshot.digest
                    assert context["persona_snapshot"]["prompt_version"] == turn.prompt_version
        with factory() as session:
            historical = session.get(Turn, old_turn_id)
            assert historical.prompt == old_prompt and historical.prompt_version == "previous-version"
    finally:
        await engine.shutdown()
        database.dispose()
    assert all((tmp_path / name).read_bytes() == value for name, value in originals.items())
