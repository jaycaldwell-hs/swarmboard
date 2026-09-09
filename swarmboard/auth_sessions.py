"""Durable collaborator sessions; bearer tokens never enter SQLite."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import String, delete, or_, select
from sqlalchemy.orm import Mapped, mapped_column

from .models import Base, UTCDateTime, utc_now


IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 8 * 60 * 60
MAX_USER_SESSIONS = 20
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    credential_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)


class SessionStore:
    def __init__(self, factory, *, settings_provider=None, clock=utc_now,
                 idle_seconds=IDLE_SECONDS, absolute_seconds=ABSOLUTE_SECONDS):
        from .auth import AuthSettings
        self.factory = factory
        self.settings_provider = settings_provider or AuthSettings.from_env
        self.clock = clock
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds

    def settings(self):
        return self.settings_provider()

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest() if isinstance(token, str) and _TOKEN.fullmatch(token) else None

    def _status(self, row=None, *, reason=None, now=None):
        now = now or self.clock()
        return {"enabled": True, "authenticated": row is not None,
                "user": row.username if row else None, "server_time": now.timestamp(),
                "idle_expires_at": min(row.last_activity_at + timedelta(seconds=self.idle_seconds),
                                       row.created_at + timedelta(seconds=self.absolute_seconds)).timestamp() if row else None,
                "absolute_expires_at": (row.created_at + timedelta(seconds=self.absolute_seconds)).timestamp() if row else None,
                **({"reason": reason} if reason else {})}

    def _reason(self, row, settings, now):
        if row is None or row.revoked_at is not None:
            return "logged_out"
        fingerprint = settings.fingerprint(row.username)
        if fingerprint is None or not hmac.compare_digest(fingerprint, row.credential_fingerprint):
            return "credentials_changed"
        if now >= row.created_at + timedelta(seconds=self.absolute_seconds):
            return "absolute_expired"
        if now >= row.last_activity_at + timedelta(seconds=self.idle_seconds):
            return "idle_expired"
        return None

    def inspect(self, token):
        digest = self._hash(token)
        if digest is None:
            return self._status(reason="authentication_required")
        settings, now = self.settings(), self.clock()
        with self.factory.begin() as session:
            row = session.get(AuthSession, digest)
            reason = self._reason(row, settings, now)
            if reason == "credentials_changed":
                row.revoked_at = now
            return self._status(None if reason else row, reason=reason, now=now)

    def valid(self, token):
        try:
            return self.inspect(token)["authenticated"]
        except ValueError:
            return False

    def issue(self, username, password, *, previous_token=None):
        settings = self.settings()
        username = settings.authenticate(username, password)
        if username is None:
            return None
        token, now = secrets.token_urlsafe(32), self.clock()
        with self.factory.begin() as session:
            self._cleanup(session, now)
            previous = session.get(AuthSession, self._hash(previous_token)) if self._hash(previous_token) else None
            if previous is not None:
                previous.revoked_at = now
            active = list(session.scalars(select(AuthSession).where(
                AuthSession.username == username, AuthSession.revoked_at.is_(None)
            ).order_by(AuthSession.created_at.desc(), AuthSession.token_hash)))
            fingerprint = settings.fingerprint(username)
            for row in active:
                if not hmac.compare_digest(row.credential_fingerprint, fingerprint):
                    row.revoked_at = now
            active = [row for row in active if row.revoked_at is None]
            for row in active[MAX_USER_SESSIONS - 1:]:
                row.revoked_at = now
            row = AuthSession(token_hash=self._hash(token), username=username,
                              credential_fingerprint=fingerprint,
                              created_at=now, last_activity_at=now)
            session.add(row)
            session.flush()
            result = self._status(row, now=now)
        return token, result

    def activity(self, token):
        digest = self._hash(token)
        if digest is None:
            return self._status(reason="authentication_required")
        settings, now = self.settings(), self.clock()
        with self.factory.begin() as session:
            row = session.get(AuthSession, digest)
            reason = self._reason(row, settings, now)
            if reason:
                if reason == "credentials_changed":
                    row.revoked_at = now
                return self._status(reason=reason, now=now)
            row.last_activity_at = now
            session.flush()
            return self._status(row, now=now)

    def logout(self, token):
        digest = self._hash(token)
        if digest:
            with self.factory.begin() as session:
                row = session.get(AuthSession, digest)
                if row is not None and row.revoked_at is None:
                    row.revoked_at = self.clock()
        return self._status(reason="logged_out")

    def _cleanup(self, session, now):
        session.execute(delete(AuthSession).where(or_(
            AuthSession.revoked_at.is_not(None),
            AuthSession.created_at <= now - timedelta(seconds=self.absolute_seconds),
            AuthSession.last_activity_at <= now - timedelta(seconds=self.idle_seconds),
        )))

    def cleanup(self):
        with self.factory.begin() as session:
            self._cleanup(session, self.clock())
