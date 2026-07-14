"""Windows one-file launcher for the password-free LAN build."""

from __future__ import annotations

import sys

from supergrok_openai.cli import main


if __name__ == "__main__":
    print("SuperGrok Gateway Windows Portable / NO WEB PASSWORD")
    print("警告：控制面板对可信局域网直接开放，请勿映射到公网。")
    raise SystemExit(
        # Intentional LAN binding; server middleware rejects non-private clients.
        main(
            [
                "serve",
                "--host",
                "0.0.0.0",  # nosec B104
                "--allow-network",
                *sys.argv[1:],
            ]
        )
    )
