"""Normalize OpenAI/Anthropic client payloads for xAI's API dialect."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable

DEFAULT_XAI_MODEL = "grok-build-0.1"

_FOREIGN_MODEL_PREFIXES = (
    "claude-",
    "codex-",
    "gpt-",
    "o1",
    "o3",
    "o4",
)
_XAI_RESPONSE_INCLUDES = frozenset(
    {
        "web_search_call.action.sources",
        "code_interpreter_call.outputs",
        "file_search_call.results",
    }
)


@dataclass(frozen=True)
class Adaptation:
    requested_model: str
    upstream_model: str
    removed_fields: tuple[str, ...] = ()

    @property
    def model_was_mapped(self) -> bool:
        return bool(
            self.requested_model
            and self.upstream_model
            and self.requested_model != self.upstream_model
        )


def _available_models(models: Iterable[Any]) -> list[str]:
    return list(
        dict.fromkeys(str(model).strip() for model in models if str(model).strip())
    )


def resolve_xai_model(requested: Any, available_models: Iterable[Any]) -> str:
    """Map foreign provider model IDs to Hermes' preferred SuperGrok model."""

    model = str(requested or "").strip()
    models = _available_models(available_models)
    lowered = model.lower()
    if model in models or lowered.startswith(("grok-", "x-ai/grok-")):
        return model
    if lowered in {"latest", "default", "auto"} or lowered.startswith(
        _FOREIGN_MODEL_PREFIXES
    ):
        if DEFAULT_XAI_MODEL in models:
            return DEFAULT_XAI_MODEL
        return models[0] if models else DEFAULT_XAI_MODEL
    return model


def _sanitize_json_schema(value: Any) -> Any:
    """Apply the xAI schema restrictions carried over from Hermes Agent."""

    if isinstance(value, list):
        return [_sanitize_json_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    sanitized: dict[str, Any] = {}
    for key, child in value.items():
        if key in {"pattern", "format"}:
            continue
        if key == "enum" and isinstance(child, list) and any(
            isinstance(item, str) and "/" in item for item in child
        ):
            continue
        sanitized[key] = _sanitize_json_schema(child)
    return sanitized


def _sanitize_tools(tools: Any) -> Any:
    if not isinstance(tools, list):
        return tools
    sanitized_tools: list[Any] = []
    for raw_tool in tools:
        if not isinstance(raw_tool, dict):
            sanitized_tools.append(raw_tool)
            continue
        tool = copy.deepcopy(raw_tool)
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("parameters"), dict):
            function["parameters"] = _sanitize_json_schema(function["parameters"])
        if isinstance(tool.get("parameters"), dict):
            tool["parameters"] = _sanitize_json_schema(tool["parameters"])
        if isinstance(tool.get("input_schema"), dict):
            tool["input_schema"] = _sanitize_json_schema(tool["input_schema"])
        tool.pop("defer_loading", None)
        tool.pop("output_schema", None)
        sanitized_tools.append(tool)
    return sanitized_tools


def _sanitize_responses_input(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    output: list[Any] = []
    for raw_item in value:
        if not isinstance(raw_item, dict):
            output.append(raw_item)
            continue
        item = copy.deepcopy(raw_item)
        if item.get("type") == "reasoning" and item.get("encrypted_content"):
            # Encrypted reasoning is sealed to the provider that issued it.
            # It cannot be replayed after switching a CCSwitch session to xAI.
            continue
        item.pop("_issuer_kind", None)
        output.append(item)
    return output


def adapt_xai_payload(
    payload: dict[str, Any],
    *,
    endpoint: str,
    available_models: Iterable[Any],
) -> tuple[dict[str, Any], Adaptation]:
    """Return an xAI-safe copy of a Chat Completions or Responses payload."""

    adapted = copy.deepcopy(payload)
    requested_model = str(adapted.get("model") or "").strip()
    upstream_model = resolve_xai_model(requested_model, available_models)
    if upstream_model:
        adapted["model"] = upstream_model

    removed: list[str] = []
    if "tools" in adapted:
        adapted["tools"] = _sanitize_tools(adapted["tools"])

    if endpoint.startswith("/responses"):
        for field in ("client_metadata", "stream_options"):
            if field in adapted:
                adapted.pop(field, None)
                removed.append(field)

        include = adapted.get("include")
        if isinstance(include, list):
            supported = [item for item in include if item in _XAI_RESPONSE_INCLUDES]
            if supported:
                adapted["include"] = supported
            else:
                adapted.pop("include", None)
            if supported != include:
                removed.append("include")

        text = adapted.get("text")
        if isinstance(text, dict) and "verbosity" in text:
            text = dict(text)
            text.pop("verbosity", None)
            if text:
                adapted["text"] = text
            else:
                adapted.pop("text", None)
            removed.append("text.verbosity")

        reasoning = adapted.get("reasoning")
        if isinstance(reasoning, dict) and "context" in reasoning:
            reasoning = dict(reasoning)
            reasoning.pop("context", None)
            if reasoning:
                adapted["reasoning"] = reasoning
            else:
                adapted.pop("reasoning", None)
            removed.append("reasoning.context")

        if "input" in adapted:
            original_input = adapted["input"]
            adapted["input"] = _sanitize_responses_input(original_input)
            if adapted["input"] != original_input:
                removed.append("input.encrypted_reasoning")

    return adapted, Adaptation(
        requested_model=requested_model,
        upstream_model=upstream_model,
        removed_fields=tuple(removed),
    )
