from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_HEADERS = {
    "authorization": "Authorization",
    "proxy-authorization": "Proxy-Authorization",
    "x-api-key": "X-API-Key",
    "api-key": "API-Key",
    "x-goog-api-key": "X-Goog-Api-Key",
    "cookie": "Cookie",
}
_HOSTED_PROVIDERS = {
    "openrouter.ai": ("OPENROUTER_API_KEY", {"/api", "/api/v1", "/api/v1/chat/completions"}),
    "api.openai.com": ("OPENAI_API_KEY", {"", "/v1", "/v1/chat/completions"}),
    "api.x.ai": ("XAI_API_KEY", {"", "/v1", "/v1/chat/completions"}),
}
_HOSTED_HEADERS = frozenset({"http-referer", "x-title"})


def _is_literal_api_key(name: object) -> bool:
    normalized = str(name).strip().casefold().replace("_", "").replace("-", "")
    return normalized == "apikey"


def scrub_agent_settings(settings: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Remove persisted credential values while retaining benign provider settings.

    The returned field labels are canonical and safe to place in an audit event;
    values are never included.
    """

    cleaned = dict(settings or {})
    removed: list[str] = []
    for key in list(cleaned):
        if _is_literal_api_key(key):
            cleaned.pop(key, None)
            if "settings.api_key" not in removed:
                removed.append("settings.api_key")

    headers = cleaned.get("headers")
    if isinstance(headers, Mapping):
        safe_headers = dict(headers)
        for key in list(safe_headers):
            canonical = _SENSITIVE_HEADERS.get(str(key).strip().casefold())
            if canonical is not None:
                safe_headers.pop(key, None)
                label = f"settings.headers.{canonical}"
                if label not in removed:
                    removed.append(label)
        cleaned["headers"] = safe_headers
    return cleaned, removed


def validate_agent_settings(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the env-name-only credential contract used by API writes."""

    original = dict(settings or {})
    _cleaned, removed = scrub_agent_settings(original)
    if removed:
        raise ValueError(
            "literal credentials cannot be stored in agent settings; use api_key_env"
        )
    api_key_env = original.get("api_key_env")
    if api_key_env not in (None, ""):
        if not isinstance(api_key_env, str) or _ENV_NAME_RE.fullmatch(api_key_env) is None:
            raise ValueError("api_key_env must be a valid environment-variable name")
    return original


def validate_hosted_provider(provider: str, settings: Mapping[str, Any] | None) -> None:
    """Bind shared-board credentials to fixed destinations, including saved agents.

    Unauthenticated local boards retain configurable gateways. Hosting or configuring
    login enables restrictions, including when a deployment omits its hosted flag.
    Validate the complete provider/settings pair before resolving any credential.
    """
    # Check only the presence of login configuration; never read its secret value here.
    if not (any(os.getenv(name, "").strip().lower() in {"1", "true", "yes"}
                for name in ("SWARMBOARD_HOSTED", "RENDER", "SWARMBOARD_REQUIRE_AUTH"))
            or "SWARMBOARD_AUTH_USERS" in os.environ):
        return
    if provider.lower() not in {"codex", "openai_compatible"}:
        raise ValueError("hosted agents must use codex or openai_compatible")
    values = validate_agent_settings(settings)
    if provider.lower() == "codex":
        return
    # URL validation alone does not bind HTTP authority when a saved Host header
    # can override it. Permit only display metadata, never routing or framing headers.
    headers = values.get("headers")
    if headers is not None and (
        not isinstance(headers, Mapping)
        or any(
            not isinstance(name, str) or name.lower() not in _HOSTED_HEADERS
            or not isinstance(value, str) or any(ord(char) < 32 or ord(char) > 126 for char in value)
            for name, value in headers.items()
        )
    ):
        raise ValueError("hosted provider headers may contain only HTTP-Referer and X-Title with printable ASCII values")
    base_url = values.get("base_url") or os.getenv("OPENAI_COMPAT_BASE_URL") or "https://api.openai.com"
    destination_error = "hosted provider URL must use an approved HTTPS provider endpoint"
    if (not isinstance(base_url, str) or any(char.isspace() or ord(char) < 32 for char in base_url)
            or any(char in base_url for char in ("?", "#", "\\"))):
        raise ValueError(destination_error)
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        valid_origin = (parsed.scheme == "https" and host in _HOSTED_PROVIDERS
                        and parsed.netloc.lower() in {host, f"{host}:443"})
    except ValueError:
        raise ValueError(destination_error) from None
    if not valid_origin:
        raise ValueError(destination_error)
    key_name, allowed_paths = _HOSTED_PROVIDERS[host]
    path = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path
    if path not in allowed_paths:
        raise ValueError(destination_error)
    if values.get("api_key_env") != key_name:
        raise ValueError("hosted provider must use its matching API key environment variable")


__all__ = ["scrub_agent_settings", "validate_agent_settings", "validate_hosted_provider"]
