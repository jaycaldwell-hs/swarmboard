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
_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "remote_plugin",
    "multi_agent", "multi_agent_v2", "memories", "hooks", "goals",
    "browser_use", "browser_use_external", "computer_use", "in_app_browser",
    "image_generation", "view_image", "skill_search", "tool_suggest",
    "code_mode", "code_mode_host", "sleep_tool",
)
_CLI_ARGUMENTS = {
    "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--sandbox",
    "--model", "--json", "--color", "--cd", "--output-schema",
    "--output-last-message", "--enable", "--disable",
}
_SAFE_DIAGNOSTICS = frozenset({
    "unsupported_feature", "unsupported_argument", "runtime_dependency",
    "invalid_configuration", "api_authentication", "model_access", "runtime_network",
    "runtime_permissions", "runtime_resources", "no_structured_events",
    "upstream_authentication", "upstream_billing", "upstream_rate_limit",
    "upstream_timeout", "upstream_safety_block",
    *(f"unsupported_feature:{feature}" for feature in ("skip_host_skill_discovery", *_DISABLED_FEATURES)),
    *(f"unsupported_argument:{argument}" for argument in _CLI_ARGUMENTS),
})


def safe_codex_diagnostic(error: BaseException) -> str | None:
    """Return only an exact known diagnostic label, safe for startup status logs."""
    value = getattr(error, "diagnostic", None)
    return value if isinstance(value, str) and value in _SAFE_DIAGNOSTICS else None


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


def _stderr_failure(stderr: bytes) -> tuple[str, str, str] | None:
    """Map CLI startup diagnostics to fixed labels; never return upstream text."""
    diagnostic = stderr.decode("utf-8", errors="replace").lower()
    if not diagnostic.strip():
        return None
    if "unknown feature" in diagnostic or "unrecognized feature" in diagnostic:
        # Only names supplied by this module are eligible for the output. This
        # helps identify a platform/build mismatch without echoing arbitrary text.
        feature = next((name for name in ("skip_host_skill_discovery", *_DISABLED_FEATURES)
                        if re.search(rf"\b{re.escape(name)}\b", diagnostic)), None)
        label = "unsupported_feature" + (f":{feature}" if feature else "")
        return "configuration", label, "the installed Codex build does not support a configured feature"
    if "unexpected argument" in diagnostic or "unrecognized argument" in diagnostic or "unknown option" in diagnostic:
        argument = next((name for name in sorted(_CLI_ARGUMENTS) if name in diagnostic), None)
        label = "unsupported_argument" + (f":{argument}" if argument else "")
        return "configuration", label, "the installed Codex build does not support a configured CLI argument"
    if any(value in diagnostic for value in (
        "error while loading shared libraries", "cannot find module", "exec format error",
        "glibc_", "glibcxx_", "unsupported platform", "unsupported architecture",
    )):
        return "configuration", "runtime_dependency", "check the installed Codex binary, Node runtime, and system libraries"
    if any(value in diagnostic for value in (
        "error parsing configuration", "error loading configuration", "failed to load config",
        "error parsing config", "unknown variant", "unknown field", "invalid value",
    )):
        return "configuration", "invalid_configuration", "the installed Codex build rejected a runtime configuration value"
    if any(value in diagnostic for value in ("invalid_api_key", "incorrect api key", "missing api key", "authentication required")):
        return "authentication", "api_authentication", "check the server API key and model access"
    if any(value in diagnostic for value in ("model_not_found", "model does not exist", "does not have access to model")):
        return "provider_error", "model_access", "check API-project access to the requested model"
    category = _failure_category([{"type": "error", "message": diagnostic}])
    if category != "provider_error":
        return category, f"upstream_{category}", "check API-project access, usage limits, and provider status"
    if any(value in diagnostic for value in (
        "network is unreachable", "failed to lookup address", "name or service not known",
        "connection refused", "error sending request", "certificate verify failed",
        "invalid peer certificate", "dns error",
    )):
        return "provider_error", "runtime_network", "check outbound network access and TLS certificates for the Codex process"
    if any(value in diagnostic for value in (
        "permission denied", "read-only file system", "operation not permitted",
        "os error 13", "os error 30",
    )):
        return "configuration", "runtime_permissions", "check ownership and write access for the Codex home and temporary directories"
    if any(value in diagnostic for value in ("no space left on device", "out of memory", "cannot allocate memory")):
        return "configuration", "runtime_resources", "check available storage and memory for the Codex process"
    return None


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
            for feature in _DISABLED_FEATURES:
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
                stdout, stderr = await asyncio.wait_for(
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
                diagnostic = _stderr_failure(stderr) if category == "provider_error" else None
                label = ""
                if diagnostic is not None:
                    category, label, guidance = diagnostic
                elif not events:
                    label = "no_structured_events"
                detail = f", {label}" if label else ""
                failure = GatewayError(f"Codex turn failed (exit {process.returncode}, {category}{detail}); {guidance}", category=category)
                failure.diagnostic = label or None
                raise failure
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
