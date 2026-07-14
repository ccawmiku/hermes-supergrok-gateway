from __future__ import annotations

import json

from supergrok_openai.auth import AuthManager
from supergrok_openai.store import load_state, save_state


def test_store_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))
    saved = save_state({"tokens": {"access_token": "a", "refresh_token": "r"}})

    assert saved == tmp_path / "auth.json"
    assert load_state()["tokens"]["refresh_token"] == "r"


def test_imports_hermes_singleton_without_modifying_source(
    tmp_path, monkeypatch
) -> None:
    app_home = tmp_path / "app"
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(app_home))
    hermes = tmp_path / "hermes-auth.json"
    original = {
        "version": 1,
        "providers": {
            "xai-oauth": {
                "tokens": {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "token_type": "Bearer",
                },
                "discovery": {"token_endpoint": "https://auth.x.ai/oauth/token"},
            }
        },
    }
    hermes.write_text(json.dumps(original), encoding="utf-8")

    path, local_key = AuthManager().import_hermes(hermes)

    assert path == app_home / "auth.json"
    assert local_key.startswith("sg-local-")
    assert load_state()["tokens"]["access_token"] == "access"
    assert json.loads(hermes.read_text(encoding="utf-8")) == original


def test_regenerates_local_api_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))
    save_state(
        {
            "local_api_key": "sg-local-old",  # pragma: allowlist secret
            "tokens": {"access_token": "access", "refresh_token": "refresh"},
        }
    )

    new_key = AuthManager().regenerate_local_api_key()

    assert new_key.startswith("sg-local-")
    assert new_key != "sg-local-old"
    assert load_state()["local_api_key"] == new_key
