from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from aiohttp import ClientSession, CookieJar, web

from supergrok_openai.server import create_app
from supergrok_openai.stats import UsageStats


class FakeAuth:
    def __init__(self) -> None:
        self.force_refreshes = 0

    def has_credentials(self) -> bool:
        return True

    def local_api_key(self) -> str:
        return "local-secret"

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        if force_refresh:
            self.force_refreshes += 1
            return "upstream-fresh"
        return "upstream-old"

    def start_device_login(self, *, timeout: float = 20.0) -> dict:
        return {
            "discovery": {"token_endpoint": "https://auth.x.ai/oauth/token"},
            "device": {
                "device_code": "device",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://auth.x.ai/device",
                "verification_uri_complete": "https://auth.x.ai/device?code=ABCD-EFGH",
                "expires_in": 600,
                "interval": 1,
            },
        }

    def finish_device_login(self, session: dict, *, timeout: float = 20.0):
        return Path("fake-auth.json"), "local-secret"


async def start(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def setup_dashboard(client: ClientSession, url: str) -> tuple[str, str]:
    response = await client.post(
        f"{url}/auth/api/setup",
        json={  # pragma: allowlist secret
            "password": "DashboardPass8",  # pragma: allowlist secret
            "confirmation": "DashboardPass8",
        },
    )
    assert response.status == 200
    page_response = await client.get(url)
    assert page_response.status == 200
    page = await page_response.text()
    token_match = re.search(r'<meta name="admin-token" content="([^"]+)">', page)
    assert token_match
    return page, token_match.group(1)


async def test_proxy_requires_local_key_and_replaces_authorization(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))
    seen: dict[str, str] = {}

    async def upstream(request: web.Request) -> web.Response:
        authorization = request.headers.get("Authorization", "")
        if authorization == "Bearer upstream-old":
            return web.json_response({"error": "expired"}, status=401)
        seen["authorization"] = authorization
        seen["body"] = await request.text()
        seen["query"] = request.query_string
        return web.json_response({"object": "list", "data": []})

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream)
    upstream_runner, upstream_url = await start(upstream_app)

    auth = FakeAuth()
    proxy_app = create_app(
        auth, upstream_base_url=f"{upstream_url}/v1", enforce_xai_origin=False
    )
    proxy_runner, proxy_url = await start(proxy_app)
    try:
        async with ClientSession() as client:
            denied = await client.post(
                f"{proxy_url}/v1/chat/completions", json={"model": "grok"}
            )
            assert denied.status == 401

            allowed = await client.post(
                f"{proxy_url}/v1/chat/completions?trace=yes",
                headers={
                    "Authorization": "Bearer local-secret",
                    "Cookie": "do-not-forward=1",
                },
                json={"model": "claude-sonnet-5"},
            )
            assert allowed.status == 200
            assert await allowed.json() == {"object": "list", "data": []}
    finally:
        await proxy_runner.cleanup()
        await upstream_runner.cleanup()

    assert auth.force_refreshes == 1
    assert seen["authorization"] == "Bearer upstream-fresh"
    assert seen["query"] == "trace=yes"
    assert '"model": "grok-build-0.1"' in seen["body"]


async def test_non_ascii_api_key_is_rejected_without_server_error() -> None:
    app = create_app(FakeAuth())
    runner, url = await start(app)
    try:
        async with ClientSession() as client:
            response = await client.post(
                f"{url}/v1/chat/completions",
                headers={"Authorization": "Bearer 登录后生成"},
                json={"model": "grok-build-0.1", "messages": []},
            )
            assert response.status == 401
            assert (await response.json())["error"]["code"] == "invalid_api_key"
    finally:
        await runner.cleanup()


