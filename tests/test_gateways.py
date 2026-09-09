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
    OpenAICompatibleGateway,
    StructuredOutputError,
    TokenUsage,
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
async def test_openrouter_sends_action_schema_and_parses_usage() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "model": "qwen/qwen-test",
                "choices": [{"message": {"role": "assistant", "content": reply_json()},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 17, "completion_tokens": 8},
            },
        )

    gateway = OpenAICompatibleGateway(
        "https://openrouter.ai/api/v1/",
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
    assert request.url == httpx.URL("https://openrouter.ai/api/v1/chat/completions")
    payload = json.loads(request.content)
    assert payload["model"] == "qwen-test"
    assert payload["messages"] == [{"role": "system", "content": "Return one action."}]
    assert payload["temperature"] == 0.25 and payload["seed"] == 41
    schema = payload["response_format"]["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])

    assert result.action == AgentAction(
        action="reply",
        parent_post_id="post-1",
        body="A bounded reply.",
        intent="support",
    )
    assert result.model == "qwen/qwen-test"
    assert result.provider == "openai_compatible"
    assert result.usage == TokenUsage(prompt_tokens=17, completion_tokens=8, total_tokens=25)
    assert result.response_metadata["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_openrouter_rejects_invalid_json_without_fabricating_a_fallback() -> None:
    calls = 0
    raw = "I agree, but this is not the required JSON object."

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "model": "bad-output-model",
                "choices": [{"message": {"role": "assistant", "content": raw}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 11},
            },
        )

    gateway = OpenAICompatibleGateway("https://openrouter.ai/api/v1", transport=httpx.MockTransport(handler))

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

    gateway = OpenAICompatibleGateway(
        "https://openrouter.ai/api/v1",
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
        "https://openrouter.ai/api/v1/",
        api_key="router-secret",
        headers={"X-Title": "swarmboard-test"},
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
    assert request.url == httpx.URL("https://openrouter.ai/api/v1/chat/completions")
    assert request.headers["authorization"] == "Bearer router-secret"
    assert request.headers["x-title"] == "swarmboard-test"
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
async def test_openrouter_preserves_raw_text_and_reasoning_request_settings() -> None:
    raw = " \r\n" + reply_json("Checked without sounding formal.") + "\t\n"
    calls = []

    async def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "qwen/qwen-test", "provider": "upstream-test",
            "choices": [{"message": {"content": raw}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 31, "completion_tokens": 7, "total_tokens": 38},
        })

    result = await OpenAICompatibleGateway(
        "https://openrouter.ai/api/v1", transport=httpx.MockTransport(handler),
    ).complete(
        model="qwen/qwen-test",
        messages=[{"role": "system", "content": "Be a conversational evidence checker."},
                  {"role": "user", "content": "What does this thread support?"}],
        sampling={"temperature": 0.15, "max_tokens": 256,
                  "reasoning": {"effort": "low", "exclude": True}},
        seed=29,
    )
    assert len(calls) == 1
    assert calls[0]["reasoning"] == {"effort": "low", "exclude": True}
    assert calls[0]["max_tokens"] == 256 and calls[0]["seed"] == 29
    assert calls[0]["messages"][0]["role"] == "system"
    assert result.raw_output == raw
    assert result.action.body == "Checked without sounding formal."
    assert result.usage == TokenUsage(prompt_tokens=31, completion_tokens=7, total_tokens=38)
    assert result.response_metadata["upstream_provider"] == "upstream-test"


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
    monkeypatch.setenv("OPENROUTER_API_KEY", "first-live-key")
    agent = SimpleNamespace(
        provider="openai_compatible",
        model="vendor/model",
        settings={
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
        },
    )
    dispatcher = ModelGateway()

    await dispatcher.complete(agent, [{"role": "user", "content": "First call"}])
    monkeypatch.setenv("OPENROUTER_API_KEY", "rotated-live-key")
    await dispatcher.complete(agent, [{"role": "user", "content": "Second call"}])

    assert [instance.kwargs["api_key"] for instance in RecordingCompatibleGateway.instances] == [
        "first-live-key",
        "rotated-live-key",
    ]


@pytest.mark.asyncio
async def test_model_gateway_rejects_missing_key_and_keyless_local_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RecordingCompatibleGateway.instances.clear()
    monkeypatch.setattr(gateway_module, "OpenAICompatibleGateway", RecordingCompatibleGateway)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    dispatcher = ModelGateway()
    missing_key_agent = SimpleNamespace(
        provider="openai_compatible",
        model="vendor/model",
        settings={
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
        },
    )

    with pytest.raises(
        GatewayError,
        match="credential environment variable is not set: OPENROUTER_API_KEY",
    ):
        await dispatcher.complete(missing_key_agent, [{"role": "user", "content": "Call"}])
    assert RecordingCompatibleGateway.instances == []

    local_agent = SimpleNamespace(
        provider="openai_compatible",
        model="local/model",
        settings={"base_url": "http://127.0.0.1:9000"},
    )
    with pytest.raises(GatewayError, match="approved HTTPS provider"):
        await dispatcher.complete(local_agent, [{"role": "user", "content": "Local call"}])
    assert RecordingCompatibleGateway.instances == []


@pytest.mark.parametrize("provider", ["ollama", "vertex", "vertex_gemini", "gemini_vertex", "openai", "compatible"])
@pytest.mark.asyncio
async def test_model_gateway_rejects_retired_providers_before_dispatch(monkeypatch, provider) -> None:
    RecordingCompatibleGateway.instances.clear()
    monkeypatch.setattr(gateway_module, "OpenAICompatibleGateway", RecordingCompatibleGateway)
    agent = SimpleNamespace(provider=provider, model="retired-model", settings={})
    with pytest.raises(GatewayError, match="Codex/Astra or OpenRouter") as error:
        await ModelGateway().complete(agent, [{"role": "user", "content": "Do not call."}])
    assert error.value.category == "configuration"
    assert RecordingCompatibleGateway.instances == []
