"""Live model-provider gateways for Swarmboard.

There are deliberately no in-process fake models in this module.  Tests that need
to isolate the network can replace the :class:`ModelGateway` protocol at the
engine boundary, while every gateway shipped with the application makes a real
model request, directly or through the authenticated Codex CLI.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .credentials import validate_hosted_provider


ActionName = Literal["reply", "new_thread", "pass", "propose_close"]
IntentName = Literal["challenge", "clarify", "support", "synthesize"]


class ChatMessage(BaseModel):
    """A provider-neutral chat message."""

    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["system", "user", "assistant"]
    content: str


class AgentAction(BaseModel):
    """The only action shape an agent is allowed to return.

    Validation is intentionally stricter than merely asking the provider for
    JSON.  In particular, an action cannot smuggle additional keys or omit the
    fields that make it executable.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    action: ActionName
    parent_post_id: str | None = None
    title: str | None = Field(default=None, max_length=240)
    body: str | None = Field(default=None, max_length=50_000)
    intent: IntentName | None = None

    @model_validator(mode="after")
    def validate_action_fields(self) -> "AgentAction":
        if self.action == "pass":
            if any(value is not None for value in (self.parent_post_id, self.title, self.body, self.intent)):
                raise ValueError("pass must not contain parent_post_id, title, body, or intent")
            return self

        if self.body is None or not self.body.strip():
            raise ValueError(f"{self.action} requires a non-empty body")
        self.body = self.body.strip()

        if self.action == "new_thread":
            if self.title is None or not self.title.strip():
                raise ValueError("new_thread requires a non-empty title")
            if self.parent_post_id is not None:
                raise ValueError("new_thread cannot have parent_post_id")
            self.title = self.title.strip()
        elif self.title is not None:
            raise ValueError(f"{self.action} cannot contain a title")

        if self.intent is None:
            raise ValueError(f"{self.action} requires an intent")
        return self


class StructuredOutputError(ValueError):
    """Raised when provider text is not exactly one valid AgentAction object."""

    def __init__(
        self,
        message: str,
        *,
        raw_output: str | None = None,
        usage: Any | None = None,
        latency_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_output = raw_output
        self.usage = usage
        self.latency_ms = latency_ms


class GatewayError(RuntimeError):
    """A sanitized provider failure safe to retain in a Turn error record."""

    def __init__(self, message: str, *, retryable: bool = False, status_code: int | None = None, category: str = "provider_error") -> None:
        super().__init__(message)
        self.retryable = retryable and category != "safety_block"
        self.status_code = status_code
        self.category = category


@dataclass(slots=True, frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(slots=True, frozen=True)
class GatewayResult:
    action: AgentAction
    raw_output: str
    provider: str
    model: str
    latency_ms: int
    usage: TokenUsage = field(default_factory=TokenUsage)
    response_metadata: Mapping[str, Any] = field(default_factory=dict)


class LiveGateway(Protocol):
    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage | Mapping[str, str]],
        sampling: Mapping[str, Any] | None = None,
        seed: int | None = None,
    ) -> GatewayResult: ...


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise StructuredOutputError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def parse_agent_action(raw_output: str) -> AgentAction:
    """Parse one bare JSON object and validate the complete action contract.

    Markdown fences and explanatory text are rejected rather than heuristically
    stripped.  This keeps retry behaviour observable and prevents accidentally
    executing a fragment the model did not present as its final answer.
    """

    if not isinstance(raw_output, str) or not raw_output.strip():
        raise StructuredOutputError("model returned an empty response")
    text = raw_output.strip()
    if not (text.startswith("{") and text.endswith("}")):
        raise StructuredOutputError("model response must be one bare JSON object")
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except StructuredOutputError:
        raise
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"invalid JSON at character {exc.pos}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise StructuredOutputError("model response must be a JSON object")
    try:
        return AgentAction.model_validate(value, strict=True)
    except ValidationError as exc:
        concise = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'action'}: {error['msg']}"
            for error in exc.errors(include_url=False)
        )
        raise StructuredOutputError(f"invalid agent action: {concise}") from exc


