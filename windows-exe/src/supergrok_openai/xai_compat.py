"""Normalize OpenAI/Anthropic client payloads for xAI's API dialect."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_XAI_MODEL = "grok-build-0.1"

_FOREIGN_MODEL_PREFIXES = ("claude-", "codex-", "gpt-", "o1", "o3", "o4")
_XAI_RESPONSE_INCLUDES = frozenset(
    {
        "web_search_call.action.sources",
        "code_interpreter_call.outputs",
        "file_search_call.results",
    }
)
_XAI_RESPONSE_TOOL_TYPES = frozenset(
    {
        "function",
        "web_search",
        "x_search",
        "image_generation",
        "collections_search",
        "file_search",
        "code_execution",
        "code_interpreter",
        "mcp",
        "shell",
    }
)
_XAI_NO_REASONING_EFFORT_MODELS = frozenset(
    {"grok-build-0.1", "grok-composer-2.5-fast"}
)


@dataclass(frozen=True)
class NamespaceTool:
    wire_name: str
    namespace: str
    name: str


@dataclass(frozen=True)
class CodexToolBridge:
    """Per-request metadata needed to restore Codex Responses tool semantics."""

    custom_tool_names: tuple[str, ...] = ()
    namespace_tools: tuple[NamespaceTool, ...] = ()
    tool_search: bool = False

    @property
    def requires_response_rewrite(self) -> bool:
        return bool(self.custom_tool_names or self.namespace_tools or self.tool_search)

    def namespace_for(self, wire_name: str) -> NamespaceTool | None:
        return next(
            (tool for tool in self.namespace_tools if tool.wire_name == wire_name),
            None,
        )


@dataclass(frozen=True)
class Adaptation:
    requested_model: str
    upstream_model: str
    removed_fields: tuple[str, ...] = ()
    tool_bridge: CodexToolBridge = field(default_factory=CodexToolBridge)

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
    """Sanitize Chat Completions tools without changing their wire format."""

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


def _wire_name(namespace: str, name: str) -> str:
    return f"{namespace}__{name}"


def _function_tool(raw: dict[str, Any], *, name: str | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {"type": "function", "name": name or str(raw["name"])}
    if isinstance(raw.get("description"), str):
        tool["description"] = raw["description"]
    parameters = raw.get("parameters")
    tool["parameters"] = _sanitize_json_schema(
        parameters if isinstance(parameters, dict) else {}
    )
    if isinstance(raw.get("strict"), bool):
        tool["strict"] = raw["strict"]
    return tool


def _custom_function_tool(raw: dict[str, Any], name: str) -> dict[str, Any]:
    description = str(raw.get("description") or "").strip()
    note = (
        "This is a freeform client tool transported as a function. Put the complete "
        'raw tool input in the JSON string field "input".'
    )
    return {
        "type": "function",
        "name": name,
        "description": f"{description}\n\n{note}".strip(),
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


def _tool_search_function(raw: dict[str, Any]) -> dict[str, Any]:
    parameters = raw.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Tool search query."},
                "limit": {"type": "number", "description": "Maximum results."},
            },
            "required": ["query"],
        }
    return {
        "type": "function",
        "name": "tool_search",
        "description": str(
            raw.get("description")
            or "Search for additional client tools to load for the next turn."
        ),
        "parameters": _sanitize_json_schema(parameters),
    }


def _collect_input_tool_hints(
    value: Any,
) -> tuple[list[str], list[NamespaceTool], list[dict[str, Any]], bool]:
    custom_names: list[str] = []
    namespace_tools: list[NamespaceTool] = []
    loaded_specs: list[dict[str, Any]] = []
    has_tool_search = False
    if not isinstance(value, list):
        return custom_names, namespace_tools, loaded_specs, has_tool_search

    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "custom_tool_call":
            name = str(item.get("name") or "").strip()
            if name:
                custom_names.append(name)
        elif item_type == "function_call":
            namespace = str(item.get("namespace") or "").strip()
            name = str(item.get("name") or "").strip()
            if namespace and name:
                namespace_tools.append(
                    NamespaceTool(_wire_name(namespace, name), namespace, name)
                )
        elif item_type in {"tool_search_call", "tool_search_output"}:
            has_tool_search = True
            if item_type == "tool_search_output" and isinstance(item.get("tools"), list):
                loaded_specs.extend(
                    tool for tool in item["tools"] if isinstance(tool, dict)
                )
        elif item_type == "additional_tools" and isinstance(item.get("tools"), list):
            loaded_specs.extend(tool for tool in item["tools"] if isinstance(tool, dict))
    return custom_names, namespace_tools, loaded_specs, has_tool_search


def _adapt_codex_tools(
    tools: Any,
    *,
    extra_tools: Iterable[dict[str, Any]] = (),
    custom_hints: Iterable[str] = (),
    namespace_hints: Iterable[NamespaceTool] = (),
    tool_search_hint: bool = False,
) -> tuple[list[dict[str, Any]], CodexToolBridge]:
    adapted: list[dict[str, Any]] = []
    seen_wire_names: set[str] = set()
    custom_names = list(custom_hints)
    namespace_tools = list(namespace_hints)
    tool_search = tool_search_hint

    raw_tools = list(tools) if isinstance(tools, list) else []
    raw_tools.extend(extra_tools)

    def append_function(tool: dict[str, Any], *, wire_name: str | None = None) -> bool:
        name = wire_name or str(tool.get("name") or "").strip()
        if not name or name in seen_wire_names:
            return False
        adapted.append(_function_tool(tool, name=name))
        seen_wire_names.add(name)
        return True

    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        tool = copy.deepcopy(raw)
        tool.pop("defer_loading", None)
        tool.pop("output_schema", None)
        tool_type = str(tool.get("type") or "")

        if tool_type == "function":
            append_function(tool)
            continue
        if tool_type == "namespace":
            namespace = str(tool.get("name") or "").strip()
            inner_tools = tool.get("tools")
            if not namespace or not isinstance(inner_tools, list):
                continue
            for inner in inner_tools:
                if not isinstance(inner, dict) or inner.get("type") != "function":
                    continue
                name = str(inner.get("name") or "").strip()
                if not name:
                    continue
                wire_name = _wire_name(namespace, name)
                if append_function(inner, wire_name=wire_name):
                    namespace_tools.append(
                        NamespaceTool(wire_name, namespace, name)
                    )
            continue
        if tool_type == "custom":
            name = str(tool.get("name") or "").strip()
            if name and name not in seen_wire_names:
                adapted.append(_custom_function_tool(tool, name))
                seen_wire_names.add(name)
                custom_names.append(name)
            continue
        if tool_type == "tool_search":
            if "tool_search" not in seen_wire_names:
                adapted.append(_tool_search_function(tool))
                seen_wire_names.add("tool_search")
            tool_search = True
            continue
        if tool_type in {"web_search", "web_search_preview"}:
            # Codex adds OpenAI-hosted search controls such as
            # external_web_access, indexed_web_access, and content_types.
            # xAI's Responses dialect supports the web_search tool itself but
            # rejects those OpenAI-only fields, so rebuild the tool instead of
            # passing an apparently supported type through verbatim.
            adapted.append({"type": "web_search"})
            continue
        if tool_type in _XAI_RESPONSE_TOOL_TYPES:
            if isinstance(tool.get("parameters"), dict):
                tool["parameters"] = _sanitize_json_schema(tool["parameters"])
            adapted.append(tool)
            continue

        # Current Codex uses named client-side tool variants beyond function/custom.
        # Preserve callable named variants as functions; never forward an enum xAI rejects.
        if str(tool.get("name") or "").strip():
            append_function(tool)

    deduped_namespaces = tuple(
        {
            (tool.wire_name, tool.namespace, tool.name): tool
            for tool in namespace_tools
        }.values()
    )
    return adapted, CodexToolBridge(
        custom_tool_names=tuple(dict.fromkeys(custom_names)),
        namespace_tools=deduped_namespaces,
        tool_search=tool_search,
    )


def _custom_input_as_function_arguments(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return json.dumps({"input": value}, ensure_ascii=False, separators=(",", ":"))


def _function_arguments_as_custom_input(value: Any) -> str:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    else:
        decoded = value
    if isinstance(decoded, str):
        return decoded
    if isinstance(decoded, dict):
        if "input" in decoded:
            inner = decoded["input"]
            return inner if isinstance(inner, str) else json.dumps(inner, ensure_ascii=False)
        command = decoded.get("command")
        if isinstance(command, list) and len(command) > 1 and command[0] == "apply_patch":
            return str(command[1])
        string_values = [item for item in decoded.values() if isinstance(item, str)]
        if len(string_values) == 1:
            return string_values[0]
    return json.dumps(decoded, ensure_ascii=False) if not isinstance(value, str) else value


def _json_arguments(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value if isinstance(value, dict) else {}, ensure_ascii=False)


def _normalize_integral_json_numbers(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_normalize_integral_json_numbers(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _normalize_integral_json_numbers(child)
            for key, child in value.items()
        }
    return value


def _has_integral_json_float(value: Any) -> bool:
    if isinstance(value, float):
        return value.is_integer()
    if isinstance(value, list):
        return any(_has_integral_json_float(item) for item in value)
    if isinstance(value, dict):
        return any(_has_integral_json_float(item) for item in value.values())
    return False


def _normalize_function_arguments(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value
    if not _has_integral_json_float(decoded):
        return value
    normalized = _normalize_integral_json_numbers(decoded)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _sanitize_input_content_parts(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    sanitized: list[Any] = []
    for raw_part in value:
        if not isinstance(raw_part, dict) or raw_part.get("type") != "encrypted_content":
            sanitized.append(raw_part)
            continue
        payload = raw_part.get("encrypted_content")
        looks_encrypted = isinstance(payload, str) and len(payload) >= 64 and bool(
            re.fullmatch(r"[A-Za-z0-9+/=_-]+", payload)
        )
        if looks_encrypted:
            sanitized.append(raw_part)
        elif isinstance(payload, str):
            # Codex multi-agent transport places plaintext spawn messages in
            # this slot and expects an OpenAI backend to encrypt it. xAI sees
            # the pre-encryption form, so restore its real text semantics.
            sanitized.append({"type": "input_text", "text": payload})
    return sanitized


def _tool_search_result(tools: Any) -> str:
    wire_names: list[str] = []
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") == "namespace" and isinstance(tool.get("tools"), list):
                namespace = str(tool.get("name") or "").strip()
                for inner in tool["tools"]:
                    if isinstance(inner, dict) and inner.get("name") and namespace:
                        wire_names.append(_wire_name(namespace, str(inner["name"])))
            elif tool.get("name"):
                wire_names.append(str(tool["name"]))
    return json.dumps({"loaded_tools": wire_names}, ensure_ascii=False)


def _sanitize_responses_input(value: Any, bridge: CodexToolBridge) -> Any:
    if not isinstance(value, list):
        return value
    custom_names = set(bridge.custom_tool_names)
    output: list[Any] = []
    for raw_item in value:
        if not isinstance(raw_item, dict):
            output.append(raw_item)
            continue
        item = copy.deepcopy(raw_item)
        item_type = item.get("type")
        if "content" in item:
            item["content"] = _sanitize_input_content_parts(item["content"])
        if item_type == "reasoning":
            # Codex replays reasoning output items in the next request. xAI's
            # stateless ModelInput parser rejects those replay items (including
            # the summary-only item emitted by grok-build-0.1 itself). The
            # messages and paired tool calls carry the usable conversation
            # state, so omit reasoning items from full-history replays.
            continue
        item.pop("_issuer_kind", None)
        if item_type == "function_call" and "arguments" in item:
            item["arguments"] = _normalize_function_arguments(item["arguments"])
        if item_type == "additional_tools":
            continue
        if item_type == "function_call" and item.get("namespace"):
            namespace = str(item.pop("namespace"))
            item["name"] = _wire_name(namespace, str(item.get("name") or ""))
        elif item_type == "custom_tool_call" and item.get("name") in custom_names:
            item["type"] = "function_call"
            item["arguments"] = _custom_input_as_function_arguments(item.pop("input", ""))
            item.pop("namespace", None)
        elif item_type == "custom_tool_call_output":
            item["type"] = "function_call_output"
            item.pop("name", None)
        elif item_type == "tool_search_call":
            item["type"] = "function_call"
            item["name"] = "tool_search"
            item["arguments"] = _json_arguments(item.get("arguments", {}))
            item.pop("execution", None)
        elif item_type == "tool_search_output":
            item["type"] = "function_call_output"
            item["output"] = _tool_search_result(item.pop("tools", []))
            item.pop("status", None)
        elif item_type == "agent_message":
            # Multi-agent v2 delivers a child's reply as a Codex-only history
            # item. Preserve its content as ordinary user context for xAI.
            item["type"] = "message"
            item["role"] = "user"
            item.pop("author", None)
            item.pop("recipient", None)
        output.append(item)
    return output


def _adapt_tool_choice(
    choice: Any, tools: list[dict[str, Any]], bridge: CodexToolBridge
) -> tuple[Any, list[dict[str, Any]]]:
    if not isinstance(choice, dict):
        return choice, tools
    choice_type = choice.get("type")
    if choice_type in {"custom", "tool_search"}:
        name = "tool_search" if choice_type == "tool_search" else choice.get("name")
        return {"type": "function", "name": name}, tools
    if choice_type == "function" and choice.get("namespace"):
        return {
            "type": "function",
            "name": _wire_name(str(choice["namespace"]), str(choice.get("name") or "")),
        }, tools
    if choice_type != "allowed_tools":
        return choice, tools

    allowed_names: set[str] = set()
    allowed_types: set[str] = set()
    entries = choice.get("tools")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_type = str(entry.get("type") or "")
            name = str(entry.get("name") or "").strip()
            namespace = str(entry.get("namespace") or "").strip()
            if name:
                allowed_names.add(_wire_name(namespace, name) if namespace else name)
                for mapped in bridge.namespace_tools:
                    if mapped.name == name:
                        allowed_names.add(mapped.wire_name)
            elif entry_type == "tool_search":
                allowed_names.add("tool_search")
            elif entry_type:
                allowed_types.add("web_search" if entry_type == "web_search_preview" else entry_type)

    filtered = [
        tool
        for tool in tools
        if (
            tool.get("type") == "function"
            and tool.get("name") in allowed_names
        )
        or tool.get("type") in allowed_types
    ]
    mode = "required" if choice.get("mode") == "required" else "auto"
    return (mode if filtered else "none"), filtered


def _tool_search_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            value = {}
    result = dict(value) if isinstance(value, dict) else {}
    if isinstance(result.get("limit"), str):
        try:
            result["limit"] = int(result["limit"])
        except ValueError:
            result.pop("limit", None)
    return result


def _transform_output_item(item: Any, bridge: CodexToolBridge) -> Any:
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return item
    name = str(item.get("name") or "")
    transformed = copy.deepcopy(item)
    if "arguments" in transformed:
        transformed["arguments"] = _normalize_function_arguments(
            transformed["arguments"]
        )
    if name in set(bridge.custom_tool_names):
        transformed["type"] = "custom_tool_call"
        transformed["input"] = _function_arguments_as_custom_input(
            transformed.pop("arguments", "")
        )
        return transformed
    if bridge.tool_search and name == "tool_search":
        transformed["type"] = "tool_search_call"
        transformed.pop("name", None)
        transformed["execution"] = "client"
        transformed["arguments"] = _tool_search_arguments(
            transformed.get("arguments", {})
        )
        return transformed
    namespaced = bridge.namespace_for(name)
    if namespaced:
        transformed["name"] = namespaced.name
        transformed["namespace"] = namespaced.namespace
    return transformed


def transform_xai_response_payload(payload: Any, bridge: CodexToolBridge) -> Any:
    """Restore standard Codex Responses items after the xAI-only transport hop."""

    if not isinstance(payload, dict):
        return payload
    transformed = copy.deepcopy(payload)
    if transformed.get("type") == "response.function_call_arguments.done":
        if "arguments" in transformed:
            transformed["arguments"] = _normalize_function_arguments(
                transformed["arguments"]
            )
    if isinstance(transformed.get("item"), dict):
        transformed["item"] = _transform_output_item(transformed["item"], bridge)
    for container in (transformed, transformed.get("response")):
        if not isinstance(container, dict) or not isinstance(container.get("output"), list):
            continue
        container["output"] = [
            _transform_output_item(item, bridge) for item in container["output"]
        ]
    return transformed


class XaiSseTransformer:
    """Stateful SSE translator for function calls rewritten to non-function items."""

    def __init__(self, bridge: CodexToolBridge):
        self.bridge = bridge
        self._non_function_item_ids: set[str] = set()

    def transform_line(self, line: bytes) -> bytes:
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

        event_type = payload.get("type") if isinstance(payload, dict) else None
        if event_type in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            item_id = str(payload.get("item_id") or payload.get("call_id") or "")
            if item_id in self._non_function_item_ids:
                return b""

        original_item = payload.get("item") if isinstance(payload, dict) else None
        transformed = transform_xai_response_payload(payload, self.bridge)
        new_item = transformed.get("item") if isinstance(transformed, dict) else None
        if (
            isinstance(original_item, dict)
            and original_item.get("type") == "function_call"
            and isinstance(new_item, dict)
            and new_item.get("type") in {"custom_tool_call", "tool_search_call"}
        ):
            for key in ("id", "call_id"):
                if original_item.get(key):
                    self._non_function_item_ids.add(str(original_item[key]))

        if transformed == payload:
            return line
        encoded = json.dumps(
            transformed, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return b"data: " + encoded + ending


def transform_xai_sse_line(line: bytes, bridge: CodexToolBridge) -> bytes:
    """Rewrite one standalone SSE data line (tests and non-stream callers)."""

    return XaiSseTransformer(bridge).transform_line(line)


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
    bridge = CodexToolBridge()
    if endpoint.startswith("/responses"):
        custom_hints, namespace_hints, loaded_specs, search_hint = (
            _collect_input_tool_hints(adapted.get("input"))
        )
        adapted_tools, bridge = _adapt_codex_tools(
            adapted.get("tools"),
            extra_tools=loaded_specs,
            custom_hints=custom_hints,
            namespace_hints=namespace_hints,
            tool_search_hint=search_hint,
        )
        if "tools" in adapted or loaded_specs:
            adapted["tools"] = adapted_tools
        if bridge.requires_response_rewrite or loaded_specs:
            removed.append("tools.codex_bridge")

        if "tool_choice" in adapted:
            adapted["tool_choice"], adapted_tools = _adapt_tool_choice(
                adapted["tool_choice"], adapted_tools, bridge
            )
            if "tools" in adapted or loaded_specs:
                adapted["tools"] = adapted_tools

        for field_name in ("client_metadata", "stream_options"):
            if field_name in adapted:
                adapted.pop(field_name, None)
                removed.append(field_name)

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
        if isinstance(reasoning, dict):
            if upstream_model in _XAI_NO_REASONING_EFFORT_MODELS:
                adapted.pop("reasoning", None)
                removed.append("reasoning")
            else:
                # xAI documents only the effort selector for supported models.
                # Codex also sends OpenAI-only summary/context controls.
                effort = reasoning.get("effort")
                if effort in {"none", "low", "medium", "high", "xhigh"}:
                    adapted["reasoning"] = {"effort": effort}
                else:
                    adapted.pop("reasoning", None)
                if adapted.get("reasoning") != reasoning:
                    removed.append("reasoning.unsupported")

        if "input" in adapted:
            original_input = adapted["input"]
            adapted["input"] = _sanitize_responses_input(original_input, bridge)
            if adapted["input"] != original_input:
                removed.append("input.codex_bridge")
    elif "tools" in adapted:
        adapted["tools"] = _sanitize_tools(adapted["tools"])

    return adapted, Adaptation(
        requested_model=requested_model,
        upstream_model=upstream_model,
        removed_fields=tuple(removed),
        tool_bridge=bridge,
    )
