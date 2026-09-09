"""Codex CLI inference with explicit hosted or saved local authentication."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from .gateways import (
    ChatMessage, GatewayError, GatewayResult, StructuredOutputError, TokenUsage,
    _messages_payload, action_json_schema, parse_agent_action, provider_error_category,
)


# Give the inference process the OS settings it needs, rather than all of the
# web server's secrets (board passwords, peer-provider keys, Render tokens).
_PROCESS_ENVIRONMENT = frozenset({
    "PATH", "HOME", "CODEX_HOME", "USER", "LOGNAME", "SHELL",
    "TMPDIR", "TMP", "TEMP", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
    "LC_COLLATE", "LC_MESSAGES", "LC_MONETARY", "LC_NUMERIC", "LC_TIME", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_CA_CERTIFICATE",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA",
})


def _runtime_environment() -> tuple[str, dict[str, str]]:
    mode = os.getenv("SWARMBOARD_CODEX_AUTH", "local").strip().lower()
    if mode not in {"local", "api_key"}:
        raise GatewayError(
            "SWARMBOARD_CODEX_AUTH must be local or api_key", category="configuration",
        )
    environment = {name: value for name, value in os.environ.items() if name in _PROCESS_ENVIRONMENT}
    if mode == "api_key":
        key = (os.getenv("SWARMBOARD_CODEX_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
        if not key or any(char in key for char in ("\x00", "\n", "\r")):
            raise GatewayError(
                "Codex API authentication requires SWARMBOARD_CODEX_API_KEY or OPENAI_API_KEY",
                category="authentication",
            )
        # This automation-only variable takes effect for this invocation, without
        # logging in or storing credentials on disk. Ambient keys are excluded.
        environment["CODEX_API_KEY"] = key
    return mode, environment


def validate_codex_configuration() -> None:
    """Fail early for an invalid auth mode or absent hosted key, without inference."""
    _runtime_environment()


def _failure_category(events: Sequence[dict[str, Any]]) -> str:
    category = "provider_error"
    for event in events:
        if event.get("type") not in {"error", "turn.failed"}:
            continue
        error = event.get("error", event)
        if not isinstance(error, dict):
            continue
        message = str(error.get("message", ""))
        status = error.get("status_code")
        # Codex transport failures can report HTTP status in the JSON message
        # instead of a status_code field. Classify it without retaining text.
        if status is None:
            match = re.search(r"\b(?:HTTP(?:/\d(?:\.\d)?)?|status(?: code)?)\s*[:=]?\s*(\d{3})\b", message, re.IGNORECASE)
            status = int(match[1]) if match else None
        elif isinstance(status, str) and status.isdigit():
            status = int(status)
        found = provider_error_category(error.get("code") or error.get("type"), status, message)
        if category != "safety_block" and found != "provider_error":
            category = found
    return category


class CodexGateway:
    """Run one ephemeral, read-only Codex turn with the board action schema.

    Local turns reuse saved CLI authentication; hosted turns use a server key.
    Each turn gets a fresh temporary working
    directory and the captured board system prompt as its instruction file.
    Host integrations and execution tools are disabled for board inference.
    """

    provider = "codex"

    def __init__(self, *, timeout_seconds: float = 180.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def complete(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage | Mapping[str, str]],
        sampling: Mapping[str, Any] | None = None,
        seed: int | None = None,
    ) -> GatewayResult:
        auth_mode, environment = _runtime_environment()
        payload = _messages_payload(messages)
        options = dict(sampling or {})
        effort = options.get("reasoning_effort", "medium")
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise GatewayError(f"unsupported Codex reasoning effort: {effort}")
        system = "\n\n".join(item["content"] for item in payload if item["role"] == "system")
        conversation = [item for item in payload if item["role"] != "system"]
        if not system or not conversation:
            raise GatewayError("Codex requires system instructions and discussion context")
        # The engine supplies one user context; retain roles for retry history.
        prompt = conversation[0]["content"] if len(conversation) == 1 and conversation[0]["role"] == "user" else json.dumps(conversation, ensure_ascii=False)
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="swarmboard-codex-") as directory:
            root = Path(directory)
            instructions = root / "instructions.md"
            schema = root / "action.schema.json"
            output = root / "action.json"
            instructions.write_bytes(system.encode("utf-8"))
            schema.write_text(json.dumps(action_json_schema()), encoding="utf-8")
            command = [
                os.getenv("SWARMBOARD_CODEX_BIN", "codex"), "exec",
                "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--model", model, "--json", "--color", "never",
                "--cd", directory, "--output-schema", str(schema),
                "--output-last-message", str(output),
                "-c", f"model_instructions_file={json.dumps(str(instructions))}",
                "-c", f"model_reasoning_effort={json.dumps(effort)}",
                "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                "-c", "project_doc_max_bytes=0", "-c", "skills.max_context_tokens=1",
                "--enable", "skip_host_skill_discovery",
            ]
            if auth_mode == "api_key":
                command.extend([
                    "-c", 'forced_login_method="api"',
                    "-c", 'cli_auth_credentials_store="ephemeral"',
                ])
            for feature in (
                "shell_tool", "unified_exec", "apps", "plugins", "remote_plugin",
                "multi_agent", "multi_agent_v2", "memories", "hooks", "goals",
                "browser_use", "browser_use_external", "computer_use", "in_app_browser",
                "image_generation", "view_image", "skill_search", "tool_suggest",
                "code_mode", "code_mode_host", "sleep_tool",
            ):
                command.extend(["--disable", feature])
            command.append("-")
            try:
                process = await asyncio.create_subprocess_exec(
                    *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, start_new_session=True, env=environment,
                )
            except OSError as exc:
                raise GatewayError("Cannot start Codex; install the CLI or check SWARMBOARD_CODEX_BIN", category="configuration") from exc
            try:
                stdout, _stderr = await asyncio.wait_for(
                    process.communicate(prompt.encode("utf-8")), timeout=self.timeout_seconds,
                )
            except (TimeoutError, asyncio.CancelledError) as exc:
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await process.communicate()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise GatewayError("Codex model call timed out", retryable=True, category="timeout") from exc

            events = []
            for line in stdout.decode("utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
            completed = next((event for event in reversed(events) if event.get("type") == "turn.completed"), None)
            if process.returncode or completed is None:
                # Classify known structured errors without persisting raw CLI diagnostics.
                category = _failure_category(events)
                guidance = "check the server API key and model access" if auth_mode == "api_key" else "check codex login status and model access"
                raise GatewayError(f"Codex turn failed (exit {process.returncode}, {category}); {guidance}", category=category)
            usage_data = completed.get("usage") or {}
            input_tokens = int(usage_data.get("input_tokens") or 0)
            output_tokens = int(usage_data.get("output_tokens") or 0)
            usage = TokenUsage(input_tokens, output_tokens, input_tokens + output_tokens)
            latency_ms = round((time.perf_counter() - started) * 1000)
            try:
                raw = output.read_text(encoding="utf-8")
            except OSError as exc:
                raise GatewayError("Codex completed without a final action") from exc
            try:
                action = parse_agent_action(raw)
            except StructuredOutputError as exc:
                exc.raw_output, exc.usage, exc.latency_ms = raw, usage, latency_ms
                raise
            return GatewayResult(
                action=action, raw_output=raw, provider=self.provider, model=model,
                latency_ms=latency_ms, usage=usage,
                response_metadata={
                    "transport": "codex exec", "sandbox": "read-only",
                    "auth_mode": auth_mode,
                    "configured_cli_version": os.getenv("SWARMBOARD_CODEX_VERSION"),
                    "reasoning_effort": effort,
                    "thread_id": next((event.get("thread_id") for event in events if event.get("type") == "thread.started"), None),
                    "cached_input_tokens": int(usage_data.get("cached_input_tokens") or 0),
                    "seed_supported": False, "max_output_tokens_enforced": False,
                },
            )