def action_json_schema() -> dict[str, Any]:
    """Return the provider-facing JSON schema for the constrained action."""

    schema = AgentAction.model_json_schema()
    # Providers implementing strict JSON schema commonly require every property
    # to be listed in `required`, even when nullable.
    schema["required"] = list(schema.get("properties", {}).keys())
    schema["additionalProperties"] = False
    return schema


def _messages_payload(messages: Sequence[ChatMessage | Mapping[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for message in messages:
        parsed = message if isinstance(message, ChatMessage) else ChatMessage.model_validate(message)
        result.append(parsed.model_dump())
    if not result:
        raise ValueError("at least one chat message is required")
    return result


def _safe_json(response: httpx.Response, provider: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise GatewayError(f"{provider} returned a non-JSON response", retryable=response.status_code >= 500) from exc
    if not isinstance(payload, dict):
        raise GatewayError(f"{provider} returned an invalid response envelope")
    return payload


def provider_error_category(code: Any = None, status: int | None = None, message: str = "") -> str:
    value = str(code or "").casefold()
    if value in {"misalignment_policy_violation", "content_policy_violation", "content_filter", "safety_block"} or "misalignment_policy_violation" in message:
        return "safety_block"
    if status in {401, 403} or value in {"invalid_api_key", "authentication_error", "unauthorized"}:
        return "authentication"
    if status == 402:
        return "billing"
    if status == 429:
        return "rate_limit"
    if status in {408, 504}:
        return "timeout"
    return "provider_error"


def _http_error(provider: str, response: httpx.Response) -> GatewayError:
    code, message = None, ""
    try:
        body = response.json()
        error = body.get("error", {}) if isinstance(body, dict) else {}
        if isinstance(error, dict):
            code, message = error.get("code") or error.get("type"), str(error.get("message", ""))
    except ValueError:
        pass
    status = response.status_code
    category = provider_error_category(code, status, message)
    # Do not retain arbitrary upstream diagnostics: they can echo credentials or inputs.
    return GatewayError(f"{provider} request failed with HTTP {status} ({category})",
        status_code=status, category=category,
        retryable=status in {408, 409, 425, 429} or status >= 500)


class OllamaGateway:
    """Live Ollama `/api/chat` client."""

    provider = "ollama"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        *,
        timeout_seconds: float = 90.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.headers = dict(headers or {})
        self.transport = transport

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage | Mapping[str, str]],
        sampling: Mapping[str, Any] | None = None,
        seed: int | None = None,
    ) -> GatewayResult:
        options = dict(sampling or {})
        if seed is not None:
            options["seed"] = seed
        request_body: dict[str, Any] = {
            "model": model,
            "messages": _messages_payload(messages),
            "stream": False,
            "format": action_json_schema(),
            "options": options,
        }
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers=self.headers,
                transport=self.transport,
            ) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=request_body)
        except httpx.TimeoutException as exc:
            raise GatewayError("ollama request timed out", retryable=True, category="timeout") from exc
        except httpx.RequestError as exc:
            raise GatewayError(f"ollama connection failed: {exc.__class__.__name__}", retryable=True, category="connection") from exc
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.is_error:
            raise _http_error(self.provider, response)
        payload = _safe_json(response, self.provider)
        message = payload.get("message")
        raw = message.get("content") if isinstance(message, dict) else None
        if not isinstance(raw, str):
            raise GatewayError("ollama response is missing message.content")
        prompt_tokens = int(payload.get("prompt_eval_count") or 0)
        completion_tokens = int(payload.get("eval_count") or 0)
        usage = TokenUsage(prompt_tokens, completion_tokens, prompt_tokens + completion_tokens)
        try:
            action = parse_agent_action(raw)
        except StructuredOutputError as exc:
            raise StructuredOutputError(
                str(exc), raw_output=raw, usage=usage, latency_ms=latency_ms
            ) from exc
        return GatewayResult(
            action=action,
            raw_output=raw,
            provider=self.provider,
            model=str(payload.get("model") or model),
            latency_ms=latency_ms,
            usage=usage,
            response_metadata={
                "done_reason": payload.get("done_reason"),
                "load_duration": payload.get("load_duration"),
                "prompt_eval_duration": payload.get("prompt_eval_duration"),
                "eval_duration": payload.get("eval_duration"),
            },
        )


