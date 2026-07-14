"""OpenAI-compatible proxy and password-free LAN control panel."""

from __future__ import annotations

import asyncio
import hmac
import html
import ipaddress
import json
import logging
import secrets
import threading
import time
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from .anthropic_compat import (
    AnthropicCompatError,
    AnthropicStreamTranslator,
    anthropic_error,
    anthropic_to_openai,
    normalize_usage,
    openai_to_anthropic,
)
from .auth import AuthError, AuthManager, XAI_API_BASE_URL
from .stats import UsageStats, usage_from_response_bytes
from .store import auth_path, delete_state, load_state
from .xai_compat import (
    Adaptation,
    adapt_xai_payload,
    transform_xai_response_payload,
    transform_xai_sse_line,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8645
MAX_REQUEST_BYTES = 10_000_000
ALLOWED_PATHS = frozenset(
    {
        "/models",
        "/responses",
        "/responses/compact",
        "/chat/completions",
        "/completions",
        "/embeddings",
    }
)
USAGE_PATHS = frozenset(
    {
        "/responses",
        "/responses/compact",
        "/chat/completions",
        "/completions",
        "/embeddings",
    }
)
HERMES_XAI_FALLBACK_MODELS = [
    "grok-build-0.1",
    "grok-composer-2.5-fast",
    "grok-4.5",
    "grok-4.3",
    "grok-4.20-0309-reasoning",
    "grok-4.20-0309-non-reasoning",
    "grok-4.20-multi-agent-0309",
]
HOP_BY_HOP = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "accept-encoding",
    }
)


def _error(status: int, message: str, code: str) -> web.Response:
    return web.json_response(
        {"error": {"message": message, "type": code, "code": code}}, status=status
    )


def _admin_error(status: int, message: str, code: str = "admin_error") -> web.Response:
    return web.json_response(
        {"ok": False, "message": message, "code": code}, status=status
    )


def _filter_request_headers(headers: Any) -> dict[str, str]:
    return {
        key: value for key, value in headers.items() if key.lower() not in HOP_BY_HOP
    }


def _filter_response_headers(headers: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP and key.lower() != "content-length"
    }


