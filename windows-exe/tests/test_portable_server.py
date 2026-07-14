from __future__ import annotations

import re

from aiohttp import ClientSession, web

from supergrok_openai.server import create_app


class FakeAuth:
    def has_credentials(self) -> bool:
        return True

    def local_api_key(self) -> str:
        return "local-secret"

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        return "upstream-token"


async def start(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def test_dashboard_has_no_password_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))
    app = create_app(FakeAuth())
    runner, url = await start(app)
    try:
        async with ClientSession() as client:
            page_response = await client.get(url, allow_redirects=False)
            assert page_response.status == 200
            page = await page_response.text()
            assert "无网页密码" in page
            token = re.search(
                r'<meta name="admin-token" content="([^"]+)">', page
            ).group(1)

            denied = await client.get(f"{url}/admin/api/status")
            assert denied.status == 403
            allowed = await client.get(
                f"{url}/admin/api/status", headers={"X-Admin-Token": token}
            )
            assert allowed.status == 200
            assert (await allowed.json())["authenticated"] is True

            removed_route = await client.get(f"{url}/auth")
            assert removed_route.status == 404
    finally:
        await runner.cleanup()


async def test_proxy_keeps_local_api_key_protection(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))
    seen: dict[str, str] = {}

    async def upstream(request: web.Request) -> web.Response:
        seen["authorization"] = request.headers.get("Authorization", "")
        return web.json_response({"object": "list", "data": []})

    upstream_app = web.Application()
    upstream_app.router.add_get("/v1/models", upstream)
    upstream_runner, upstream_url = await start(upstream_app)
    proxy = create_app(
        FakeAuth(), upstream_base_url=f"{upstream_url}/v1", enforce_xai_origin=False
    )
    proxy_runner, proxy_url = await start(proxy)
    try:
        async with ClientSession() as client:
            denied = await client.get(f"{proxy_url}/v1/models")
            assert denied.status == 401
            allowed = await client.get(
                f"{proxy_url}/v1/models",
                headers={"Authorization": "Bearer local-secret"},
            )
            assert allowed.status == 200
    finally:
        await proxy_runner.cleanup()
        await upstream_runner.cleanup()

    assert seen["authorization"] == "Bearer upstream-token"