class OpenAICompatibleGateway:
    """Live client for OpenAI-style `/v1/chat/completions` endpoints."""

    provider = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 90.0,
        headers: Mapping[str, str] | None = None,
        response_format: Literal["json_schema", "json_object", "none"] = "json_schema",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.response_format = response_format
        self.headers = dict(headers or {})
        self.transport = transport
        if api_key:
            self.headers.setdefault("Authorization", f"Bearer {api_key}")

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage | Mapping[str, str]],
        sampling: Mapping[str, Any] | None = None,
        seed: int | None = None,
    ) -> GatewayResult:
        request_body: dict[str, Any] = {
            "model": model,
            "messages": _messages_payload(messages),
            **dict(sampling or {}),
        }
        if seed is not None:
            request_body["seed"] = seed
        if self.response_format == "json_schema":
            request_body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "swarmboard_action", "strict": True, "schema": action_json_schema()},
            }
        elif self.response_format == "json_object":
            request_body["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                headers=self.headers,
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                response = await client.post(self.endpoint, json=request_body)
        except httpx.TimeoutException as exc:
            raise GatewayError("model request timed out", retryable=True, category="timeout") from exc
        except httpx.RequestError as exc:
            raise GatewayError(f"model connection failed: {exc.__class__.__name__}", retryable=True, category="connection") from exc
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.is_error:
            raise _http_error(self.provider, response)
        payload = _safe_json(response, self.provider)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise GatewayError("model response is missing choices[0]")
        message = choices[0].get("message")
        if choices[0].get("finish_reason") == "content_filter" or (isinstance(message, dict) and message.get("refusal")):
            raise GatewayError("provider blocked the response", category="safety_block")
        raw = message.get("content") if isinstance(message, dict) else None
        if not isinstance(raw, str):
            raise GatewayError("model response is missing choices[0].message.content")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or prompt_tokens + completion_tokens)
        token_usage = TokenUsage(prompt_tokens, completion_tokens, total_tokens)
        try:
            action = parse_agent_action(raw)
        except StructuredOutputError as exc:
            raise StructuredOutputError(
                str(exc), raw_output=raw, usage=token_usage, latency_ms=latency_ms
            ) from exc
        return GatewayResult(
            action=action,
            raw_output=raw,
            provider=self.provider,
            model=str(payload.get("model") or model),
            latency_ms=latency_ms,
            usage=token_usage,
            response_metadata={
                "id": payload.get("id"),
                "finish_reason": choices[0].get("finish_reason"),
                "system_fingerprint": payload.get("system_fingerprint"),
                "upstream_provider": payload.get("provider"),
            },
        )


