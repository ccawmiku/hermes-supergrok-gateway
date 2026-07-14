"""Persistent aggregate usage statistics based on upstream-reported tokens."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from .store import app_home, load_state, save_state
from .anthropic_compat import normalize_usage

logger = logging.getLogger(__name__)


def _empty_counter() -> dict[str, int]:
    return {
        "requests": 0,
        "usage_reported_requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
    }


class UsageStats:
    def __init__(self) -> None:
        self.path = app_home() / "stats.json"
        self._lock = threading.RLock()

    def _load(self) -> dict[str, Any]:
        state = load_state(self.path)
        if not state:
            state = {
                "totals": _empty_counter(),
                "by_model": {},
                "by_endpoint": {},
                "recent": [],
            }
        return state

    @staticmethod
    def _add(counter: dict[str, Any], usage: dict[str, int], reported: bool) -> None:
        counter["requests"] = int(counter.get("requests") or 0) + 1
        if reported:
            counter["usage_reported_requests"] = (
                int(counter.get("usage_reported_requests") or 0) + 1
            )
        for key in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
        ):
            counter[key] = int(counter.get(key) or 0) + int(usage.get(key) or 0)

    def record(self, *, model: str, endpoint: str, usage: dict[str, int]) -> None:
        with self._lock:
            state = self._load()
            normalized = {key: max(0, int(value or 0)) for key, value in usage.items()}
            reported = any(
                normalized.get(key, 0)
                for key in ("input_tokens", "output_tokens", "total_tokens")
            )
            totals = state.setdefault("totals", _empty_counter())
            by_model = state.setdefault("by_model", {})
            by_endpoint = state.setdefault("by_endpoint", {})
            model_key = model or "unknown"
            endpoint_key = endpoint or "unknown"
            model_counter = by_model.setdefault(model_key, _empty_counter())
            endpoint_counter = by_endpoint.setdefault(endpoint_key, _empty_counter())
            self._add(totals, normalized, reported)
            self._add(model_counter, normalized, reported)
            self._add(endpoint_counter, normalized, reported)
            recent = state.setdefault("recent", [])
            recent.insert(
                0,
                {
                    "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "model": model_key,
                    "endpoint": endpoint_key,
                    "usage_reported": reported,
                    **normalized,
                },
            )
            del recent[100:]
            save_state(state, self.path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = self._load()
            state["stats_path"] = str(self.path)
            return state

    def reset(self) -> None:
        with self._lock:
            save_state(
                {
                    "totals": _empty_counter(),
                    "by_model": {},
                    "by_endpoint": {},
                    "recent": [],
                },
                self.path,
            )


def usage_from_response_bytes(raw: bytes) -> dict[str, int]:
    """Extract the final/cumulative usage from JSON or OpenAI SSE bytes."""
    best = normalize_usage({})
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return best
    candidates: list[Any] = []
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            candidates.append(json.loads(stripped))
        except (json.JSONDecodeError, TypeError) as exc:
            logger.debug("usage response was not JSON: %s", type(exc).__name__)
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                candidates.append(json.loads(data))
            except (json.JSONDecodeError, TypeError) as exc:
                logger.debug(
                    "ignored malformed usage SSE event: %s", type(exc).__name__
                )
                continue
    for payload in candidates:
        if not isinstance(payload, dict) or not payload.get("usage"):
            continue
        usage = normalize_usage(payload["usage"])
        for key in best:
            best[key] = max(best[key], usage[key])
    return best