async def test_responses_custom_tool_is_bridged_through_stream() -> None:
    seen: dict[str, object] = {}
    patch = "*** Begin Patch\n*** Add File: hello.txt\n+hello\n*** End Patch"

    async def upstream(request: web.Request) -> web.Response:
        seen.update(await request.json())
        item = {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "apply_patch",
            "arguments": json.dumps({"input": patch}),
            "status": "completed",
        }
        events = [
            {"type": "response.output_item.added", "item": {**item, "arguments": ""}},
            {"type": "response.output_item.done", "item": item},
            {
                "type": "response.completed",
                "response": {"id": "resp_1", "output": [item]},
            },
        ]
        body = "".join(
            f"data: {json.dumps(event)}\n\n" for event in events
        ) + "data: [DONE]\n\n"
        return web.Response(text=body, content_type="text/event-stream")

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/responses", upstream)
    upstream_runner, upstream_url = await start(upstream_app)
    proxy_app = create_app(
        FakeAuth(), upstream_base_url=f"{upstream_url}/v1", enforce_xai_origin=False
    )
    proxy_runner, proxy_url = await start(proxy_app)
    try:
        async with ClientSession() as client:
            response = await client.post(
                f"{proxy_url}/v1/responses",
                headers={"Authorization": "Bearer local-secret"},
                json={
                    "model": "gpt-5.6-codex",
                    "stream": True,
                    "input": [{"role": "user", "content": "edit a file"}],
                    "tools": [
                        {
                            "type": "custom",
                            "name": "apply_patch",
                            "description": "Apply a raw patch.",
                            "format": {
                                "type": "grammar",
                                "syntax": "lark",
                                "definition": "start: PATCH",
                            },
                        }
                    ],
                },
            )
            assert response.status == 200
            events = [
                json.loads(line[6:])
                for line in (await response.text()).splitlines()
                if line.startswith("data: {")
            ]
    finally:
        await proxy_runner.cleanup()
        await upstream_runner.cleanup()

    assert seen["tools"][0]["type"] == "function"
    assert seen["tools"][0]["parameters"]["required"] == ["input"]
    added, done, completed = events
    assert added["item"]["type"] == "custom_tool_call"
    assert done["item"]["type"] == "custom_tool_call"
    assert done["item"]["input"] == patch
    assert completed["response"]["output"][0]["type"] == "custom_tool_call"


async def test_unknown_v1_path_is_rejected() -> None:
    auth = FakeAuth()
    app = create_app(auth)
    runner, url = await start(app)
    try:
        async with ClientSession() as client:
            response = await client.get(
                f"{url}/v1/files", headers={"Authorization": "Bearer local-secret"}
            )
            assert response.status == 404
            assert (await response.json())["error"]["code"] == "path_not_allowed"
    finally:
        await runner.cleanup()


async def test_dashboard_is_served_and_admin_api_requires_page_token(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))
    app = create_app(FakeAuth())
    runner, url = await start(app)
    try:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            redirect = await client.get(url, allow_redirects=False)
            assert redirect.status == 302
            assert redirect.headers["Location"] == "/auth"

            page, token = await setup_dashboard(client, url)
            assert "SuperGrok OpenAI" in page

            denied = await client.get(f"{url}/admin/api/status")
            assert denied.status == 403

            allowed = await client.get(
                f"{url}/admin/api/status",
                headers={"X-Admin-Token": token},
            )
            assert allowed.status == 200
            assert (await allowed.json())["authenticated"] is True
    finally:
        await runner.cleanup()


async def test_dashboard_password_setup_login_logout_and_rate_limit(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))
    app = create_app(FakeAuth())
    runner, url = await start(app)
    try:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            status = await client.get(f"{url}/auth/api/status")
            assert (await status.json())["configured"] is False

            weak = await client.post(
                f"{url}/auth/api/setup",
                json={  # pragma: allowlist secret
                    "password": "weakpass",  # pragma: allowlist secret
                    "confirmation": "weakpass",
                },
            )
            assert weak.status == 400
            assert (await weak.json())["code"] == "password_too_weak"

            await setup_dashboard(client, url)
            logout = await client.post(f"{url}/auth/api/logout")
            assert logout.status == 200
            locked = await client.get(url, allow_redirects=False)
            assert locked.status == 302

            login = await client.post(
                f"{url}/auth/api/login",
                json={"password": "DashboardPass8"},  # pragma: allowlist secret
            )
            assert login.status == 200
            assert (await client.get(url)).status == 200
            await client.post(f"{url}/auth/api/logout")

            for _ in range(5):
                denied = await client.post(
                    f"{url}/auth/api/login",
                    json={"password": "WrongPass8"},  # pragma: allowlist secret
                )
                assert denied.status == 401
            limited = await client.post(
                f"{url}/auth/api/login",
                json={"password": "DashboardPass8"},  # pragma: allowlist secret
            )
            assert limited.status == 429
            assert int(limited.headers["Retry-After"]) > 0
    finally:
        await runner.cleanup()


