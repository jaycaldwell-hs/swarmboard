"""Shared HTTP Basic access, with credentials held only in deployment secrets."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}\Z")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_logger = logging.getLogger(__name__)


def _flag(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value not in {"", "0", "1", "false", "true", "no", "yes"}:
        raise ValueError(f"{name} must be a boolean")
    return value in {"1", "true", "yes"}


@dataclass(frozen=True)
class AuthSettings:
    # Retain fixed-size digests instead of plaintext credentials in app state.
    users: tuple[tuple[str, bytes, bytes], ...] = field(default=(), repr=False)

    @classmethod
    def from_env(cls) -> "AuthSettings":
        required = _flag("SWARMBOARD_REQUIRE_AUTH") or _flag("RENDER")
        raw = os.getenv("SWARMBOARD_AUTH_USERS")
        if raw is None:
            if required:
                raise ValueError("SWARMBOARD_AUTH_USERS is required when authentication is mandatory")
            return cls()
        try:
            users = json.loads(raw)
        except (TypeError, ValueError):
            raise ValueError("SWARMBOARD_AUTH_USERS must be a JSON object of usernames and passwords") from None
        if not isinstance(users, dict) or not users or len(users) > 100:
            raise ValueError("SWARMBOARD_AUTH_USERS must contain 1 to 100 users")
        records = []
        for username, password in users.items():
            if not isinstance(username, str) or not _USERNAME.fullmatch(username) or username == "SYSTEM":
                raise ValueError("SWARMBOARD_AUTH_USERS usernames must be valid human handles")
            if not isinstance(password, str) or not password or len(password) > 1024:
                raise ValueError("SWARMBOARD_AUTH_USERS passwords must contain 1 to 1024 characters")
            records.append((username, hashlib.sha256(username.encode()).digest(),
                            hashlib.sha256(password.encode()).digest()))
        return cls(tuple(records))

    def authenticate(self, authorization: str) -> str | None:
        scheme, separator, encoded = authorization.partition(" ")
        if not separator or scheme.lower() != "basic" or len(encoded) > 8192:
            return None
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            return None
        username, separator, password = decoded.partition(":")
        if not separator:
            return None
        user_digest = hashlib.sha256(username.encode()).digest()
        password_digest = hashlib.sha256(password.encode()).digest()
        authenticated = None
        for handle, expected_user, expected_password in self.users:
            # Evaluate both comparisons for every user, including unknown users.
            user_matches = hmac.compare_digest(user_digest, expected_user)
            password_matches = hmac.compare_digest(password_digest, expected_password)
            if user_matches & password_matches:
                authenticated = handle
        return authenticated


def human_handle(request: Request, fallback: str = "human") -> str:
    return getattr(request.state, "authenticated_user", None) or fallback


def request_key(request: Request, key: str | None) -> str | None:
    """Keep retries idempotent without one login consuming another's request key."""
    username = getattr(request.state, "authenticated_user", None)
    if username is None or key is None:
        return key
    return "auth:" + hashlib.sha256(json.dumps([username, key]).encode()).hexdigest()


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return None
        return (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except ValueError:
        return None


def _cross_origin_mutation(scope: Scope, headers: Headers) -> bool:
    if scope["method"] in _SAFE_METHODS:
        return False
    # Basic credentials can be sent automatically by a browser, so protect
    # state-changing requests against cross-origin use of a cached login.
    if headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
        return True
    origin = headers.get("origin")
    if origin is None:
        return False  # Non-browser API clients need no synthetic Origin header.
    expected = _origin(f"{scope['scheme']}://{headers.get('host', '')}")
    return expected is None or _origin(origin) != expected


class BasicAuthMiddleware:
    """Pure ASGI middleware preserves streaming responses and disconnect handling."""

    def __init__(self, app: ASGIApp, settings: AuthSettings,
                 audit: Callable[[Scope, int], Awaitable[None]] | None = None) -> None:
        self.app = app
        self.settings = settings
        self.audit = audit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"} or not self.settings.users:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # The application exposes SSE, not WebSockets. Do not leave a
            # future protocol endpoint outside the authenticated boundary.
            await send({"type": "websocket.close", "code": 1008})
            return

        audited = False

        async def private_send(message: Message) -> None:
            nonlocal audited
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "private, no-store"
                headers.add_vary_header("Authorization")
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Content-Security-Policy"] = "frame-ancestors 'none'"
                headers["X-Frame-Options"] = "DENY"
                if (self.audit is not None and not audited
                        and scope["method"] not in _SAFE_METHODS
                        and 200 <= message["status"] < 300
                        and scope.get("state", {}).get("authenticated_user")):
                    audited = True
                    try:
                        await self.audit(scope, message["status"])
                    except Exception:
                        # The endpoint has already committed its mutation. Do
                        # not turn an audit failure into a misleading retry, or
                        # log exception text that might contain request data.
                        _logger.error("Could not record authenticated human action")
            await send(message)

        public_health = scope["path"] == "/health" and scope["method"] in {"GET", "HEAD"}
        if not public_health:
            headers = Headers(scope=scope)
            authorizations = headers.getlist("authorization")
            username = self.settings.authenticate(authorizations[0]) if len(authorizations) == 1 else None
            if username is None:
                await JSONResponse({"detail": "Authentication required"}, status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="Swarmboard", charset="UTF-8"'})(
                        scope, receive, private_send)
                return
            if _cross_origin_mutation(scope, headers):
                await JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)(
                    scope, receive, private_send)
                return
            scope.setdefault("state", {})["authenticated_user"] = username
        await self.app(scope, receive, private_send)
