"""Opt-in, once-per-ID runtime verification without creating board history."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .codex_gateway import CodexGateway, safe_codex_diagnostic
from .gateways import GatewayError, StructuredOutputError


_ERROR_MESSAGES = {
    "authentication": "Codex startup check could not authenticate; check the server API key and model access.",
    "configuration": "Codex startup check configuration or private marker storage is invalid.",
    "timeout": "Codex startup check timed out.",
    "rate_limit": "Codex startup check reached the provider rate limit.",
    "quota": "Codex startup check reached the provider quota.",
    "safety_block": "Codex startup check was blocked by the provider.",
    "structured_output": "Codex startup check did not return a valid pass action.",
    "provider_error": "Codex startup check failed; check provider availability and model access.",
}
_VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\Z")


def _safe_record(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GatewayError(_ERROR_MESSAGES["configuration"], category="configuration")
    mode = value.get("auth_mode")
    version = value.get("version")
    tokens = value.get("token_count")
    if (value.get("status") != "passed" or mode not in {"api_key", "local"}
            or (version is not None and (not isinstance(version, str) or len(version) > 64 or not _VERSION.fullmatch(version)))
            or type(tokens) is not int or tokens < 0):
        raise GatewayError(_ERROR_MESSAGES["configuration"], category="configuration")
    return {"status": "passed", "auth_mode": mode, "version": version, "token_count": tokens}


def run_codex_preflight(database: Path) -> dict[str, Any] | None:
    """Run before app initialization, while the hosted process holds its storage lock."""
    check_id = os.getenv("SWARMBOARD_CODEX_PREFLIGHT_ID", "")
    if not check_id:
        return None
    directory = database.parent / ".runtime-checks"
    marker = directory / (hashlib.sha256(check_id.encode("utf-8")).hexdigest() + ".json")
    diagnostic = None
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        if marker.exists():
            marker.chmod(0o600)
            saved = _safe_record(json.loads(marker.read_text(encoding="utf-8")))
            print(json.dumps({**saved, "status": "already_passed"}), flush=True)
            return saved

        result = asyncio.run(CodexGateway(timeout_seconds=90).complete(
            model="gpt-6-astra", sampling={"reasoning_effort": "medium"},
            messages=[
                {"role": "system", "content": (
                    "You are performing a minimal application startup check. Return exactly one JSON action "
                    "with action set to pass and parent_post_id, title, body, and intent all null."
                )},
                {"role": "user", "content": "Return the pass action now. No other task is requested."},
            ],
        ))
        if result.action.action != "pass":
            raise GatewayError(_ERROR_MESSAGES["structured_output"], category="structured_output")
        record = _safe_record({
            "status": "passed", "auth_mode": result.response_metadata.get("auth_mode"),
            "version": result.response_metadata.get("configured_cli_version"),
            "token_count": result.usage.total_tokens,
        })
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".pending-", suffix=".json", delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, marker)
            temporary = None
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        print(json.dumps(record), flush=True)
        return record
    except GatewayError as exc:
        category = exc.category if exc.category in _ERROR_MESSAGES else "provider_error"
        diagnostic = safe_codex_diagnostic(exc)
    except StructuredOutputError:
        category = "structured_output"
    except (OSError, ValueError, TypeError):
        category = "configuration"
    message = _ERROR_MESSAGES[category]
    print(json.dumps({"status": "failed", "category": category, "diagnostic": diagnostic, "message": message}), flush=True)
    raise GatewayError(message, category=category) from None
