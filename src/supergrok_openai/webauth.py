"""Password and in-memory session protection for the browser dashboard."""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .store import app_home, load_state, save_state

COOKIE_NAME = "sg_dashboard_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_WINDOW_SECONDS = 10 * 60
MAX_LOGIN_FAILURES = 5
MAX_PASSWORD_LENGTH = 1024


class WebAuthError(RuntimeError):
    def __init__(self, message: str, *, code: str = "web_auth_error") -> None:
        super().__init__(message)
        self.code = code


def web_auth_path() -> Path:
    return app_home() / "dashboard-auth.json"


def password_requirements(password: str) -> list[str]:
    """Return unmet requirements without ever including the supplied password."""
    missing: list[str] = []
    if len(password) < 8:
        missing.append("密码至少需要 8 位")
    if len(password) > MAX_PASSWORD_LENGTH:
        missing.append("密码过长")
    if not re.search(r"[a-z]", password):
        missing.append("密码需要包含小写字母")
    if not re.search(r"[A-Z]", password):
        missing.append("密码需要包含大写字母")
    if not re.search(r"[0-9]", password):
        missing.append("密码需要包含数字")
    return missing


class WebAuthManager:
    """Own password verification, browser sessions, and login throttling."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or web_auth_path()
        self._hasher = PasswordHasher()
        self._lock = threading.RLock()
        self._sessions: dict[str, float] = {}
        self._failures: defaultdict[str, deque[float]] = defaultdict(deque)

    def _load(self) -> dict[str, Any]:
        return load_state(self.path)

    def configured(self) -> bool:
        return bool(str(self._load().get("password_hash") or "").strip())

    def set_initial_password(self, password: str) -> None:
        missing = password_requirements(password)
        if missing:
            raise WebAuthError("；".join(missing), code="password_too_weak")
        with self._lock:
            if self.configured():
                raise WebAuthError(
                    "管理密码已经设置", code="password_already_configured"
                )
            save_state(
                {
                    "password_hash": self._hasher.hash(password),
                    "created_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
                self.path,
            )

    def verify_password(self, password: str) -> bool:
        if len(password) > MAX_PASSWORD_LENGTH:
            return False
        with self._lock:
            state = self._load()
            encoded = str(state.get("password_hash") or "")
            if not encoded:
                return False
            try:
                verified = self._hasher.verify(encoded, password)
            except (InvalidHashError, VerificationError, VerifyMismatchError):
                return False
            if verified and self._hasher.check_needs_rehash(encoded):
                state["password_hash"] = self._hasher.hash(password)
                save_state(state, self.path)
            return bool(verified)

    @staticmethod
    def _session_digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_session(self) -> str:
        token = secrets.token_urlsafe(48)
        with self._lock:
            self._prune_sessions(time.time())
            self._sessions[self._session_digest(token)] = (
                time.time() + SESSION_TTL_SECONDS
            )
        return token

    def authenticated(self, token: str) -> bool:
        if not token:
            return False
        now = time.time()
        digest = self._session_digest(token)
        with self._lock:
            self._prune_sessions(now)
            expires_at = self._sessions.get(digest, 0)
            if expires_at <= now:
                return False
            self._sessions[digest] = now + SESSION_TTL_SECONDS
            return True

    def revoke_session(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(self._session_digest(token), None)

    def _prune_sessions(self, now: float) -> None:
        expired = [
            digest for digest, expires_at in self._sessions.items() if expires_at <= now
        ]
        for digest in expired:
            self._sessions.pop(digest, None)

    def login_retry_after(self, remote: str) -> int:
        now = time.time()
        key = remote or "unknown"
        with self._lock:
            attempts = self._failures[key]
            self._prune_failures(attempts, now)
            if len(attempts) < MAX_LOGIN_FAILURES:
                return 0
            return max(1, int(LOGIN_WINDOW_SECONDS - (now - attempts[0])))

    def record_login_failure(self, remote: str) -> None:
        now = time.time()
        key = remote or "unknown"
        with self._lock:
            attempts = self._failures[key]
            self._prune_failures(attempts, now)
            attempts.append(now)

    def clear_login_failures(self, remote: str) -> None:
        with self._lock:
            self._failures.pop(remote or "unknown", None)

    @staticmethod
    def _prune_failures(attempts: deque[float], now: float) -> None:
        cutoff = now - LOGIN_WINDOW_SECONDS
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
