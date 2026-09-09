from __future__ import annotations

import os
import re
import json
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
}
_HOSTED_HEADERS = frozenset({"http-referer", "x-title"})


def redact(value: Any) -> Any:
    """Redact known credential values recursively at human-facing boundaries.

    Storage retains the original model artifact. Exports/HTTP responses may
    contain a redacted representation, including secrets embedded in free text.
    Environment-variable *names* remain useful and are never treated as secrets.
    """
    secrets: set[str] = set()
    for name, secret in os.environ.items():
        if re.search(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH_USERS)", name, re.I) and secret:
            secrets.add(secret)
            if name == "SWARMBOARD_AUTH_USERS":
                try:
                    users = json.loads(secret)
                    if isinstance(users, dict):
                        secrets.update(password for password in users.values() if isinstance(password, str) and password)
                except ValueError:
                    pass
    ordered = sorted(secrets, key=len, reverse=True)

    def clean(item):
        if isinstance(item, str):
            for secret in ordered:
                item = item.replace(secret, "[REDACTED]")
                escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
                if escaped != secret:
                    item = item.replace(escaped, "[REDACTED]")
            return item
        if isinstance(item, Mapping):
            result = {}
            for key, content in item.items():
                sensitive = _credential_field(key) is not None
                result[clean(str(key))] = "[REDACTED]" if sensitive and content is not None else clean(content)
            return result
        if isinstance(item, (list, tuple)):
            return [clean(child) for child in item]
        return item

    return clean(value)


def _is_literal_api_key(name: object) -> bool:
    normalized = str(name).strip().casefold().replace("_", "").replace("-", "")
    return normalized == "apikey"


def _credential_field(name: object) -> str | None:
    """Canonical audit labels for fields that hold credential values."""
    normalized = str(name).strip().casefold().replace("_", "").replace("-", "")
    if normalized == "apikey":
        return "api_key"
    header = _SENSITIVE_HEADERS.get(str(name).strip().casefold())
    if header:
        return header
    if normalized in {"password", "secret", "clientsecret", "accesstoken", "refreshtoken", "credential", "credentials", "token"}:
        return str(name)
    return None


def scrub_agent_settings(settings: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Remove persisted credential values while retaining benign provider settings.

    The returned field labels are canonical and safe to place in an audit event;
    values are never included.
    """

    removed: list[str] = []

    def clean(value, path):
        if isinstance(value, Mapping):
            output = {}
            for key, content in value.items():
                credential = _credential_field(key)
                if credential:
                    label = f"{path}.{credential}"
                    if label not in removed:
                        removed.append(label)
                else:
                    output[key] = clean(content, f"{path}.{key}")
            return output
        if isinstance(value, (list, tuple)):
            return [clean(content, f"{path}[{index}]") for index, content in enumerate(value)]
        return value

    return clean(dict(settings or {}), "settings"), removed


def validate_agent_settings(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the env-name-only credential contract used by API writes."""

    original = dict(settings or {})
    _cleaned, removed = scrub_agent_settings(original)
    if removed:
        raise ValueError(
            "literal credentials cannot be stored in agent settings; use api_key_env"
        )
    def validate_env_names(value):
        if isinstance(value, Mapping):
            for name, content in value.items():
                if str(name).strip().casefold().replace("_", "").replace("-", "") == "apikeyenv" and content not in (None, ""):
                    if not isinstance(content, str) or _ENV_NAME_RE.fullmatch(content) is None:
                        raise ValueError("api_key_env must be a valid environment-variable name")
                validate_env_names(content)
        elif isinstance(value, (list, tuple)):
            for item in value:
                validate_env_names(item)
    validate_env_names(original)
    version = original.get("persona_version", 1)
    if type(version) is not int or version < 1:
        raise ValueError("persona_version must be a positive integer")
    return original


def validate_hosted_provider(provider: str, settings: Mapping[str, Any] | None) -> None:
    """Validate the supported OpenRouter or Codex destination before reading secrets.

    The historical function name is retained for internal callers. The same
    boundary now applies to local and shared boards: OpenRouter for peers,
    server-controlled Codex authentication for Astra.
    """
    if provider.lower() not in {"codex", "openai_compatible"}:
        raise ValueError("agents must use Codex/Astra or OpenRouter (openai_compatible)")
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
    base_url = values.get("base_url") or os.getenv("OPENAI_COMPAT_BASE_URL") or "https://openrouter.ai/api/v1"
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
    # Deployment allowlists can narrow the supported destination/key pair.
    # Additional entries never enable a new host, IP range, or credential name.
    hosts = {entry.strip().casefold() for entry in os.getenv(
        "SWARMBOARD_ALLOWED_PROVIDER_HOSTS", "openrouter.ai").split(",") if entry.strip()}
    env_names = {entry.strip() for entry in os.getenv(
        "SWARMBOARD_ALLOWED_CREDENTIAL_ENV_VARS", "OPENROUTER_API_KEY").split(",") if entry.strip()}
    if host not in hosts:
        raise ValueError("provider host is outside SWARMBOARD_ALLOWED_PROVIDER_HOSTS")
    if key_name not in env_names:
        raise ValueError("credential environment name is outside SWARMBOARD_ALLOWED_CREDENTIAL_ENV_VARS")


__all__ = ["scrub_agent_settings", "validate_agent_settings", "validate_hosted_provider"]
