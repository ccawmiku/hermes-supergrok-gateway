from __future__ import annotations

import base64
import json
import time

import pytest

from supergrok_openai.auth import (
    AuthError,
    LONG_TOKEN_REFRESH_SKEW_SECONDS,
    SHORT_TOKEN_REFRESH_SKEW_SECONDS,
    jwt_exp,
    proactive_refresh_skew,
    validate_xai_oauth_endpoint,
)


def jwt_with_exp(exp: int) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


def test_jwt_exp_and_adaptive_refresh_skew() -> None:
    short = jwt_with_exp(int(time.time()) + 15 * 60)
    long = jwt_with_exp(int(time.time()) + 5 * 60 * 60)

    assert jwt_exp(short) is not None
    assert proactive_refresh_skew(short) == SHORT_TOKEN_REFRESH_SKEW_SECONDS
    assert proactive_refresh_skew(long) == LONG_TOKEN_REFRESH_SKEW_SECONDS


@pytest.mark.parametrize(
    "url",
    [
        "http://auth.x.ai/oauth/token",
        "https://attacker.example/oauth/token",
        "https://x.ai.attacker.example/oauth/token",
    ],
)
def test_oauth_endpoint_rejects_non_xai_origin(url: str) -> None:
    with pytest.raises(AuthError, match="unsafe"):
        validate_xai_oauth_endpoint(url, field="token_endpoint")


def test_oauth_endpoint_accepts_xai_https() -> None:
    url = "https://auth.x.ai/oauth/token"
    assert validate_xai_oauth_endpoint(url, field="token_endpoint") == url
