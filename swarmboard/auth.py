"""Expiring collaborator cookie sessions with deployment-owned credentials."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}\Z")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_logger = logging.getLogger(__name__)
COOKIE_NAME = "swarmboard_session"
ACTIVITY_HEADER = "X-Swarmboard-Activity"
_PUBLIC_ASSETS = {"/static/auth.js", "/static/auth.css"}


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

    def authenticate(self, username: str, password: str) -> str | None:
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 80 or len(password) > 1024:
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

    def fingerprint(self, username: str) -> str | None:
        return next((hashlib.sha256(user_digest + password_digest).hexdigest()
                     for handle, user_digest, password_digest in self.users if handle == username), None)


def human_handle(request: Request, fallback: str = "human") -> str:
    return (getattr(request.state, "authenticated_user", None)
            or os.getenv("SWARMBOARD_LOCAL_OPERATOR") or fallback)


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
    # Cookies are sent automatically by a browser. Keep mutations same-origin.
    if headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
        return True
    origin = headers.get("origin")
    if origin is None:
        return False  # Non-browser API clients need no synthetic Origin header.
    expected = _origin(f"{scope['scheme']}://{headers.get('host', '')}")
    return expected is None or _origin(origin) != expected


def session_token(headers: Headers) -> str | None:
    values = [part.partition("=")[2].strip() for header in headers.getlist("cookie")
              for part in header.split(";") if part.partition("=")[0].strip() == COOKIE_NAME]
    return values[0] if len(values) == 1 else None


def _local_status():
    return {"enabled": False, "authenticated": True, "user": os.getenv("SWARMBOARD_LOCAL_OPERATOR"),
            "server_time": time.time(), "idle_expires_at": None, "absolute_expires_at": None}


def _secure_cookie(request):
    return request.url.scheme == "https" or _flag("SWARMBOARD_HOSTED") or _flag("RENDER")


def _clear_cookie(response, request):
    response.delete_cookie(COOKIE_NAME, path="/", secure=_secure_cookie(request), httponly=True, samesite="strict")


class LoginThrottle:
    """Bounded per-process attempt windows; no credentials enter the buckets."""
    def __init__(self, *, clock=time.monotonic, limit=20, window=60, max_buckets=4096):
        self.clock, self.limit, self.window, self.max_buckets = clock, limit, window, max_buckets
        self.buckets = OrderedDict()

    def allow(self, address):
        key = hashlib.sha256(address.encode()).hexdigest()
        now = self.clock()
        attempts = self.buckets.pop(key, deque())
        while attempts and attempts[0] <= now - self.window:
            attempts.popleft()
        allowed = len(attempts) < self.limit
        if allowed:
            attempts.append(now)
        self.buckets[key] = attempts
        while len(self.buckets) > self.max_buckets:
            self.buckets.popitem(last=False)
        return allowed


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


def router(store):
    api = APIRouter()
    throttle = LoginThrottle()

    @api.post("/api/auth/login")
    async def login(payload: LoginInput, request: Request):
        if not store.settings().users:
            return _local_status()
        address = request.client.host if request.client else "unknown"
        if not throttle.allow(address):
            return JSONResponse({"detail": "Too many login attempts; try again shortly"}, status_code=429,
                                headers={"Retry-After": str(throttle.window)})
        issued = store.issue(payload.username, payload.password, previous_token=session_token(request.headers))
        if issued is None:
            return JSONResponse({"detail": "Invalid username or password"}, status_code=401)
        token, status = issued
        response = JSONResponse(status)
        response.set_cookie(COOKIE_NAME, token, max_age=store.absolute_seconds, path="/",
                            secure=_secure_cookie(request), httponly=True, samesite="strict")
        return response

    @api.get("/api/auth/session")
    async def session_status(request: Request):
        if not store.settings().users:
            return _local_status()
        return store.inspect(session_token(request.headers))

    @api.post("/api/auth/logout")
    async def logout(request: Request):
        status = store.logout(session_token(request.headers)) if store.settings().users else _local_status()
        response = JSONResponse(status)
        _clear_cookie(response, request)
        return response

    @api.post("/api/auth/activity")
    async def activity(request: Request):
        if request.headers.get(ACTIVITY_HEADER) != "1":
            return JSONResponse({"detail": "An explicit activity header is required"}, status_code=403)
        if not store.settings().users:
            return _local_status()
        status = store.activity(session_token(request.headers))
        return JSONResponse(status, status_code=200 if status["authenticated"] else 401)

    return api


class SessionAuthMiddleware:
    """Pure ASGI middleware preserves streaming responses and disconnect handling."""

    def __init__(self, app: ASGIApp, settings: AuthSettings, store=None,
                 audit: Callable[[Scope, int], Awaitable[None]] | None = None) -> None:
        self.app = app
        self.settings = settings
        self.store = store
        self.audit = audit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
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
                headers.add_vary_header("Cookie")
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Content-Security-Policy"] = "frame-ancestors 'none'"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                if (self.audit is not None and not audited
                        and scope["method"] not in _SAFE_METHODS
                        and not scope["path"].startswith("/api/auth/")
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

        try:
            settings = self.store.settings() if self.store else self.settings
        except ValueError:
            await JSONResponse({"detail": "Authentication configuration is unavailable"}, status_code=503)(scope, receive, private_send)
            return
        local_operator = os.getenv("SWARMBOARD_LOCAL_OPERATOR")
        if not settings.users:
            if local_operator and (not _USERNAME.fullmatch(local_operator) or local_operator == "SYSTEM"):
                await JSONResponse({"detail": "Invalid local operator identity"}, status_code=503)(scope, receive, send)
                return
            if local_operator:
                scope.setdefault("state", {})["authenticated_user"] = local_operator
            scope.setdefault("state", {})["auth_session_valid"] = lambda: True
            await self.app(scope, receive, private_send)
            return

        headers = Headers(scope=scope)
        if _cross_origin_mutation(scope, headers):
            await JSONResponse({"detail": "Cross-origin requests are not allowed"}, status_code=403)(scope, receive, private_send)
            return
        path, method = scope["path"], scope["method"]
        public = (method in {"GET", "HEAD"} and path in {"/health", "/login", "/api/auth/session", *_PUBLIC_ASSETS}
                  or method == "POST" and path in {"/api/auth/login", "/api/auth/logout"})
        token = session_token(headers)
        status = self.store.inspect(token) if self.store else {"authenticated": False, "reason": "authentication_required"}
        scope.setdefault("state", {})["auth_session_valid"] = lambda: bool(self.store and self.store.valid(token))
        if status["authenticated"]:
            scope["state"]["authenticated_user"] = status["user"]
        elif not public:
            navigation = method in {"GET", "HEAD"} and not path.startswith(("/api/", "/static/")) and "text/html" in headers.get("accept", "")
            if navigation:
                next_path = path if path.startswith("/") and not path.startswith("//") and "\\" not in path and not any(ord(ch) < 32 for ch in path) else "/"
                query = scope.get("query_string", b"").decode("latin-1")[:2048]
                response = RedirectResponse("/login?" + urlencode({"next": next_path + ("?" + query if query else "")}), status_code=303)
            else:
                response = JSONResponse({"detail": "Authentication required", "reason": status.get("reason", "authentication_required")}, status_code=401)
            await response(scope, receive, private_send)
            return
        await self.app(scope, receive, private_send)


# Compatibility import only; this class never accepts HTTP Basic credentials.
BasicAuthMiddleware = SessionAuthMiddleware