def _bearer_from_request(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return str(
        request.headers.get("x-api-key") or request.headers.get("api-key") or ""
    ).strip()


def _secure_equal(left: str, right: str) -> bool:
    """Constant-time comparison that also handles non-ASCII client input."""

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _validate_upstream(base_url: str, enforce_xai_origin: bool) -> str:
    candidate = base_url.rstrip("/")
    if not enforce_xai_origin:
        return candidate
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or (host != "x.ai" and not host.endswith(".x.ai")):
        raise ValueError("OAuth bearer may only be sent to an HTTPS xAI origin")
    return candidate


def _is_lan_client(remote: str | None) -> bool:
    if not remote:
        return False
    try:
        address = ipaddress.ip_address(remote.split("%", 1)[0])
    except ValueError:
        return remote.lower() == "localhost"
    if address.is_loopback or address.is_private or address.is_link_local:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(
        mapped and (mapped.is_loopback or mapped.is_private or mapped.is_link_local)
    )


def _web_text(filename: str) -> str:
    return (
        resources.files("supergrok_openai")
        .joinpath("web", filename)
        .read_text(encoding="utf-8")
    )


class LoginController:
    """Own the single background device-code poll used by the dashboard."""

    def __init__(self, auth: AuthManager) -> None:
        self.auth = auth
        self.task: asyncio.Task[None] | None = None
        self.state: dict[str, Any] = {"status": "idle"}
        self._guard = asyncio.Lock()

    def pending(self) -> bool:
        return bool(self.task and not self.task.done())

    async def start(self, *, timeout: float = 20.0) -> dict[str, Any]:
        async with self._guard:
            if self.pending():
                return dict(self.state)
            session = await asyncio.to_thread(
                self.auth.start_device_login, timeout=timeout
            )
            device = session["device"]
            verification_url = str(
                device.get("verification_uri_complete")
                or device.get("verification_uri")
                or ""
            )
            expires_in = int(device.get("expires_in") or 0)
            self.state = {
                "status": "pending",
                "verification_url": verification_url,
                "user_code": str(device.get("user_code") or ""),
                "expires_at": time.time() + expires_in,
                "message": "等待在 xAI 页面确认登录",
            }
            self.task = asyncio.create_task(self._finish(session, timeout=timeout))
            return dict(self.state)

    async def _finish(self, session: dict[str, Any], *, timeout: float) -> None:
        try:
            path, _ = await asyncio.to_thread(
                self.auth.finish_device_login, session, timeout=timeout
            )
            self.state = {
                "status": "success",
                "message": "xAI OAuth 登录成功",
                "auth_path": str(path),
            }
        except Exception as exc:
            code = exc.code if isinstance(exc, AuthError) else "login_failed"
            self.state = {"status": "error", "message": str(exc), "code": code}

    def snapshot(self) -> dict[str, Any]:
        return dict(self.state)


def create_app(
    auth: AuthManager,
    *,
    upstream_base_url: str = XAI_API_BASE_URL,
    enforce_xai_origin: bool = True,
) -> web.Application:
    upstream = _validate_upstream(upstream_base_url, enforce_xai_origin)
    admin_token = secrets.token_urlsafe(32)

    @web.middleware
    async def dashboard_security(
        request: web.Request, handler: Any
    ) -> web.StreamResponse:
        browser_path = (
            request.path in {"/", "/dashboard"}
            or request.path.startswith("/assets/")
            or request.path.startswith("/admin/")
        )
        if not _is_lan_client(request.remote):
            return _admin_error(403, "服务只允许从本机或局域网访问", "lan_required")
        if request.path.startswith("/admin/api/"):
            supplied = request.headers.get("X-Admin-Token", "")
            if not supplied or not _secure_equal(supplied, admin_token):
                return _admin_error(
                    403, "控制面板会话无效，请刷新页面", "admin_token_invalid"
                )
        response = await handler(request)
        if browser_path:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Cache-Control"] = "no-store"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            )
        return response

    app = web.Application(
        middlewares=[dashboard_security], client_max_size=MAX_REQUEST_BYTES
    )
    session_key = web.AppKey("upstream_session", aiohttp.ClientSession)
    login = LoginController(auth)
    usage_stats = UsageStats()
    model_catalog: dict[str, Any] = {
        "models": list(HERMES_XAI_FALLBACK_MODELS),
        "source": "hermes-curated-fallback",
        "updated_at": "",
    }

    async def start_session(application: web.Application) -> None:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=600)
        application[session_key] = aiohttp.ClientSession(
            timeout=timeout, trust_env=True, auto_decompress=False
        )

    async def close_session(application: web.Application) -> None:
        await application[session_key].close()

    app.on_startup.append(start_session)
    app.on_cleanup.append(close_session)

    async def dashboard(_: web.Request) -> web.Response:
        page = _web_text("index.html").replace(
            "__ADMIN_TOKEN__", html.escape(admin_token)
        )
        return web.Response(text=page, content_type="text/html")

    async def dashboard_redirect(_: web.Request) -> web.Response:
        raise web.HTTPFound("/")

    async def asset(request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        if filename not in {"app.css", "app.js"}:
            raise web.HTTPNotFound()
        content_type = (
            "text/css" if filename.endswith(".css") else "application/javascript"
        )
        return web.Response(text=_web_text(filename), content_type=content_type)

    async def health(_: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "upstream": "xAI Grok OAuth",
                "authenticated": auth.has_credentials(),
                "dashboard": "/",
            }
        )

    async def admin_status(_: web.Request) -> web.Response:
        try:
            state = load_state()
            authenticated = auth.has_credentials()
            api_key = auth.local_api_key() if authenticated else ""
            return web.json_response(
                {
                    "ok": True,
                    "authenticated": authenticated,
                    "source": state.get("source") or "",
                    "last_refresh": state.get("last_refresh") or "",
                    "auth_path": str(auth_path()),
                    "api_key": api_key,
                    "api_base_url": "/v1",
                    "upstream": XAI_API_BASE_URL,
                    "login": login.snapshot(),
                    "model_catalog": dict(model_catalog),
                }
            )
        except Exception as exc:
            return _admin_error(500, str(exc), "credential_store_error")

    async def admin_login_start(request: web.Request) -> web.Response:
        try:
            payload = await request.json() if request.can_read_body else {}
            timeout = (
                float(payload.get("timeout") or 20.0)
                if isinstance(payload, dict)
                else 20.0
            )
            state = await login.start(timeout=max(5.0, min(timeout, 120.0)))
            return web.json_response({"ok": True, "login": state})
        except AuthError as exc:
            return _admin_error(400, str(exc), exc.code)
        except Exception as exc:
            return _admin_error(500, str(exc), "login_start_failed")

    async def admin_login_status(_: web.Request) -> web.Response:
        return web.json_response({"ok": True, "login": login.snapshot()})

    async def admin_import(request: web.Request) -> web.Response:
        if login.pending():
            return _admin_error(409, "请先完成当前 xAI 登录", "login_in_progress")
        try:
            payload = await request.json() if request.can_read_body else {}
            raw_path = (
                str(payload.get("path") or "").strip()
                if isinstance(payload, dict)
                else ""
            )
            source = Path(raw_path).expanduser() if raw_path else None
            path, key = await asyncio.to_thread(auth.import_hermes, source)
            return web.json_response(
                {
                    "ok": True,
                    "message": "Hermes 凭据已导入",
                    "auth_path": str(path),
                    "api_key": key,
                    "warning": (
                        "xAI refresh token 会轮换。若 Hermes 也要同时运行，"
                        "请改用网页中的 xAI 独立登录。"
                    ),
                }
            )
        except AuthError as exc:
            return _admin_error(400, str(exc), exc.code)
        except Exception as exc:
            return _admin_error(500, str(exc), "hermes_import_failed")

    async def admin_logout(_: web.Request) -> web.Response:
        if login.pending():
            return _admin_error(409, "请先完成当前 xAI 登录", "login_in_progress")
        try:
            removed = await asyncio.to_thread(delete_state)
            return web.json_response(
                {"ok": True, "message": "本地凭据已删除" if removed else "本地没有凭据"}
            )
        except Exception as exc:
            return _admin_error(500, str(exc), "logout_failed")

    async def admin_regenerate_key(_: web.Request) -> web.Response:
        try:
            key = await asyncio.to_thread(auth.regenerate_local_api_key)
            return web.json_response(
                {"ok": True, "message": "本地 API key 已更新", "api_key": key}
            )
        except AuthError as exc:
            return _admin_error(400, str(exc), exc.code)
        except Exception as exc:
            return _admin_error(500, str(exc), "key_rotation_failed")

    async def _upstream_request(
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        query_string: str = "",
    ) -> aiohttp.ClientResponse:
        token = await asyncio.to_thread(auth.get_access_token)
        target = f"{upstream}{path}"
        if query_string:
            target += "?" + query_string

        async def send(active_token: str) -> aiohttp.ClientResponse:
            active_headers = dict(headers or {})
            active_headers["Authorization"] = f"Bearer {active_token}"
            active_headers["Accept-Encoding"] = "identity"
            return await app[session_key].request(
                method,
                target,
                headers=active_headers,
                data=body if body else None,
                allow_redirects=False,
            )

        response = await send(token)
        if response.status == 401:
            response.release()
            token = await asyncio.to_thread(auth.get_access_token, force_refresh=True)
            response = await send(token)
        return response

    async def admin_probe(_: web.Request) -> web.Response:
        try:
            response = await _upstream_request(
                "GET", "/models", headers={"Accept": "application/json"}
            )
            raw = await response.read()
            status = response.status
            response.release()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {}
            if status >= 400:
                detail = payload.get("error") if isinstance(payload, dict) else None
                return _admin_error(
                    status, str(detail or f"xAI 返回 HTTP {status}"), "probe_failed"
                )
            models: list[str] = []
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                models = [
                    str(item.get("id"))
                    for item in payload["data"]
                    if isinstance(item, dict) and item.get("id")
                ]
            if (
                isinstance(payload, dict)
                and not models
                and isinstance(payload.get("models"), list)
            ):
                models = [
                    str(item.get("id") if isinstance(item, dict) else item)
                    for item in payload["models"]
                    if item
                ]
            if models:
                model_catalog.update(
                    {
                        "models": list(dict.fromkeys(models)),
                        "source": "xai-live",
                        "updated_at": time.time(),
                    }
                )
                message = "xAI 连接正常，已读取实时模型目录"
            else:
                model_catalog.update(
                    {
                        "models": list(HERMES_XAI_FALLBACK_MODELS),
                        "source": "hermes-curated-fallback",
                        "updated_at": time.time(),
                    }
                )
                message = "xAI 连接正常；上游目录为空，已显示 Hermes 精选模型"
            return web.json_response(
                {
                    "ok": True,
                    "message": message,
                    "models": model_catalog["models"],
                    "count": len(model_catalog["models"]),
                    "source": model_catalog["source"],
                }
            )
        except AuthError as exc:
            return _admin_error(401, str(exc), exc.code)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return _admin_error(502, f"无法连接 xAI：{exc}", "upstream_unreachable")
        except Exception as exc:
            return _admin_error(500, str(exc), "probe_failed")

    async def admin_stats(_: web.Request) -> web.Response:
        try:
            return web.json_response(
                {"ok": True, "stats": await asyncio.to_thread(usage_stats.snapshot)}
            )
        except Exception as exc:
            return _admin_error(500, str(exc), "stats_read_failed")

    async def admin_stats_reset(_: web.Request) -> web.Response:
        try:
            await asyncio.to_thread(usage_stats.reset)
            return web.json_response({"ok": True, "message": "Token 统计已清零"})
        except Exception as exc:
            return _admin_error(500, str(exc), "stats_reset_failed")

    def _anthropic_response_error(status: int, message: str) -> web.Response:
        return web.json_response(anthropic_error(status, message), status=status)

    async def anthropic_messages(request: web.Request) -> web.StreamResponse:
        try:
            expected = auth.local_api_key()
        except Exception as exc:
            return _anthropic_response_error(401, str(exc))
        supplied = _bearer_from_request(request)
        if not supplied or not _secure_equal(supplied, expected):
            return _anthropic_response_error(401, "Invalid local API key")
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise AnthropicCompatError("request body must be a JSON object")
            converted = anthropic_to_openai(payload)
            converted, _adaptation = adapt_xai_payload(
                converted,
                endpoint="/chat/completions",
                available_models=model_catalog["models"],
            )
        except (
            json.JSONDecodeError,
            AnthropicCompatError,
            TypeError,
            ValueError,
        ) as exc:
            return _anthropic_response_error(400, str(exc))

        requested_model = str(payload.get("model") or "")
        is_stream = bool(payload.get("stream"))
        upstream_body = json.dumps(converted, ensure_ascii=False).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if is_stream else "application/json",
        }
        try:
            upstream_response = await _upstream_request(
                "POST", "/chat/completions", body=upstream_body, headers=headers
            )
        except AuthError as exc:
            return _anthropic_response_error(401, str(exc))
        except asyncio.TimeoutError:
            return _anthropic_response_error(504, "xAI upstream timed out")
        except aiohttp.ClientError as exc:
            return _anthropic_response_error(
                502, f"xAI upstream connection failed: {exc}"
            )

        if upstream_response.status >= 400:
            status = upstream_response.status
            raw = await upstream_response.read()
            upstream_response.release()
            try:
                upstream_error = json.loads(raw)
                detail = upstream_error.get("error", upstream_error)
                if isinstance(detail, dict):
                    detail = detail.get("message") or detail
            except Exception:
                detail = raw.decode("utf-8", errors="replace")[:1000]
            return _anthropic_response_error(
                status, str(detail or f"xAI returned HTTP {status}")
            )

        if not is_stream:
            try:
                raw = await upstream_response.read()
                upstream_response.release()
                upstream_payload = json.loads(raw)
                if not isinstance(upstream_payload, dict):
                    raise ValueError("xAI returned a non-object response")
                usage = normalize_usage(upstream_payload.get("usage"))
                await asyncio.to_thread(
                    usage_stats.record,
                    model=str(upstream_payload.get("model") or requested_model),
                    endpoint="/v1/messages",
                    usage=usage,
                )
                return web.json_response(
                    openai_to_anthropic(
                        upstream_payload, requested_model=requested_model
                    )
                )
            except Exception as exc:
                upstream_response.release()
                return _anthropic_response_error(
                    502, f"Cannot translate xAI response: {exc}"
                )

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)
        translator = AnthropicStreamTranslator(requested_model)
        pending = b""

        async def process_line(line: bytes) -> None:
            stripped = line.strip()
            if not stripped.startswith(b"data:"):
                return
            data = stripped[5:].strip()
            if data == b"[DONE]":
                events = translator.finish()
            else:
                try:
                    chunk_payload = json.loads(data)
                except Exception:
                    return
                if not isinstance(chunk_payload, dict):
                    return
                events = translator.feed(chunk_payload)
            for event in events:
                await response.write(event)

        try:
            async for chunk in upstream_response.content.iter_any():
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    await process_line(line)
            if pending:
                await process_line(pending)
            for event in translator.finish():
                await response.write(event)
        except (ConnectionResetError, aiohttp.ClientError, asyncio.CancelledError):
            logger.debug("client disconnected while streaming Anthropic response")
        finally:
            upstream_response.release()
            await asyncio.to_thread(
                usage_stats.record,
                model=translator.model or requested_model,
                endpoint="/v1/messages",
                usage=translator.usage,
            )
        try:
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    async def forward(request: web.Request) -> web.StreamResponse:
        try:
            expected = auth.local_api_key()
        except AuthError as exc:
            return _error(401, str(exc), exc.code)
        except Exception as exc:
            logger.warning("local credential store failed: %s", exc)
            return _error(
                500, "Cannot read the local credential store", "credential_store_error"
            )
        supplied = _bearer_from_request(request)
        if not supplied or not _secure_equal(supplied, expected):
            return _error(401, "Invalid local API key", "invalid_api_key")

        rel_path = "/" + request.match_info.get("tail", "").strip("/")
        if rel_path not in ALLOWED_PATHS:
            return _error(
                404, f"Endpoint /v1{rel_path} is not enabled", "path_not_allowed"
            )

        body = await request.read()
        adaptation = Adaptation("", "")
        if request.method == "POST" and rel_path in {
            "/responses",
            "/responses/compact",
            "/chat/completions",
            "/completions",
        }:
            try:
                request_payload = json.loads(body)
                if isinstance(request_payload, dict):
                    request_payload, adaptation = adapt_xai_payload(
                        request_payload,
                        endpoint=rel_path,
                        available_models=model_catalog["models"],
                    )
                    body = json.dumps(request_payload, ensure_ascii=False).encode()
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        headers = _filter_request_headers(request.headers)
        try:
            upstream_response = await _upstream_request(
                request.method,
                rel_path,
                body=body,
                headers=headers,
                query_string=request.query_string,
            )
        except AuthError as exc:
            return _error(401, str(exc), exc.code)
        except asyncio.TimeoutError:
            return _error(504, "xAI upstream timed out", "upstream_timeout")
        except aiohttp.ClientError as exc:
            logger.warning("xAI upstream connection failed: %s", exc)
            return _error(
                502, f"xAI upstream connection failed: {exc}", "upstream_unreachable"
            )

        response_headers = _filter_response_headers(upstream_response.headers)
        if adaptation.model_was_mapped:
            response_headers["X-SuperGrok-Requested-Model"] = (
                adaptation.requested_model
            )
            response_headers["X-SuperGrok-Upstream-Model"] = adaptation.upstream_model
        response = web.StreamResponse(
            status=upstream_response.status,
            headers=response_headers,
        )
        upstream_status = upstream_response.status
        collected = bytearray()
        await response.prepare(request)

        async def write_chunk(chunk: bytes) -> None:
            if not chunk:
                return
            if len(collected) < 4_000_000:
                remaining = 4_000_000 - len(collected)
                collected.extend(chunk[:remaining])
            await response.write(chunk)

        try:
            content_type = upstream_response.headers.get("Content-Type", "").lower()
            if adaptation.custom_tool_names and "text/event-stream" in content_type:
                async for line in upstream_response.content:
                    await write_chunk(
                        transform_xai_sse_line(line, adaptation.custom_tool_names)
                    )
            elif adaptation.custom_tool_names:
                upstream_body = await upstream_response.read()
                transformed_body = upstream_body
                try:
                    upstream_payload = json.loads(upstream_body)
                    transformed_payload = transform_xai_response_payload(
                        upstream_payload, adaptation.custom_tool_names
                    )
                    transformed_body = json.dumps(
                        transformed_payload, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass
                await write_chunk(transformed_body)
            else:
                async for chunk in upstream_response.content.iter_any():
                    await write_chunk(chunk)
        except (ConnectionResetError, aiohttp.ClientError, asyncio.CancelledError):
            logger.debug("client disconnected while streaming xAI response")
        finally:
            upstream_response.release()
            if upstream_status >= 400 and collected:
                detail = bytes(collected[:4000]).decode("utf-8", errors="replace")
                logger.warning(
                    "xAI returned HTTP %s for /v1%s (model %s -> %s): %s",
                    upstream_status,
                    rel_path,
                    adaptation.requested_model or "-",
                    adaptation.upstream_model or "-",
                    detail,
                )
            if rel_path in USAGE_PATHS and upstream_status < 400:
                try:
                    request_payload = json.loads(body) if body else {}
                except Exception:
                    request_payload = {}
                model = (
                    str(request_payload.get("model") or "unknown")
                    if isinstance(request_payload, dict)
                    else "unknown"
                )
                await asyncio.to_thread(
                    usage_stats.record,
                    model=model,
                    endpoint=f"/v1{rel_path}",
                    usage=usage_from_response_bytes(bytes(collected)),
                )
        try:
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    app.router.add_get("/", dashboard)
    app.router.add_get("/dashboard", dashboard_redirect)
    app.router.add_get("/assets/{filename}", asset)
    app.router.add_get("/health", health)
    app.router.add_get("/admin/api/status", admin_status)
    app.router.add_post("/admin/api/login/start", admin_login_start)
    app.router.add_get("/admin/api/login/status", admin_login_status)
    app.router.add_post("/admin/api/import-hermes", admin_import)
    app.router.add_post("/admin/api/logout", admin_logout)
    app.router.add_post("/admin/api/key/regenerate", admin_regenerate_key)
    app.router.add_post("/admin/api/probe", admin_probe)
    app.router.add_get("/admin/api/stats", admin_stats)
    app.router.add_post("/admin/api/stats/reset", admin_stats_reset)
    app.router.add_post("/v1/messages", anthropic_messages)
    app.router.add_route("*", "/v1/{tail:.*}", forward)
    return app


def run(
    auth: AuthManager,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> None:
    if open_browser:
        timer = threading.Timer(
            0.8, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")
        )
        timer.daemon = True
        timer.start()
    web.run_app(create_app(auth), host=host, port=port, access_log=None)
