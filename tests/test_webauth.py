from __future__ import annotations

import json

import pytest

from supergrok_openai.webauth import MAX_LOGIN_FAILURES, WebAuthError, WebAuthManager


def test_password_policy_and_argon2id_storage(tmp_path) -> None:
    manager = WebAuthManager(tmp_path / "dashboard-auth.json")

    with pytest.raises(WebAuthError) as weak:
        manager.set_initial_password("lowercase8")
    assert weak.value.code == "password_too_weak"

    manager.set_initial_password("StrongPass8")
    state = json.loads(manager.path.read_text(encoding="utf-8"))
    assert state["password_hash"].startswith("$argon2id$")
    assert "StrongPass8" not in manager.path.read_text(encoding="utf-8")
    assert manager.verify_password("StrongPass8") is True
    assert manager.verify_password("WrongPass8") is False


def test_initial_password_can_only_be_claimed_once(tmp_path) -> None:
    manager = WebAuthManager(tmp_path / "dashboard-auth.json")
    manager.set_initial_password("StrongPass8")

    with pytest.raises(WebAuthError) as duplicate:
        manager.set_initial_password("AnotherPass9")
    assert duplicate.value.code == "password_already_configured"


def test_sessions_are_revocable_and_failures_are_limited(tmp_path) -> None:
    manager = WebAuthManager(tmp_path / "dashboard-auth.json")
    token = manager.create_session()
    assert manager.authenticated(token) is True
    manager.revoke_session(token)
    assert manager.authenticated(token) is False

    for _ in range(MAX_LOGIN_FAILURES):
        manager.record_login_failure("192.168.1.10")
    assert manager.login_retry_after("192.168.1.10") > 0
    assert manager.login_retry_after("192.168.1.11") == 0
    manager.clear_login_failures("192.168.1.10")
    assert manager.login_retry_after("192.168.1.10") == 0
