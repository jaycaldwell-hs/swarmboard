from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import swarmboard.gateways as gateway_module
from swarmboard.gateways import (
    AgentAction,
    GatewayError,
    GatewayResult,
    ModelGateway,
    OllamaGateway,
    OpenAICompatibleGateway,
    StructuredOutputError,
    TokenUsage,
    VertexGeminiGateway,
)


def reply_json(body: str = "A bounded reply.") -> str:
    return json.dumps(
        {
            "action": "reply",
            "parent_post_id": "post-1",
            "title": None,
            "body": body,
            "intent": "support",
        }
    )


@pytest.mark.asyncio
async def test_ollama_sends_action_schema_and_parses_usage() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "qwen-test:latest",
                "message": {"role": "assistant", "content": reply_json()},
                "done_reason": "stop",
                "prompt_eval_count": 17,
                "eval_count": 8,
            },
        )

    gateway = OllamaGateway(
        "http://ollama.test/",
        transport=httpx.MockTransport(handler),
    )
    result = await gateway.complete(
        model="qwen-test",
        messages=[{"role": "system", "content": "Return one action."}],
        sampling={"temperature": 0.25},
        seed=41,
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.url == httpx.URL("http://ollama.test/api/chat")
    payload = json.loads(request.content)
    assert payload["model"] == "qwen-test"
    assert payload["messages"] == [{"role": "system", "content": "Return one action."}]
    assert payload["stream"] is False
    assert payload["options"] == {"temperature": 0.25, "seed": 41}
    assert payload["format"]["additionalProperties"] is False
    assert set(payload["format"]["required"]) == set(payload["format"]["properties"])

    assert result.action == AgentAction(
        action="reply",
        parent_post_id="post-1",
        body="A bounded reply.",
        intent="support",
    )
    assert result.model == "qwen-test:latest"
    assert result.provider == "ollama"
    assert result.usage == TokenUsage(prompt_tokens=17, completion_tokens=8, total_tokens=25)
    assert result.response_metadata["done_reason"] == "stop"


@pytest.mark.asyncio
async def test_ollama_rejects_invalid_json_without_fabricating_a_fallback() -> None:
    calls = 0
    raw = "I agree, but this is not the required JSON object."

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "bad-output-model",
                "message": {"role": "assistant", "content": raw},
                "prompt_eval_count": 5,
                "eval_count": 11,
            },
        )

    gateway = OllamaGateway(transport=httpx.MockTransport(handler))

    with pytest.raises(StructuredOutputError, match="one bare JSON object") as raised:
        await gateway.complete(
            model="bad-output-model",
            messages=[{"role": "user", "content": "Contribute or pass."}],
        )

    assert calls == 1
    assert raised.value.raw_output == raw
    assert raised.value.usage == TokenUsage(prompt_tokens=5, completion_tokens=11, total_tokens=16)
    assert raised.value.latency_ms is not None