async def test_dashboard_starts_and_completes_device_login(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))
    app = create_app(FakeAuth())
    runner, url = await start(app)
    try:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            _, token = await setup_dashboard(client, url)
            headers = {"X-Admin-Token": token, "Content-Type": "application/json"}
            started = await client.post(
                f"{url}/admin/api/login/start", headers=headers, json={}
            )
            payload = await started.json()
            assert payload["login"]["status"] == "pending"
            assert payload["login"]["user_code"] == "ABCD-EFGH"

            for _ in range(20):
                await asyncio.sleep(0.01)
                status = await client.get(
                    f"{url}/admin/api/login/status", headers={"X-Admin-Token": token}
                )
                login = (await status.json())["login"]
                if login["status"] != "pending":
                    break
            assert login["status"] == "success"
    finally:
        await runner.cleanup()


async def test_anthropic_messages_endpoint_translates_and_tracks_usage(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))
    seen: dict = {}

    async def upstream(request: web.Request) -> web.Response:
        seen.update(await request.json())
        return web.json_response(
            {
                "id": "chatcmpl-anthropic",
                "model": "grok-4.5",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Hello from Grok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 14,
                    "completion_tokens": 4,
                    "total_tokens": 18,
                },
            }
        )

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream)
    upstream_runner, upstream_url = await start(upstream_app)
    proxy_app = create_app(
        FakeAuth(), upstream_base_url=f"{upstream_url}/v1", enforce_xai_origin=False
    )
    proxy_runner, proxy_url = await start(proxy_app)
    try:
        async with ClientSession() as client:
            response = await client.post(
                f"{proxy_url}/v1/messages",
                headers={
                    "x-api-key": "local-secret",
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-5",
                    "max_tokens": 128,
                    "system": "Be brief",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
            assert response.status == 200
            payload = await response.json()
    finally:
        await proxy_runner.cleanup()
        await upstream_runner.cleanup()

    assert seen["messages"][0] == {"role": "system", "content": "Be brief"}
    assert seen["model"] == "grok-build-0.1"
    assert payload["type"] == "message"
    assert payload["content"] == [{"type": "text", "text": "Hello from Grok"}]
    assert payload["usage"] == {"input_tokens": 14, "output_tokens": 4}
    assert UsageStats().snapshot()["totals"]["total_tokens"] == 18


async def test_anthropic_messages_stream_translates_sse(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))

    async def upstream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        chunks = [
            {
                "id": "chatcmpl-stream",
                "model": "grok-4.5",
                "choices": [{"delta": {"content": "Hello"}, "finish_reason": None}],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 1,
                    "total_tokens": 9,
                },
            },
            {
                "id": "chatcmpl-stream",
                "model": "grok-4.5",
                "choices": [{"delta": {"content": "!"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 2,
                    "total_tokens": 10,
                },
            },
        ]
        for chunk in chunks:
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/chat/completions", upstream)
    upstream_runner, upstream_url = await start(upstream_app)
    proxy_app = create_app(
        FakeAuth(), upstream_base_url=f"{upstream_url}/v1", enforce_xai_origin=False
    )
    proxy_runner, proxy_url = await start(proxy_app)
    try:
        async with ClientSession() as client:
            response = await client.post(
                f"{proxy_url}/v1/messages",
                headers={"x-api-key": "local-secret"},
                json={
                    "model": "grok-4.5",
                    "max_tokens": 128,
                    "stream": True,
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
            stream = await response.text()
    finally:
        await proxy_runner.cleanup()
        await upstream_runner.cleanup()

    assert response.status == 200
    assert "event: message_start" in stream
    assert '"text":"Hello"' in stream
    assert "event: message_stop" in stream
    assert UsageStats().snapshot()["totals"]["total_tokens"] == 10


async def test_empty_live_model_catalog_uses_hermes_fallback(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))

    async def models(_: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": []})

    upstream_app = web.Application()
    upstream_app.router.add_get("/v1/models", models)
    upstream_runner, upstream_url = await start(upstream_app)
    proxy_app = create_app(
        FakeAuth(), upstream_base_url=f"{upstream_url}/v1", enforce_xai_origin=False
    )
    proxy_runner, proxy_url = await start(proxy_app)
    try:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            _, token = await setup_dashboard(client, proxy_url)
            response = await client.post(
                f"{proxy_url}/admin/api/probe", headers={"X-Admin-Token": token}
            )
            payload = await response.json()
    finally:
        await proxy_runner.cleanup()
        await upstream_runner.cleanup()

    assert payload["source"] == "hermes-curated-fallback"
    assert payload["count"] >= 7
    assert "grok-4.5" in payload["models"]
