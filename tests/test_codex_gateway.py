from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from swarmboard import codex_gateway
from swarmboard.codex_gateway import CodexGateway, validate_codex_configuration
from swarmboard.gateways import AgentAction, GatewayError, ModelGateway, StructuredOutputError


MESSAGES = [
    {"role": "system", "content": "A persona: café.\r\nPreserve whitespace.  \n"},
    {"role": "user", "content": "Captured board context, including `literal` $(text)."},
]


@pytest.fixture(autouse=True)
def isolate_codex_auth(monkeypatch):
    for name in ("SWARMBOARD_CODEX_AUTH", "SWARMBOARD_CODEX_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "SWARMBOARD_CODEX_VERSION"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_action", [True, False])
async def test_codex_dispatch_preserves_prompt_schema_and_accounts_usage(monkeypatch, valid_action):
    directory = None

    async def launch(*args, **kwargs):
        nonlocal directory
        directory = Path(args[args.index("--cd") + 1])
        assert (directory / "instructions.md").read_bytes() == MESSAGES[0]["content"].encode()
        schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
        assert schema["required"] == list(schema["properties"])
        assert not schema["additionalProperties"]
        assert args[args.index("--model") + 1] == "gpt-6-astra"
        assert "--ignore-user-config" in args and "--ephemeral" in args
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert "shell_tool" in args and 'web_search="disabled"' in args
        assert kwargs["start_new_session"] is True
        raw = AgentAction(action="pass").model_dump_json() if valid_action else '{"action":"reply"}'
        Path(args[args.index("--output-last-message") + 1]).write_text(raw)
        events = [
            {"type": "thread.started", "thread_id": "codex-test"},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "cached_input_tokens": 8, "output_tokens": 5}},
        ]

        async def communicate(prompt):
            assert prompt == MESSAGES[1]["content"].encode()
            return "\n".join(json.dumps(event) for event in events).encode(), b""

        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(codex_gateway.asyncio, "create_subprocess_exec", launch)
    agent = SimpleNamespace(provider="codex", model="gpt-6-astra", settings={})
    if valid_action:
        result = await ModelGateway().complete(agent, MESSAGES, seed=17)
        assert (result.provider, result.model, result.action.action) == ("codex", "gpt-6-astra", "pass")
        assert result.usage.total_tokens == 25
        assert result.response_metadata["cached_input_tokens"] == 8
        assert result.response_metadata["seed_supported"] is False
        assert result.response_metadata["max_output_tokens_enforced"] is False
        assert result.response_metadata["auth_mode"] == "local"
    else:
        with pytest.raises(StructuredOutputError) as error:
            await ModelGateway().complete(agent, MESSAGES)
        assert error.value.usage.total_tokens == 25
        assert error.value.raw_output == '{"action":"reply"}'
    assert directory is not None and not directory.exists()


@pytest.mark.asyncio
async def test_codex_failure_does_not_return_a_fabricated_action_or_raw_diagnostics(monkeypatch):
    process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b'{"type":"turn.failed"}\n', b"secret diagnostic")))
    monkeypatch.setattr(codex_gateway.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(GatewayError, match="Codex turn failed") as error:
        await CodexGateway().complete(model="gpt-6-astra", messages=MESSAGES)
    assert "secret diagnostic" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_kill_and_reap_codex(monkeypatch, cancel):
    entered = asyncio.Event()
    calls = 0

    async def communicate(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await asyncio.Event().wait()
        return b"", b""

    process = SimpleNamespace(returncode=None, pid=12345, communicate=communicate)
    killed = []
    monkeypatch.setattr(codex_gateway.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    monkeypatch.setattr(codex_gateway.os, "killpg", lambda pid, sig: killed.append(pid))
    task = asyncio.create_task(CodexGateway(timeout_seconds=0.02 if not cancel else 10).complete(model="gpt-6-astra", messages=MESSAGES))
    await entered.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else GatewayError):
        await task
    assert killed == [12345] and calls == 2


@pytest.mark.asyncio
async def test_missing_codex_cli_is_a_visible_configuration_error(monkeypatch):
    monkeypatch.setattr(codex_gateway.asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError))
    with pytest.raises(GatewayError, match="install the CLI"):
        await CodexGateway().complete(model="gpt-6-astra", messages=MESSAGES)


@pytest.mark.asyncio
@pytest.mark.parametrize("dedicated_key", [False, True])
async def test_hosted_codex_uses_only_server_key_without_persisting_auth(monkeypatch, dedicated_key):
    monkeypatch.setenv("SWARMBOARD_CODEX_AUTH", "api_key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-project-test")
    monkeypatch.setenv("CODEX_API_KEY", "sk-ambient-test")
    monkeypatch.setenv("SWARMBOARD_CODEX_VERSION", "0.153.4")
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", '{"admin":"secret"}')
    monkeypatch.setenv("ANTHROPIC_API_KEY", "peer-provider-secret")
    monkeypatch.setenv("RENDER_API_KEY", "deployment-secret")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unwanted-proxy.invalid")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "unwanted-chatgpt-token")
    monkeypatch.setenv("OPENAI_FEDERATION_RULE_ID", "unwanted-workload-identity")
    if dedicated_key:
        monkeypatch.setenv("SWARMBOARD_CODEX_API_KEY", "sk-dedicated-test")
    expected_key = "sk-dedicated-test" if dedicated_key else "sk-project-test"

    async def launch(*args, **kwargs):
        assert args[1] == "exec"
        assert 'forced_login_method="api"' in args
        assert 'cli_auth_credentials_store="ephemeral"' in args
        assert expected_key not in " ".join(args)
        environment = kwargs["env"]
        assert environment["CODEX_API_KEY"] == expected_key
        for name in (
            "SWARMBOARD_CODEX_API_KEY", "OPENAI_API_KEY", "SWARMBOARD_AUTH_USERS",
            "ANTHROPIC_API_KEY", "RENDER_API_KEY", "OPENAI_BASE_URL",
            "CODEX_ACCESS_TOKEN", "OPENAI_FEDERATION_RULE_ID",
        ):
            assert name not in environment
        assert environment["PATH"] == codex_gateway.os.environ["PATH"]
        assert environment["HOME"] == codex_gateway.os.environ["HOME"]
        Path(args[args.index("--output-last-message") + 1]).write_text(AgentAction(action="pass").model_dump_json())
        return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b'{"type":"turn.completed"}\n', b"")))

    monkeypatch.setattr(codex_gateway.asyncio, "create_subprocess_exec", launch)
    result = await CodexGateway().complete(model="gpt-6-astra", messages=MESSAGES)
    assert result.response_metadata["auth_mode"] == "api_key"
    assert result.response_metadata["configured_cli_version"] == "0.153.4"
    assert expected_key not in repr(result)
    assert codex_gateway.os.environ["CODEX_API_KEY"] == "sk-ambient-test"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, key, expected_category", [
    ("api_key", None, "authentication"),
    ("api_key", "   ", "authentication"),
    ("api_key", "invalid\nsecret", "authentication"),
    ("misspelled-auth-secret", "sk-test", "configuration"),
])
async def test_hosted_codex_configuration_fails_before_process_launch(monkeypatch, mode, key, expected_category):
    monkeypatch.setenv("SWARMBOARD_CODEX_AUTH", mode)
    monkeypatch.setenv("CODEX_API_KEY", "ambient-must-not-be-fallback")
    if key is not None:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    launch = AsyncMock()
    monkeypatch.setattr(codex_gateway.asyncio, "create_subprocess_exec", launch)
    with pytest.raises(GatewayError) as error:
        validate_codex_configuration()
    assert error.value.category == expected_category
    with pytest.raises(GatewayError) as error:
        await CodexGateway().complete(model="gpt-6-astra", messages=MESSAGES)
    assert error.value.category == expected_category
    assert "secret" not in str(error.value)
    launch.assert_not_called()


