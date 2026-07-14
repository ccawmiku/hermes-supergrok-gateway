"""Small, atomic credential store used only by this proxy."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Any

STORE_VERSION = 1
MAX_STORE_BYTES = 1_000_000


class StoreError(RuntimeError):
    pass


def app_home() -> Path:
    override = os.getenv("SUPERGROK_OPENAI_HOME", "").strip()
    return (
        Path(override).expanduser() if override else Path.home() / ".supergrok-openai"
    )


def auth_path() -> Path:
    return app_home() / "auth.json"


def load_state(path: Path | None = None) -> dict[str, Any]:
    target = path or auth_path()
    if not target.exists():
        return {}
    try:
        if target.stat().st_size > MAX_STORE_BYTES:
            raise StoreError(f"credential store is unexpectedly large: {target}")
        payload = json.loads(target.read_text(encoding="utf-8"))
    except StoreError:
        raise
    except Exception as exc:
        raise StoreError(f"cannot read credential store {target}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StoreError(f"credential store must contain a JSON object: {target}")
    return payload


def save_state(state: dict[str, Any], path: Path | None = None) -> Path:
    target = path or auth_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload["version"] = STORE_VERSION
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    fd, temp_name = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=target.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_permissions(temp)
        os.replace(temp, target)
        _restrict_permissions(target)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
    return target


def ensure_local_api_key(state: dict[str, Any]) -> str:
    existing = str(state.get("local_api_key") or "").strip()
    if existing:
        return existing
    key = "sg-local-" + secrets.token_urlsafe(32)
    state["local_api_key"] = key
    return key


def new_local_api_key() -> str:
    return "sg-local-" + secrets.token_urlsafe(32)


def delete_state(path: Path | None = None) -> bool:
    target = path or auth_path()
    if not target.exists():
        return False
    target.unlink()
    return True


def _restrict_permissions(path: Path) -> None:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows ACLs are inherited from the user's profile directory. chmod is
        # still attempted, but failure must not corrupt an otherwise valid save.
        pass
