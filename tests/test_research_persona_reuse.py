"""Exact resampling preserves captured files while honoring new model choices."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from swarmboard import codex_gateway, research, sessions
from swarmboard.engine import SwarmEngine
from swarmboard.gateways import AgentAction, GatewayResult, ModelGateway, OpenAICompatibleGateway
from swarmboard.models import Turn
from swarmboard.persona_context import PersonaSnapshot
from swarmboard.repository import Repository
from tests.test_engine_acceptance import make_database


@pytest.mark.asyncio
@pytest.mark.parametrize("captured_files", ["snapshot", "legacy", "none"])
@pytest.mark.parametrize("provider", ["codex", "openai_compatible"])
async def test_exact_resample_preserves_original_files_after_reload_and_model_change(
    monkeypatch, captured_files, provider,
):
    for name in ("SWARMBOARD_CODEX_AUTH", "SWARMBOARD_CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "planted-reuse-test-key")
    sql_engine, factory = make_database()
    engine = SwarmEngine(factory, gateway=ModelGateway())
    original_files = PersonaSnapshot(
        source="/private/planted/original-persona",
        instructions="\ufeffOriginal instructions.\r\n  ", memory="Original memory: café.\r\n\t",
    )
    newer_files = original_files.model_copy(update={
        "instructions": "New instructions after reload", "memory": "New memory after reload",
    })
    original_has_files = captured_files != "none"
    expected_files = original_has_files and captured_files != "legacy"
    calls = []
    try:
        with factory.begin() as session:
            repo = Repository(session)
            agent = repo.create_agent(
                handle="ada", persona="Original plain persona", provider="codex", model="gpt-6-astra",
                settings={"sampling": {"reasoning_effort": "low"},
                          **({"persona_harness": original_files.model_dump()} if original_has_files else {})},
                cooldown_seconds=0,
            )
            run = sessions.create_session(repo, agents=[agent], continuous=False,
                                          session_type="research", policy="permissive")
            thread = repo.list_threads(run_id=run.id)[0]
            posts = repo.list_posts(thread.id)
            stimulus = repo.list_stimuli(run_id=run.id)[0]
            context, messages, memory_ids = engine._build_context(
                session, run=run, thread=thread, posts=posts, stimulus=stimulus, agent=agent,
            )
            if captured_files == "legacy":
                context.pop("agent_snapshot")
            # Deliberate noncanonical whitespace proves reuse does not rebuild JSON.
            prompt = json.dumps([message.model_dump() for message in messages], ensure_ascii=False, indent=2) + "\r\n"
            original = repo.create_turn(
                run_id=run.id, thread_id=thread.id, agent_id=agent.id,
                context_post_ids=[post.id for post in posts], context_snapshot=context,
                prompt=prompt, prompt_version="captured-before-runtime-upgrade",
                provider=agent.provider, model=agent.model, retrieved_memory_ids=memory_ids,
            )
            repo.finish_turn(original.id, state="passed", raw_output=AgentAction(action="pass").model_dump_json())
            original_id, original_context, original_raw = original.id, deepcopy(original.context_snapshot), original.raw_output
            # This is the registered state at fork time, after a persona reload.
            agent.provider, agent.model, agent.persona = provider, "replacement-model", "New plain persona"
            agent.settings = {
                "persona_harness": newer_files.model_dump(), "sampling": {"reasoning_effort": "high"},
                **({"base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY"}
                   if provider == "openai_compatible" else {}),
            }
            session.flush()
            child = research.resample(repo, turn_id=original.id, author="operator", idempotency_key="reuse")["forks"][0]

        async def launch(*args, **kwargs):
            calls.append("codex")
            directory = Path(args[args.index("--cd") + 1])
            assert args[args.index("--model") + 1] == "replacement-model"
            assert 'model_reasoning_effort="high"' in args
            assert (directory / "instructions.md").read_bytes() == messages[0].content.encode()
            for name, content in (("AGENTS.md", original_files.instructions), ("memory.md", original_files.memory)):
                if expected_files:
                    assert (directory / name).read_bytes() == content.encode()
                else:
                    assert not (directory / name).exists()
            Path(args[args.index("--output-last-message") + 1]).write_text(AgentAction(action="pass").model_dump_json())

            async def communicate(payload):
                assert payload == messages[1].content.encode()
                return b'{"type":"turn.completed"}\n', b""

            return SimpleNamespace(returncode=0, communicate=communicate)

        async def peer_complete(self, *, model, messages: list, sampling=None, seed=None):
            calls.append("openai_compatible")
            assert model == "replacement-model" and sampling["reasoning_effort"] == "high"
            assert [message.model_dump() for message in messages] == json.loads(prompt)
            action = AgentAction(action="pass")
            return GatewayResult(action=action, raw_output=action.model_dump_json(), provider=provider, model=model, latency_ms=1)

        monkeypatch.setattr(codex_gateway.asyncio, "create_subprocess_exec", launch)
        monkeypatch.setattr(OpenAICompatibleGateway, "complete", peer_complete)
        await engine.step(child["run_id"])
        assert calls == [provider]
        with factory() as session:
            turn = session.scalar(select(Turn).where(Turn.run_id == child["run_id"]))
            assert turn.outcome == "passed", turn.error
            assert turn.prompt == prompt
            assert turn.prompt_version == "captured-before-runtime-upgrade"
            assert (turn.provider, turn.model) == (provider, "replacement-model")
            assert turn.sampling_settings["reasoning_effort"] == "high"
            configuration = turn.context_snapshot["agent_snapshot"]["configuration"]
            if captured_files != "legacy":
                assert configuration["persona"] == "Original plain persona"
            if expected_files:
                assert configuration["settings"]["persona_harness"] == original_files.model_dump()
                assert turn.context_snapshot["agent_snapshot"]["persona"] == original_context["agent_snapshot"]["persona"]
            else:
                assert "persona_harness" not in configuration["settings"]
            saved = session.get(Turn, original_id)
            assert (saved.prompt, saved.context_snapshot, saved.raw_output) == (prompt, original_context, original_raw)
    finally:
        await engine.shutdown()
        sql_engine.dispose()
