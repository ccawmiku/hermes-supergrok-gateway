"""xAI device-code OAuth and refresh logic distilled from Hermes Agent."""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .store import ensure_local_api_key, load_state, new_local_api_key, save_state

XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_DEVICE_CODE_URL = f"{XAI_OAUTH_ISSUER}/oauth2/device/code"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_API_BASE_URL = "https://api.x.ai/v1"
LONG_TOKEN_REFRESH_SKEW_SECONDS = 3600
SHORT_TOKEN_REFRESH_SKEW_SECONDS = 120
logger = logging.getLogger(__name__)


class AuthError(RuntimeError):
    def __init__(
        self, message: str, *, code: str = "auth_error", status: int | None = None
    ):
        super().__init__(message)
        self.code = code
        self.status = status


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def jwt_exp(access_token: str) -> float | None:
    if not isinstance(access_token, str) or "." not in access_token:
        return None
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
        exp = payload.get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


def proactive_refresh_skew(access_token: str) -> int:
    exp = jwt_exp(access_token)
    if exp is None:
        return LONG_TOKEN_REFRESH_SKEW_SECONDS
    remaining = exp - time.time()
    if 0 < remaining <= 45 * 60:
        return SHORT_TOKEN_REFRESH_SKEW_SECONDS
    return LONG_TOKEN_REFRESH_SKEW_SECONDS