@pytest.mark.asyncio
async def test_local_codex_preserves_saved_login_without_ambient_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARMBOARD_CODEX_API_KEY", "sk-dedicated-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-project-test")
    monkeypatch.setenv("CODEX_API_KEY", "sk-ambient-test")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    auth_file = tmp_path / "auth.json"
    auth_file.write_text('{"saved":"unchanged"}')

    async def launch(*args, **kwargs):
        assert not any("forced_login_method" in arg or "cli_auth_credentials_store" in arg for arg in args)
        assert kwargs["env"]["CODEX_HOME"] == str(tmp_path)
        assert "CODEX_API_KEY" not in kwargs["env"]
        assert "OPENAI_API_KEY" not in kwargs["env"]
        Path(args[args.index("--output-last-message") + 1]).write_text(AgentAction(action="pass").model_dump_json())
        return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b'{"type":"turn.completed"}\n', b"")))

    monkeypatch.setattr(codex_gateway.asyncio, "create_subprocess_exec", launch)
    result = await CodexGateway().complete(model="gpt-6-astra", messages=MESSAGES)
    assert result.response_metadata["auth_mode"] == "local"
    assert auth_file.read_text() == '{"saved":"unchanged"}'


@pytest.mark.asyncio
@pytest.mark.parametrize("code, category", [("invalid_api_key", "authentication"), ("misalignment_policy_violation", "safety_block")])
async def test_hosted_failures_preserve_category_without_diagnostics_or_fallback(monkeypatch, code, category):
    monkeypatch.setenv("SWARMBOARD_CODEX_AUTH", "api_key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-echo-this")
    events = [{"type": "turn.failed", "error": {"code": code, "message": "sk-never-echo-this raw diagnostic"}}]
    process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(json.dumps(events[0]).encode(), b"private stderr")))
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(codex_gateway.asyncio, "create_subprocess_exec", launch)
    with pytest.raises(GatewayError) as error:
        await CodexGateway().complete(model="gpt-6-astra", messages=MESSAGES)
    assert error.value.category == category
    assert "server API key" in str(error.value)
    assert "sk-never-echo-this" not in str(error.value)
    assert "private stderr" not in str(error.value)
    assert "codex login" not in str(error.value)
    launch.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status, category", [(401, "authentication"), (429, "rate_limit"), (504, "timeout")])
async def test_codex_transport_status_survives_generic_final_failure(monkeypatch, status, category):
    events = [
        {"type": "error", "message": f"unexpected status {status}: sensitive diagnostics"},
        {"type": "turn.failed", "error": {"message": "retry exhausted"}},
    ]
    process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=("\n".join(json.dumps(event) for event in events).encode(), b"")))
    monkeypatch.setattr(codex_gateway.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(GatewayError) as error:
        await CodexGateway().complete(model="gpt-6-astra", messages=MESSAGES)
    assert error.value.category == category
    assert "sensitive diagnostics" not in str(error.value)