@pytest.mark.asyncio
async def test_retryable_http_error_is_sanitized() -> None:
    secret = "secret-that-must-not-appear"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {secret}"
        return httpx.Response(503, json={"error": {"message": "provider temporarily overloaded"}})

    gateway = OllamaGateway(
        headers={"Authorization": f"Bearer {secret}"},
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(GatewayError) as raised:
        await gateway.complete(
            model="unavailable-model",
            messages=[{"role": "user", "content": "Try once."}],
        )

    error = raised.value
    assert error.status_code == 503
    assert error.retryable is True
    assert "provider_error" in str(error)
    assert "provider temporarily overloaded" not in str(error)
    assert secret not in str(error)
    assert "Authorization" not in str(error)


@pytest.mark.asyncio
async def test_openai_compatible_gateway_uses_endpoint_bearer_and_strict_schema() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "generation-1",
                "model": "vendor/model-v2",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": reply_json("Parsed strictly.")},
                    }
                ],
                "usage": {"prompt_tokens": 23, "completion_tokens": 9, "total_tokens": 32},
                "system_fingerprint": "fp_test",
            },
        )

    gateway = OpenAICompatibleGateway(
        "https://router.test/api/v1/",
        api_key="router-secret",
        headers={"X-Client": "swarmboard-test"},
        transport=httpx.MockTransport(handler),
    )
    result = await gateway.complete(
        model="vendor/model",
        messages=[{"role": "user", "content": "Return a useful action."}],
        sampling={"temperature": 0.1, "max_tokens": 200},
        seed=73,
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.url == httpx.URL("https://router.test/api/v1/chat/completions")
    assert request.headers["authorization"] == "Bearer router-secret"
    assert request.headers["x-client"] == "swarmboard-test"
    payload = json.loads(request.content)
    assert payload["model"] == "vendor/model"
    assert payload["temperature"] == 0.1
    assert payload["max_tokens"] == 200
    assert payload["seed"] == 73
    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "swarmboard_action"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["schema"]["additionalProperties"] is False

    assert result.action.body == "Parsed strictly."
    assert result.model == "vendor/model-v2"
    assert result.usage == TokenUsage(prompt_tokens=23, completion_tokens=9, total_tokens=32)
    assert result.response_metadata == {
        "id": "generation-1",
        "finish_reason": "stop",
        "system_fingerprint": "fp_test",
        "upstream_provider": None,
    }


@pytest.mark.asyncio
async def test_vertex_gemini_uses_structured_json_and_candidate_parts() -> None:
    calls: list[dict[str, Any]] = []

    class FakeModels:
        async def generate_content(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        content=SimpleNamespace(
                            parts=[
                                SimpleNamespace(text=reply_json("Checked without sounding formal."))
                            ]
                        ),
                        finish_reason=SimpleNamespace(value="STOP"),
                    )
                ],
                usage_metadata=SimpleNamespace(
                    prompt_token_count=31,
                    candidates_token_count=7,
                    total_token_count=38,
                ),
                model_version="gemini-test-001",
            )

    gateway = VertexGeminiGateway(
        "handshake-production",
        location="global",
        async_client=SimpleNamespace(models=FakeModels()),
    )
    result = await gateway.complete(
        model="gemini-3-pro-preview",
        messages=[
            {"role": "system", "content": "Be a conversational evidence checker."},
            {"role": "user", "content": "What does this thread actually support?"},
        ],
        sampling={
            "temperature": 0.15,
            "max_tokens": 256,
            "reasoning": {"effort": "low", "exclude": True},
        },
        seed=29,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == "gemini-3-pro-preview"
    assert call["contents"] == [
        {
            "role": "user",
            "parts": [{"text": "What does this thread actually support?"}],
        }
    ]
    config = call["config"]
    assert config.system_instruction == "Be a conversational evidence checker."
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema["additionalProperties"] is False
    assert config.max_output_tokens == 256
    assert config.thinking_config.include_thoughts is False
    assert config.thinking_config.thinking_level.value == "LOW"
    assert config.seed == 29
    assert result.action.body == "Checked without sounding formal."
    assert result.provider == "vertex_gemini"
    assert result.model == "gemini-test-001"
    assert result.usage == TokenUsage(prompt_tokens=31, completion_tokens=7, total_tokens=38)
    assert result.response_metadata == {
        "finish_reason": "STOP",
        "project": "handshake-production",
        "location": "global",
    }


class RecordingCompatibleGateway:
    instances: list["RecordingCompatibleGateway"] = []

    def __init__(self, base_url: str, **kwargs: Any) -> None:
        self.base_url = base_url
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    async def complete(self, *, model: str, messages: Any, sampling: Any, seed: int | None) -> GatewayResult:
        action = AgentAction(action="pass")
        return GatewayResult(
            action=action,
            raw_output=action.model_dump_json(),
            provider="recording-compatible",
            model=model,
            latency_ms=0,
        )


@pytest.mark.asyncio
async def test_model_gateway_resolves_named_environment_key_at_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RecordingCompatibleGateway.instances.clear()
    monkeypatch.setattr(gateway_module, "OpenAICompatibleGateway", RecordingCompatibleGateway)
    monkeypatch.setenv("SWARMBOARD_TEST_PROVIDER_KEY", "first-live-key")
    agent = SimpleNamespace(
        provider="openai_compatible",
        model="vendor/model",
        settings={
            "base_url": "https://provider.test/v1",
            "api_key_env": "SWARMBOARD_TEST_PROVIDER_KEY",
            "api_key": "persisted-key-must-be-ignored",
        },
    )
    dispatcher = ModelGateway()

    await dispatcher.complete(agent, [{"role": "user", "content": "First call"}])
    monkeypatch.setenv("SWARMBOARD_TEST_PROVIDER_KEY", "rotated-live-key")
    await dispatcher.complete(agent, [{"role": "user", "content": "Second call"}])

    assert [instance.kwargs["api_key"] for instance in RecordingCompatibleGateway.instances] == [
        "first-live-key",
        "rotated-live-key",
    ]
    assert all(
        instance.kwargs["api_key"] != "persisted-key-must-be-ignored"
        for instance in RecordingCompatibleGateway.instances
    )


@pytest.mark.asyncio
async def test_model_gateway_rejects_missing_named_key_but_allows_keyless_local_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RecordingCompatibleGateway.instances.clear()
    monkeypatch.setattr(gateway_module, "OpenAICompatibleGateway", RecordingCompatibleGateway)
    monkeypatch.delenv("SWARMBOARD_MISSING_PROVIDER_KEY", raising=False)
    dispatcher = ModelGateway()
    missing_key_agent = SimpleNamespace(
        provider="openai_compatible",
        model="vendor/model",
        settings={
            "base_url": "https://provider.test/v1",
            "api_key_env": "SWARMBOARD_MISSING_PROVIDER_KEY",
            "api_key": "legacy-persisted-key",
        },
    )

    with pytest.raises(
        GatewayError,
        match="credential environment variable is not set: SWARMBOARD_MISSING_PROVIDER_KEY",
    ):
        await dispatcher.complete(missing_key_agent, [{"role": "user", "content": "Call"}])
    assert RecordingCompatibleGateway.instances == []

    local_agent = SimpleNamespace(
        provider="openai_compatible",
        model="local/model",
        settings={"base_url": "http://127.0.0.1:9000", "api_key": "legacy-persisted-key"},
    )
    await dispatcher.complete(local_agent, [{"role": "user", "content": "Local call"}])

    assert len(RecordingCompatibleGateway.instances) == 1
    assert RecordingCompatibleGateway.instances[0].kwargs["api_key"] is None


class RecordingVertexGateway:
    instances: list["RecordingVertexGateway"] = []

    def __init__(self, project: str, **kwargs: Any) -> None:
        self.project = project
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    async def complete(self, *, model: str, messages: Any, sampling: Any, seed: int | None) -> GatewayResult:
        action = AgentAction(action="pass")
        return GatewayResult(
            action=action,
            raw_output=action.model_dump_json(),
            provider="vertex_gemini",
            model=model,
            latency_ms=0,
        )


@pytest.mark.asyncio
async def test_model_gateway_dispatches_vertex_project_and_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RecordingVertexGateway.instances.clear()
    monkeypatch.setattr(gateway_module, "VertexGeminiGateway", RecordingVertexGateway)
    agent = SimpleNamespace(
        provider="vertex_gemini",
        model="gemini-3-pro-preview",
        settings={
            "project": "handshake-production",
            "location": "global",
            "sampling": {"temperature": 0.15},
        },
    )

    result = await ModelGateway().complete(
        agent,
        [{"role": "user", "content": "Check this claim."}],
        seed=11,
    )

    assert result.provider == "vertex_gemini"
    assert len(RecordingVertexGateway.instances) == 1
    instance = RecordingVertexGateway.instances[0]
    assert instance.project == "handshake-production"
    assert instance.kwargs["location"] == "global"
    assert instance.kwargs["timeout_seconds"] == 90.0