def validate_xai_oauth_endpoint(url: str, *, field: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or (host != "x.ai" and not host.endswith(".x.ai")):
        raise AuthError(
            f"xAI discovery returned an unsafe {field}: {url!r}",
            code="xai_discovery_invalid",
        )
    return url


def discover(timeout: float = 15.0) -> dict[str, str]:
    try:
        response = httpx.get(
            XAI_OAUTH_DISCOVERY_URL,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except Exception as exc:
        raise AuthError(
            f"xAI OIDC discovery failed: {exc}", code="xai_discovery_failed"
        ) from exc
    if response.status_code != 200:
        raise AuthError(
            f"xAI OIDC discovery returned HTTP {response.status_code}",
            code="xai_discovery_failed",
            status=response.status_code,
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            "xAI OIDC discovery returned invalid JSON", code="xai_discovery_invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise AuthError(
            "xAI OIDC discovery was not an object", code="xai_discovery_invalid"
        )
    authorization = str(payload.get("authorization_endpoint") or "").strip()
    token = str(payload.get("token_endpoint") or "").strip()
    if not authorization or not token:
        raise AuthError(
            "xAI OIDC discovery omitted required endpoints",
            code="xai_discovery_incomplete",
        )
    return {
        "authorization_endpoint": validate_xai_oauth_endpoint(
            authorization, field="authorization_endpoint"
        ),
        "token_endpoint": validate_xai_oauth_endpoint(token, field="token_endpoint"),
    }


def _json_or_error(response: httpx.Response, context: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            f"{context} returned invalid JSON", code="xai_invalid_json"
        ) from exc
    if not isinstance(payload, dict):
        raise AuthError(
            f"{context} returned a non-object response", code="xai_invalid_response"
        )
    return payload


def request_device_code(client: httpx.Client) -> dict[str, Any]:
    response = client.post(
        XAI_OAUTH_DEVICE_CODE_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        data={"client_id": XAI_OAUTH_CLIENT_ID, "scope": XAI_OAUTH_SCOPE},
    )
    if response.status_code != 200:
        raise AuthError(
            f"xAI device-code request failed with HTTP {response.status_code}: {response.text.strip()}",
            code="device_code_request_failed",
            status=response.status_code,
        )
    payload = _json_or_error(response, "xAI device-code request")
    required = {
        "device_code",
        "user_code",
        "verification_uri",
        "verification_uri_complete",
        "expires_in",
        "interval",
    }
    missing = sorted(required.difference(payload))
    if missing:
        raise AuthError(
            f"xAI device-code response omitted: {', '.join(missing)}",
            code="device_code_invalid",
        )
    return payload


def poll_device_token(
    client: httpx.Client,
    *,
    token_endpoint: str,
    device_code: str,
    expires_in: int,
    poll_interval: int,
) -> dict[str, Any]:
    validate_xai_oauth_endpoint(token_endpoint, field="token_endpoint")
    deadline = time.monotonic() + max(1, expires_in)
    interval = max(1, poll_interval)
    while time.monotonic() < deadline:
        response = client.post(
            token_endpoint,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": XAI_OAUTH_CLIENT_ID,
                "device_code": device_code,
            },
        )
        payload = _json_or_error(response, "xAI device-token polling")
        if response.status_code == 200:
            if not payload.get("access_token") or not payload.get("refresh_token"):
                raise AuthError(
                    "xAI token response omitted required tokens",
                    code="xai_device_token_invalid",
                )
            return payload
        error = str(payload.get("error") or "")
        if error == "authorization_pending":
            time.sleep(interval)
            continue
        if error == "slow_down":
            interval = min(interval + 1, 30)
            time.sleep(interval)
            continue
        detail = payload.get("error_description") or error or response.text
        raise AuthError(
            f"xAI device-token polling failed: {detail}",
            code="xai_device_token_failed",
            status=response.status_code,
        )
    raise AuthError(
        "Timed out waiting for xAI authorization", code="device_code_timeout"
    )


def refresh_tokens(
    tokens: dict[str, Any],
    *,
    token_endpoint: str,
    timeout: float = 20.0,
) -> dict[str, Any]:
    endpoint = validate_xai_oauth_endpoint(token_endpoint, field="token_endpoint")
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    if not refresh_token:
        raise AuthError(
            "No xAI refresh token is stored; log in again",
            code="xai_missing_refresh_token",
        )
    with httpx.Client(
        timeout=max(5.0, timeout), headers={"Accept": "application/json"}
    ) as client:
        response = client.post(
            endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "client_id": XAI_OAUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )
    if response.status_code != 200:
        detail = response.text.strip()
        raise AuthError(
            f"xAI token refresh failed with HTTP {response.status_code}"
            + (f": {detail}" if detail else ""),
            code="xai_refresh_failed",
            status=response.status_code,
        )
    payload = _json_or_error(response, "xAI token refresh")
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise AuthError(
            "xAI refresh response omitted access_token", code="xai_refresh_invalid"
        )
    updated = dict(tokens)
    updated.update(
        {
            "access_token": access_token,
            "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
            "token_type": str(payload.get("token_type") or "Bearer").strip()
            or "Bearer",
        }
    )
    if payload.get("id_token"):
        updated["id_token"] = str(payload["id_token"])
    if payload.get("expires_in") is not None:
        updated["expires_in"] = payload["expires_in"]
    updated["expires_at"] = _expiry_for(updated)
    return updated


def _expiry_for(tokens: dict[str, Any]) -> float | None:
    token_exp = jwt_exp(str(tokens.get("access_token") or ""))
    if token_exp is not None:
        return token_exp
    expires_in = tokens.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return time.time() + float(expires_in)
    return None


def _extract_hermes_tokens(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    providers = payload.get("providers")
    provider = providers.get("xai-oauth") if isinstance(providers, dict) else None
    if isinstance(provider, dict):
        tokens = provider.get("tokens")
        if (
            isinstance(tokens, dict)
            and tokens.get("access_token")
            and tokens.get("refresh_token")
        ):
            return dict(tokens), dict(provider.get("discovery") or {})

    pool = payload.get("credential_pool")
    entries = pool.get("xai-oauth") if isinstance(pool, dict) else None
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("access_token") and entry.get("refresh_token"):
                return {
                    "access_token": str(entry["access_token"]),
                    "refresh_token": str(entry["refresh_token"]),
                    "token_type": str(entry.get("token_type") or "Bearer"),
                }, {}
    raise AuthError(
        "Hermes auth.json has no usable xai-oauth tokens", code="hermes_xai_missing"
    )


class AuthManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    def has_credentials(self) -> bool:
        try:
            state = load_state()
            tokens = state.get("tokens")
            return (
                isinstance(tokens, dict)
                and bool(tokens.get("access_token"))
                and bool(tokens.get("refresh_token"))
            )
        except Exception:
            return False

    def local_api_key(self) -> str:
        state = load_state()
        key = str(state.get("local_api_key") or "").strip()
        if not key:
            raise AuthError(
                "No local API key is configured; log in or import Hermes auth"
            )
        return key

    def regenerate_local_api_key(self) -> str:
        with self._lock:
            state = load_state()
            if not state:
                raise AuthError(
                    "No credentials are stored; log in before rotating the local API key"
                )
            key = new_local_api_key()
            state["local_api_key"] = key
            save_state(state)
            return key

    def start_device_login(self, *, timeout: float = 20.0) -> dict[str, Any]:
        discovery = discover(timeout)
        with httpx.Client(
            timeout=max(20.0, timeout), headers={"Accept": "application/json"}
        ) as client:
            device = request_device_code(client)
        return {"discovery": discovery, "device": device}

    def finish_device_login(
        self,
        session: dict[str, Any],
        *,
        timeout: float = 20.0,
    ) -> tuple[Path, str]:
        discovery = session.get("discovery")
        device = session.get("device")
        if not isinstance(discovery, dict) or not isinstance(device, dict):
            raise AuthError(
                "Invalid pending xAI login session", code="device_code_invalid"
            )
        with httpx.Client(
            timeout=max(20.0, timeout), headers={"Accept": "application/json"}
        ) as client:
            tokens = poll_device_token(
                client,
                token_endpoint=str(discovery.get("token_endpoint") or ""),
                device_code=str(device.get("device_code") or ""),
                expires_in=int(device.get("expires_in") or 0),
                poll_interval=int(device.get("interval") or 1),
            )
        return self._save_login_tokens(tokens, discovery=discovery)

    def _save_login_tokens(
        self,
        tokens: dict[str, Any],
        *,
        discovery: dict[str, Any],
    ) -> tuple[Path, str]:
        tokens = dict(tokens)
        tokens["expires_at"] = _expiry_for(tokens)
        state = {
            "provider": "xai-oauth",
            "tokens": tokens,
            "discovery": dict(discovery),
            "last_refresh": utc_now(),
            "source": "oauth-device-code",
        }
        key = ensure_local_api_key(state)
        return save_state(state), key

    def login(
        self, *, open_browser: bool = True, timeout: float = 20.0
    ) -> tuple[Path, str]:
        session = self.start_device_login(timeout=timeout)
        device = session["device"]
        url = str(device.get("verification_uri_complete") or device["verification_uri"])
        code = str(device["user_code"])
        print(f"打开：{url}")
        print(f"如页面要求，输入验证码：{code}")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception as exc:
                logger.debug(
                    "could not open the system browser: %s", type(exc).__name__
                )
        print("等待浏览器授权……")
        return self.finish_device_login(session, timeout=timeout)

    def import_hermes(self, path: Path | None = None) -> tuple[Path, str]:
        hermes_home = (
            Path(os.getenv("HERMES_HOME", "")).expanduser()
            if os.getenv("HERMES_HOME")
            else None
        )
        source = path or ((hermes_home or (Path.home() / ".hermes")) / "auth.json")
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except Exception as exc:
            raise AuthError(
                f"Cannot read Hermes credentials from {source}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise AuthError(f"Hermes credential file is not a JSON object: {source}")
        tokens, discovery = _extract_hermes_tokens(payload)
        tokens["expires_at"] = _expiry_for(tokens)
        state = load_state()
        state.update(
            {
                "provider": "xai-oauth",
                "tokens": tokens,
                "discovery": discovery,
                "last_refresh": utc_now(),
                "source": f"hermes-import:{source}",
            }
        )
        key = ensure_local_api_key(state)
        return save_state(state), key

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            state = load_state()
            tokens = state.get("tokens")
            if not isinstance(tokens, dict):
                raise AuthError("No xAI OAuth credentials; run login or import-hermes")
            access_token = str(tokens.get("access_token") or "").strip()
            refresh_token = str(tokens.get("refresh_token") or "").strip()
            if not access_token or not refresh_token:
                raise AuthError(
                    "Stored xAI OAuth credentials are incomplete; log in again"
                )
            exp = jwt_exp(access_token)
            if exp is None and isinstance(tokens.get("expires_at"), (int, float)):
                exp = float(tokens["expires_at"])
            should_refresh = force_refresh
            if exp is not None and exp <= time.time() + proactive_refresh_skew(
                access_token
            ):
                should_refresh = True
            if not should_refresh:
                return access_token

            discovery = (
                state.get("discovery")
                if isinstance(state.get("discovery"), dict)
                else {}
            )
            endpoint = str(discovery.get("token_endpoint") or "").strip()
            if not endpoint:
                fresh_discovery = discover()
                state["discovery"] = fresh_discovery
                endpoint = fresh_discovery["token_endpoint"]
            updated = refresh_tokens(tokens, token_endpoint=endpoint)
            state["tokens"] = updated
            state["last_refresh"] = utc_now()
            save_state(state)
            return str(updated["access_token"])
