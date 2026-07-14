"""Normalize OpenAI/Anthropic client payloads for xAI's API dialect."""

from __future__ import annotations

import copy
import json
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
    custom_tool_names: tuple[str, ...] = ()

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


def _adapt_responses_custom_tools(tools: Any) -> tuple[Any, tuple[str, ...]]:
    """Expose OpenAI freeform custom tools to xAI as string functions."""

    if not isinstance(tools, list):
        return tools, ()
    adapted: list[Any] = []
    custom_names: list[str] = []
    for raw_tool in tools:
        if not isinstance(raw_tool, dict) or raw_tool.get("type") != "custom":
            adapted.append(raw_tool)
            continue
        name = str(raw_tool.get("name") or "").strip()
        if not name:
            # Preserve malformed input so the upstream error remains explicit.
            adapted.append(raw_tool)
            continue
        description = str(raw_tool.get("description") or "").strip()
        transport_note = (
            'This freeform tool is transported through xAI as a function. '
            'Put the complete raw tool input in the JSON string field "input".'
        )
        adapted.append(
            {
                "type": "function",
                "name": name,
                "description": f"{description}\n\n{transport_note}".strip(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {
                            "type": "string",
                            "description": "Complete raw input for the freeform tool.",
                        }
                    },
                    "required": ["input"],
                    "additionalProperties": False,
                },
                "strict": False,
            }
        )
        custom_names.append(name)
    return adapted, tuple(dict.fromkeys(custom_names))


def _custom_input_as_function_arguments(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return json.dumps({"input": value}, ensure_ascii=False, separators=(",", ":"))


def _function_arguments_as_custom_input(value: Any) -> str:
    if not isinstance(value, str):
        if isinstance(value, dict) and "input" in value:
            inner = value["input"]
            return inner if isinstance(inner, str) else json.dumps(inner, ensure_ascii=False)
        return json.dumps(value, ensure_ascii=False)
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value
    if isinstance(decoded, dict) and "input" in decoded:
        inner = decoded["input"]
        return inner if isinstance(inner, str) else json.dumps(inner, ensure_ascii=False)
    return value


def _sanitize_responses_input(value: Any, custom_tool_names: Iterable[str]) -> Any:
    if not isinstance(value, list):
        return value
    custom_names = set(custom_tool_names)
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
        if item.get("type") == "custom_tool_call" and item.get("name") in custom_names:
            item["type"] = "function_call"
            item["arguments"] = _custom_input_as_function_arguments(item.pop("input", ""))
            item.pop("namespace", None)
        elif item.get("type") == "custom_tool_call_output":
            item["type"] = "function_call_output"
            item.pop("name", None)
        output.append(item)
    return output


def _transform_custom_output_item(
    item: Any, custom_tool_names: Iterable[str]
) -> Any:
    if not isinstance(item, dict):
        return item
    custom_names = set(custom_tool_names)
    if item.get("type") != "function_call" or item.get("name") not in custom_names:
        return item
    transformed = copy.deepcopy(item)
    transformed["type"] = "custom_tool_call"
    transformed["input"] = _function_arguments_as_custom_input(
        transformed.pop("arguments", "")
    )
    return transformed


def transform_xai_response_payload(
    payload: Any, custom_tool_names: Iterable[str]
) -> Any:
    """Turn xAI function calls back into the custom calls Codex registered."""

    if not isinstance(payload, dict):
        return payload
    transformed = copy.deepcopy(payload)
    if isinstance(transformed.get("item"), dict):
        transformed["item"] = _transform_custom_output_item(
            transformed["item"], custom_tool_names
        )
    for container in (transformed, transformed.get("response")):
        if not isinstance(container, dict) or not isinstance(container.get("output"), list):
            continue
        container["output"] = [
            _transform_custom_output_item(item, custom_tool_names)
            for item in container["output"]
        ]
    return transformed


def transform_xai_sse_line(line: bytes, custom_tool_names: Iterable[str]) -> bytes:
    """Rewrite one SSE data line while preserving its original line ending."""

    stripped = line.rstrip(b"\r\n")
    ending = line[len(stripped) :]
    if not stripped.startswith(b"data:"):
        return line
    raw_data = stripped[5:].lstrip()
    if not raw_data or raw_data == b"[DONE]":
        return line
    try:
        payload = json.loads(raw_data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return line
    transformed = transform_xai_response_payload(payload, custom_tool_names)
    if transformed == payload:
        return line
    encoded = json.dumps(
        transformed, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return b"data: " + encoded + ending


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
    custom_tool_names: tuple[str, ...] = ()
    if "tools" in adapted:
        adapted["tools"] = _sanitize_tools(adapted["tools"])

    if endpoint.startswith("/responses"):
        if "tools" in adapted:
            adapted["tools"], custom_tool_names = _adapt_responses_custom_tools(
                adapted["tools"]
            )
            if custom_tool_names:
                removed.append("tools.custom")
        if isinstance(adapted.get("input"), list):
            history_names = (
                str(item.get("name") or "").strip()
                for item in adapted["input"]
                if isinstance(item, dict) and item.get("type") == "custom_tool_call"
            )
            custom_tool_names = tuple(
                dict.fromkeys((*custom_tool_names, *(name for name in history_names if name)))
            )
        tool_choice = adapted.get("tool_choice")
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "custom":
            tool_choice = dict(tool_choice)
            tool_choice["type"] = "function"
            adapted["tool_choice"] = tool_choice

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
            adapted["input"] = _sanitize_responses_input(
                original_input, custom_tool_names
            )
            if adapted["input"] != original_input:
                removed.append("input.encrypted_reasoning")

    return adapted, Adaptation(
        requested_model=requested_model,
        upstream_model=upstream_model,
        removed_fields=tuple(removed),
        custom_tool_names=custom_tool_names,
    )
