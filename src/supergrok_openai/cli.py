"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import socket
from pathlib import Path

from .auth import AuthError, AuthManager
from .server import DEFAULT_HOST, DEFAULT_PORT, run
from .store import auth_path, delete_state, load_state


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="supergrok-openai",
        description="把个人 SuperGrok OAuth 会话暴露为本地 OpenAI 兼容 API",
    )
    sub = root.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="通过 xAI device-code OAuth 登录")
    login.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    login.add_argument(
        "--timeout", type=float, default=20.0, help="单次 HTTP 请求超时秒数"
    )

    imported = sub.add_parser("import-hermes", help="复制现有 Hermes xai-oauth 凭据")
    imported.add_argument("--path", type=Path, help="Hermes auth.json 路径")

    serve = sub.add_parser("serve", help="启动本地 OpenAI 兼容 API")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument(
        "--allow-network",
        action="store_true",
        help="允许监听非回环地址；请自行配置防火墙与 TLS",
    )
    serve.add_argument(
        "--no-browser", action="store_true", help="启动时不自动打开控制面板"
    )
    serve.add_argument("--verbose", action="store_true")

    sub.add_parser("status", help="显示登录状态（不显示 OAuth token）")
    sub.add_parser("show-key", help="显示客户端应使用的本地 API key")
    sub.add_parser("logout", help="删除本工具复制/创建的凭据，不修改 Hermes")
    return root


def _is_loopback(host: str) -> bool:
    return host.lower() in {"127.0.0.1", "localhost", "::1"}


def _lan_addresses(port: int) -> list[str]:
    urls: set[str] = set()
    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = entry[4][0]
            if address and not address.startswith("127."):
                urls.add(f"http://{address}:{port}/")
    except OSError:
        pass
    return sorted(urls)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    manager = AuthManager()
    try:
        if args.command == "login":
            path, key = manager.login(
                open_browser=not args.no_browser, timeout=args.timeout
            )
            print(f"登录成功，凭据保存在：{path}")
            print(f"本地 API key：{key}")
            return 0
        if args.command == "import-hermes":
            path, key = manager.import_hermes(args.path)
            print(f"导入成功，独立副本保存在：{path}")
            print(f"本地 API key：{key}")
            print(
                "注意：xAI refresh token 会轮换；不要让 Hermes 与本工具长期并发使用这份导入会话。"
            )
            print(
                "如需同时使用，请改为运行 supergrok-openai login 创建独立 OAuth 会话。"
            )
            return 0
        if args.command == "serve":
            if not _is_loopback(args.host) and not args.allow_network:
                raise AuthError(
                    "拒绝监听非回环地址；如确有需要请显式添加 --allow-network"
                )
            logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
            print(f"本机控制面板：http://127.0.0.1:{args.port}/")
            if not _is_loopback(args.host):
                for url in _lan_addresses(args.port):
                    print(f"局域网控制面板：{url}")
                print("首次访问需要设置管理密码；不要把此端口映射到公网。")
            print(f"OpenAI 兼容 API：http://{args.host}:{args.port}/v1")
            print("按 Ctrl+C 停止。")
            run(
                manager,
                host=args.host,
                port=args.port,
                open_browser=not args.no_browser,
            )
            return 0
        if args.command == "status":
            state = load_state()
            print(f"凭据文件：{auth_path()}")
            print(f"已登录：{'是' if manager.has_credentials() else '否'}")
            print(f"来源：{state.get('source') or '-'}")
            print(f"最近登录/刷新：{state.get('last_refresh') or '-'}")
            return 0
        if args.command == "show-key":
            print(manager.local_api_key())
            return 0
        if args.command == "logout":
            print("已删除本地凭据。" if delete_state() else "本地没有凭据。")
            return 0
    except AuthError as exc:
        print(f"错误：{exc}")
        return 2
    except KeyboardInterrupt:
        print("\n已停止。")
        return 130
    return 1