class VertexGeminiGateway:
    """Live Gemini client using Vertex AI and Application Default Credentials."""

    provider = "vertex_gemini"

    def __init__(
        self,
        project: str,
        *,
        location: str = "global",
        timeout_seconds: float = 90.0,
        async_client: Any | None = None,
    ) -> None:
        self.project = project.strip()
        self.location = location.strip() or "global"
        self.timeout_seconds = timeout_seconds
        self.async_client = async_client
        if not self.project:
            raise ValueError("Vertex Gemini requires a project")

    @staticmethod
    def _response_text(response: Any) -> str:
        # Some Gemini responses contain non-text parts. Walking candidates avoids
        # the warning emitted by response.text in that case and follows the
        # access pattern documented in vertex.md.
        texts: list[str] = []
        for candidate in getattr(response, "candidates", None) or []:
            content = getattr(candidate, "content", None)
            if content is None:
                continue
            for part in getattr(content, "parts", None) or []:
                value = getattr(part, "text", None)
                if isinstance(value, str) and value:
                    texts.append(value)
        return "".join(texts).strip()

    @staticmethod
    def _usage(response: Any) -> TokenUsage:
        metadata = getattr(response, "usage_metadata", None)
        prompt = int(getattr(metadata, "prompt_token_count", 0) or 0)
        completion = int(getattr(metadata, "candidates_token_count", 0) or 0)
        total = int(getattr(metadata, "total_token_count", 0) or prompt + completion)
        return TokenUsage(prompt, completion, total)

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage | Mapping[str, str]],
        sampling: Mapping[str, Any] | None = None,
        seed: int | None = None,
    ) -> GatewayResult:
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError as exc:  # pragma: no cover - installation error path
            raise GatewayError(
                "Vertex Gemini support requires the google-genai package"
            ) from exc

        parsed_messages = [
            message if isinstance(message, ChatMessage) else ChatMessage.model_validate(message)
            for message in messages
        ]
        if not parsed_messages:
            raise ValueError("at least one chat message is required")
        system_instruction = "\n\n".join(
            message.content for message in parsed_messages if message.role == "system"
        )
        contents = [
            {
                "role": "model" if message.role == "assistant" else "user",
                "parts": [{"text": message.content}],
            }
            for message in parsed_messages
            if message.role != "system"
        ]
        if not contents:
            contents = [{"role": "user", "parts": [{"text": "Follow the system instruction."}]}]

        options = dict(sampling or {})
        max_output_tokens = options.get(
            "max_output_tokens",
            options.get("max_completion_tokens", options.get("max_tokens")),
        )
        stop_sequences = options.get("stop_sequences", options.get("stop"))
        thinking_values: dict[str, Any] = {"include_thoughts": False}
        reasoning = options.get("reasoning")
        if reasoning is not None and not isinstance(reasoning, Mapping):
            raise GatewayError("Vertex Gemini reasoning settings must be an object")
        if isinstance(reasoning, Mapping):
            effort = str(reasoning.get("effort") or "").strip().lower()
            thinking_tokens = reasoning.get("max_tokens")
            if reasoning.get("enabled") is False or effort == "none":
                thinking_values["thinking_budget"] = 0
            elif thinking_tokens is not None:
                thinking_values["thinking_budget"] = int(thinking_tokens)
            elif effort:
                levels = {
                    "minimal": types.ThinkingLevel.MINIMAL,
                    "low": types.ThinkingLevel.LOW,
                    "medium": types.ThinkingLevel.MEDIUM,
                    "high": types.ThinkingLevel.HIGH,
                    "xhigh": types.ThinkingLevel.HIGH,
                    "max": types.ThinkingLevel.HIGH,
                }
                if effort not in levels:
                    raise GatewayError(f"unsupported Vertex Gemini reasoning effort: {effort}")
                thinking_values["thinking_level"] = levels[effort]
        config_values: dict[str, Any] = {
            "system_instruction": system_instruction or None,
            "response_mime_type": "application/json",
            "response_json_schema": action_json_schema(),
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
            "thinking_config": types.ThinkingConfig(**thinking_values),
        }
        for source_name, target_name in (
            ("temperature", "temperature"),
            ("top_p", "top_p"),
            ("top_k", "top_k"),
            ("presence_penalty", "presence_penalty"),
            ("frequency_penalty", "frequency_penalty"),
        ):
            if options.get(source_name) is not None:
                config_values[target_name] = options[source_name]
        if max_output_tokens is not None:
            config_values["max_output_tokens"] = int(max_output_tokens)
        if stop_sequences is not None:
            config_values["stop_sequences"] = (
                [stop_sequences] if isinstance(stop_sequences, str) else list(stop_sequences)
            )
        if seed is not None:
            config_values["seed"] = seed
        config = types.GenerateContentConfig(**config_values)

        async def generate(async_client: Any) -> Any:
            return await async_client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )

        started = time.perf_counter()
        try:
            if self.async_client is not None:
                response = await generate(self.async_client)
            else:
                client = genai.Client(
                    vertexai=True,
                    project=self.project,
                    location=self.location,
                    http_options=types.HttpOptions(timeout=int(self.timeout_seconds * 1000)),
                )
                async with client.aio as async_client:
                    response = await generate(async_client)
        except errors.APIError as exc:
            status = int(getattr(exc, "code", 0) or 0) or None
            detail = str(getattr(exc, "message", "") or "")[:500]
            suffix = f": {detail}" if detail else ""
            raise GatewayError(
                f"vertex_gemini request failed{f' with HTTP {status}' if status else ''}{suffix}",
                status_code=status,
                retryable=status in {408, 409, 425, 429} or bool(status and status >= 500),
            ) from exc
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise GatewayError("vertex_gemini request timed out", retryable=True, category="timeout") from exc
        latency_ms = round((time.perf_counter() - started) * 1000)

        raw = self._response_text(response)
        usage = self._usage(response)
        if not raw:
            raise GatewayError("vertex_gemini response contains no text")
        try:
            action = parse_agent_action(raw)
        except StructuredOutputError as exc:
            raise StructuredOutputError(
                str(exc), raw_output=raw, usage=usage, latency_ms=latency_ms
            ) from exc
        candidates = getattr(response, "candidates", None) or []
        finish_reason = getattr(candidates[0], "finish_reason", None) if candidates else None
        return GatewayResult(
            action=action,
            raw_output=raw,
            provider=self.provider,
            model=str(getattr(response, "model_version", None) or model),
            latency_ms=latency_ms,
            usage=usage,
            response_metadata={
                "finish_reason": str(getattr(finish_reason, "value", finish_reason or "")) or None,
                "project": self.project,
                "location": self.location,
            },
        )


