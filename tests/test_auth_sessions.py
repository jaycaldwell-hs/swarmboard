from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from swarmboard.app import create_app
from swarmboard.auth import COOKIE_NAME, AuthSettings, LoginThrottle
from swarmboard.auth_sessions import ABSOLUTE_SECONDS, IDLE_SECONDS, MAX_USER_SESSIONS, AuthSession, SessionStore
from swarmboard.database import init_db, make_engine, make_session_factory
from .auth_helpers import login
from .test_engine_acceptance import ScriptedGateway


@pytest.fixture
def auth_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", '{"researcher":"test-session-password"}')
    engine = make_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    init_db(engine)
    factory = make_session_factory(engine)
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    store = SessionStore(factory, clock=lambda: now[0])
    try:
        yield store, now, engine
    finally:
        engine.dispose()


def issue(store):
    return store.issue("researcher", "test-session-password")


def test_only_hashes_are_stored_and_sessions_survive_store_restart(auth_store):
    store, now, engine = auth_store
    token, status = issue(store)
    assert status["authenticated"] and status["user"] == "researcher"
    with store.factory() as session:
        row = session.scalar(select(AuthSession))
        fields = {column.key: str(getattr(row, column.key)) for column in AuthSession.__table__.columns}
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in json.dumps(fields) and "test-session-password" not in json.dumps(fields)
    init_db(engine)
    restarted = SessionStore(make_session_factory(engine), clock=lambda: now[0])
    assert restarted.inspect(token) == status


def test_reads_do_not_refresh_idle_and_expired_session_cannot_be_revived(auth_store):
    store, now, _ = auth_store
    token, status = issue(store)
    now[0] += timedelta(seconds=IDLE_SECONDS - 1)
    assert store.inspect(token)["idle_expires_at"] == status["idle_expires_at"]
    assert store.valid(token)
    now[0] += timedelta(seconds=1)
    assert store.inspect(token)["reason"] == "idle_expired"
    assert not store.activity(token)["authenticated"]
    assert not store.valid(token)


def test_activity_refreshes_idle_without_extending_absolute_expiry(auth_store):
    store, now, _ = auth_store
    token, status = issue(store)
    start = now[0]
    for seconds in range(1700, ABSOLUTE_SECONDS, 1700):
        now[0] = start + timedelta(seconds=seconds)
        active = store.activity(token)
        assert active["authenticated"]
        assert active["absolute_expires_at"] == status["absolute_expires_at"]
        assert active["idle_expires_at"] <= active["absolute_expires_at"]
    now[0] = start + timedelta(seconds=ABSOLUTE_SECONDS)
    assert store.activity(token)["reason"] == "absolute_expired"


def test_logout_and_password_changes_revoke_existing_sessions(auth_store, monkeypatch):
    store, _, _ = auth_store
    logged_out, _ = issue(store)
    other, _ = issue(store)
    store.logout(logged_out)
    assert store.inspect(logged_out)["reason"] == "logged_out"
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", '{"researcher":"changed-session-password"}')
    assert store.inspect(other)["reason"] == "credentials_changed"
    assert store.issue("researcher", "test-session-password") is None
    current, _ = store.issue("researcher", "changed-session-password")
    assert store.valid(current)
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", '{"researcher":"test-session-password"}')
    assert store.inspect(other)["reason"] == "logged_out"


def test_session_population_and_cleanup_are_bounded(auth_store):
    store, now, _ = auth_store
    oldest, _ = issue(store)
    for _ in range(MAX_USER_SESSIONS):
        now[0] += timedelta(seconds=1)
        issue(store)
    assert not store.valid(oldest)
    with store.factory() as session:
        assert session.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.revoked_at.is_(None))) == MAX_USER_SESSIONS
    now[0] += timedelta(seconds=IDLE_SECONDS)
    store.cleanup()
    with store.factory() as session:
        assert session.scalar(select(func.count()).select_from(AuthSession)) == 0


def test_throttle_attempt_window_and_memory_are_bounded():
    now = [100.0]
    throttle = LoginThrottle(clock=lambda: now[0], limit=2, window=60, max_buckets=3)
    assert throttle.allow("client") and throttle.allow("client")
    assert not throttle.allow("client")
    now[0] += 60
    assert throttle.allow("client")
    for index in range(10):
        assert throttle.allow(f"client-{index}")
    assert len(throttle.buckets) == 3


@pytest.fixture
async def app_client(tmp_path, monkeypatch):
    monkeypatch.setattr("swarmboard.config.load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("SWARMBOARD_AUTH_USERS", '{"researcher":"test-session-password"}')
    monkeypatch.setenv("SWARMBOARD_REQUIRE_AUTH", "1")
    app = create_app(database_url=f"sqlite:///{tmp_path / 'auth-api.db'}", gateway=ScriptedGateway())
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    app.state.auth_sessions.clock = lambda: now[0]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://board.test") as client:
            yield app, client, now


@pytest.mark.asyncio
async def test_cookie_login_polling_idle_expiry_and_explicit_activity(app_client):
    app, client, now = app_client
    anonymous = await client.get("/api/auth/session")
    assert anonymous.status_code == 200 and not anonymous.json()["authenticated"]
    response = await client.post("/api/auth/login", json={"username": "researcher", "password": "test-session-password"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert "domain=" not in cookie and "test-session-password" not in response.text
    token = client.cookies[COOKIE_NAME]
    initial = response.json()
    now[0] += timedelta(seconds=IDLE_SECONDS - 10)
    for path in ("/api/state", "/api/auth/session", "/api/events?once=true"):
        assert (await client.get(path)).status_code == 200
    assert app.state.auth_sessions.inspect(token)["idle_expires_at"] == initial["idle_expires_at"]
    assert (await client.post("/api/auth/activity")).status_code == 403
    renewed = await client.post("/api/auth/activity", headers={"X-Swarmboard-Activity": "1", "Origin": "https://board.test"})
    assert renewed.status_code == 200 and renewed.json()["idle_expires_at"] > initial["idle_expires_at"]
    now[0] += timedelta(seconds=IDLE_SECONDS)
    expired = await client.get("/api/state", auth=("researcher", "test-session-password"))
    assert expired.status_code == 401 and expired.json()["reason"] == "idle_expired"
    assert "www-authenticate" not in expired.headers
    assert (await client.post("/api/auth/activity", headers={"X-Swarmboard-Activity": "1"})).status_code == 401
    assert not (await client.get("/api/auth/session")).json()["authenticated"]


@pytest.mark.asyncio
async def test_login_failure_is_generic_and_rate_limited(app_client):
    _, client, _ = app_client
    for index in range(20):
        response = await client.post("/api/auth/login", json={"username": "unknown" if index % 2 else "researcher", "password": "wrong-password"})
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid username or password"}
    throttled = await client.post("/api/auth/login", json={"username": "researcher", "password": "test-session-password"})
    assert throttled.status_code == 429 and throttled.headers["retry-after"] == "60"
    assert COOKIE_NAME not in client.cookies


@pytest.mark.asyncio
async def test_browser_navigation_redirects_to_a_relative_login_destination(app_client):
    _, client, _ = app_client
    response = await client.get("/sessions?view=recent", headers={"Accept": "text/html"})
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fsessions%3Fview%3Drecent"
    assert (await client.get("/api/state", headers={"Accept": "text/html"})).status_code == 401
