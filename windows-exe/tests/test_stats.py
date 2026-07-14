from __future__ import annotations

from supergrok_openai.stats import UsageStats, usage_from_response_bytes


def test_extracts_cumulative_usage_from_xai_sse() -> None:
    raw = b"\n".join(
        [
            b'data: {"usage":{"prompt_tokens":10,"completion_tokens":1,"total_tokens":11}}',
            b'data: {"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}}',
            b"data: [DONE]",
        ]
    )

    assert usage_from_response_bytes(raw)["total_tokens"] == 13
    assert usage_from_response_bytes(raw)["output_tokens"] == 3


def test_usage_stats_persist_by_model_and_reset(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUPERGROK_OPENAI_HOME", str(tmp_path))
    stats = UsageStats()
    stats.record(
        model="grok-4.5",
        endpoint="/v1/messages",
        usage={
            "input_tokens": 12,
            "output_tokens": 5,
            "total_tokens": 17,
            "cached_tokens": 3,
            "reasoning_tokens": 2,
        },
    )

    snapshot = stats.snapshot()
    assert snapshot["totals"]["requests"] == 1
    assert snapshot["totals"]["total_tokens"] == 17
    assert snapshot["by_model"]["grok-4.5"]["cached_tokens"] == 3

    stats.reset()
    assert stats.snapshot()["totals"]["requests"] == 0