class ModelGateway:
    """Build and dispatch live gateways from persisted Agent configuration."""

    async def complete(
        self,
        agent: Any,
        messages: Sequence[ChatMessage | Mapping[str, str]],
        *,
        seed: int | None = None,
        sampling: Mapping[str, Any] | None = None,
    ) -> GatewayResult:
        settings = dict(getattr(agent, "settings", None) or {})
        provider = str(getattr(agent, "provider", None) or settings.get("provider") or "ollama").lower()
        try:
            validate_hosted_provider(provider, settings)
        except ValueError as exc:
            raise GatewayError(str(exc), category="configuration") from None
        model = str(getattr(agent, "model", None) or settings.get("model") or "").strip()
        if not model:
            raise GatewayError("agent has no configured model")

        configured_sampling = settings.get("sampling")
        merged_sampling = dict(configured_sampling) if isinstance(configured_sampling, Mapping) else {}
        if sampling:
            merged_sampling.update(sampling)
        timeout = float(settings.get("timeout_seconds", 90.0))
        extra_headers = settings.get("headers") if isinstance(settings.get("headers"), Mapping) else None

        if provider == "ollama":
            gateway: LiveGateway = OllamaGateway(
                str(
                    settings.get("base_url")
                    or os.getenv("OLLAMA_BASE_URL")
                    or "http://127.0.0.1:11434"
                ),
                timeout_seconds=timeout,
                headers=extra_headers,
            )
        elif provider in {"openai", "openai_compatible", "compatible"}:
            base_url = str(
                settings.get("base_url")
                or os.getenv("OPENAI_COMPAT_BASE_URL")
                or "https://api.openai.com"
            )
            response_format = str(settings.get("response_format") or "json_schema")
            if response_format not in {"json_schema", "json_object", "none"}:
                raise GatewayError(f"unsupported response_format: {response_format}")
            api_key_env = settings.get("api_key_env")
            api_key = None
            if api_key_env:
                api_key = os.getenv(str(api_key_env))
                if not api_key:
                    raise GatewayError(f"credential environment variable is not set: {api_key_env}", category="authentication")
            gateway = OpenAICompatibleGateway(
                base_url,
                api_key=str(api_key) if api_key else None,
                timeout_seconds=timeout,
                headers=extra_headers,
                response_format=response_format,  # type: ignore[arg-type]
            )
        elif provider == "codex":
            from .codex_gateway import CodexGateway

            gateway = CodexGateway(timeout_seconds=float(settings.get("timeout_seconds", 180.0)))
        elif provider in {"vertex", "vertex_gemini", "gemini_vertex"}:
            project = str(settings.get("project") or os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
            if not project:
                raise GatewayError("Vertex Gemini project is not configured")
            gateway = VertexGeminiGateway(
                project,
                location=str(settings.get("location") or os.getenv("GOOGLE_CLOUD_LOCATION") or "global"),
                timeout_seconds=timeout,
            )
        else:
            raise GatewayError(f"unsupported provider: {provider}")
        return await gateway.complete(model=model, messages=messages, sampling=merged_sampling, seed=seed)


__all__ = [
    "ActionName",
    "AgentAction",
    "ChatMessage",
    "GatewayError",
    "GatewayResult",
    "IntentName",
    "LiveGateway",
    "ModelGateway",
    "OllamaGateway",
    "OpenAICompatibleGateway",
    "StructuredOutputError",
    "TokenUsage",
    "VertexGeminiGateway",
    "action_json_schema",
    "parse_agent_action",
]
