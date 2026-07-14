"""Translate Anthropic Messages API traffic to xAI Chat Completions."""

from __future__ import annotations

import json
from typing import Any


class AnthropicCompatError(ValueError):
    pass


def _text_from_blocks(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
        elif block is not None:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(part for part in parts if part)


def _openai_content_block(block: dict[str, Any]) -> dict[str, Any] | None:
    kind = block.get("type")
    if kind == "text":
        return {"type": "text", "text": str(block.get("text") or "")}
    if kind == "image":
        source = block.get("source")
        if not isinstance(source, dict):
            raise AnthropicCompatError("image block is missing source")
        source_type = source.get("type")
        if source_type == "base64":
            media_type = str(source.get("media_type") or "image/png")
            url = f"data:{media_type};base64,{source.get('data') or ''}"
        elif source_type == "url":
            url = str(source.get("url") or "")
        else:
            raise AnthropicCompatError(
                f"unsupported Anthropic image source: {source_type!r}"
            )
        return {"type": "image_url", "image_url": {"url": url}}
    return None


def _convert_user_message(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if not isinstance(content, list):
        raise AnthropicCompatError("message content must be a string or an array")

    output: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def flush_user() -> None:
        if not pending:
            return
        output.append({"role": "user", "content": list(pending)})
        pending.clear()

    for block in content:
        if not isinstance(block, dict):
            raise AnthropicCompatError("content blocks must be objects")
        if block.get("type") == "tool_result":
            flush_user()
            output.append(
                {
                    "role": "tool",
                    "tool_call_id": str(block.get("tool_use_id") or ""),
                    "content": _text_from_blocks(block.get("content", "")),
                }
            )
            continue
        converted = _openai_content_block(block)
        if converted is not None:
            pending.append(converted)
    flush_user()
    return output or [{"role": "user", "content": ""}]


def _convert_assistant_message(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    if not isinstance(content, list):
        raise AnthropicCompatError("message content must be a string or an array")
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(
                            block.get("input") or {}, ensure_ascii=False
                        ),
                    },
                }
            )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) or None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            str(block.get("text") or "")
            for block in system
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def anthropic_to_openai(payload: dict[str, Any]) -> dict[str, Any]:
    model = str(payload.get("model") or "").strip()
    messages = payload.get("messages")
    if not model:
        raise AnthropicCompatError("model is required")
    if not isinstance(messages, list) or not messages:
        raise AnthropicCompatError("messages must be a non-empty array")

    converted_messages: list[dict[str, Any]] = []
    system = _system_text(payload.get("system"))
    if system:
        converted_messages.append({"role": "system", "content": system})
    for message in messages:
        if not isinstance(message, dict):
            raise AnthropicCompatError("each message must be an object")
        role = message.get("role")
        if role == "user":
            converted_messages.extend(_convert_user_message(message.get("content")))
        elif role == "assistant":
            converted_messages.append(
                _convert_assistant_message(message.get("content"))
            )
        else:
            raise AnthropicCompatError(f"unsupported Anthropic message role: {role!r}")

    result: dict[str, Any] = {"model": model, "messages": converted_messages}
    if payload.get("max_tokens") is not None:
        result["max_tokens"] = int(payload["max_tokens"])
    for key in ("temperature", "top_p", "stream"):
        if payload.get(key) is not None:
            result[key] = payload[key]
    if payload.get("stop_sequences"):
        result["stop"] = payload["stop_sequences"]

    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        result["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": str(tool.get("name") or ""),
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("input_schema")
                    or {"type": "object", "properties": {}},
                },
            }
            for tool in tools
            if isinstance(tool, dict) and tool.get("name")
        ]
        choice = payload.get("tool_choice")
        if isinstance(choice, dict):
            choice_type = choice.get("type")
            if choice_type == "any":
                result["tool_choice"] = "required"
            elif choice_type in {"auto", "none"}:
                result["tool_choice"] = choice_type
            elif choice_type == "tool":
                result["tool_choice"] = {
                    "type": "function",
                    "function": {"name": str(choice.get("name") or "")},
                }
        if payload.get("disable_parallel_tool_use") is not None:
            result["parallel_tool_calls"] = not bool(
                payload["disable_parallel_tool_use"]
            )
    return result


def _anthropic_id(value: Any) -> str:
    raw = str(value or "")
    if raw.startswith("msg_"):
        return raw
    for prefix in ("chatcmpl-", "chatcmpl_", "resp_", "response-"):
        if raw.startswith(prefix):
            raw = raw[len(prefix) :]
            break
    return "msg_" + (raw or "grok")


def _stop_reason(reason: Any) -> str:
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "refusal",
    }.get(str(reason or ""), "end_turn")


def _parse_tool_input(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(str(arguments or "{}"))
        return value if isinstance(value, dict) else {"value": value}
    except Exception:
        return {"raw": str(arguments or "")}


def normalize_usage(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        }
    input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    output_tokens = int(
        usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    )
    total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
    prompt_details = (
        usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    )
    completion_details = (
        usage.get("completion_tokens_details")
        or usage.get("output_tokens_details")
        or {}
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": int(prompt_details.get("cached_tokens") or 0)
        if isinstance(prompt_details, dict)
        else 0,
        "reasoning_tokens": int(completion_details.get("reasoning_tokens") or 0)
        if isinstance(completion_details, dict)
        else 0,
    }


def openai_to_anthropic(
    payload: dict[str, Any], *, requested_model: str
) -> dict[str, Any]:
    choices = payload.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    if not isinstance(message, dict):
        message = {}
    content: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})
    elif isinstance(text, list):
        for block in text:
            if isinstance(block, dict) and block.get("type") == "text":
                content.append({"type": "text", "text": str(block.get("text") or "")})
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = (
                call.get("function") if isinstance(call.get("function"), dict) else {}
            )
            content.append(
                {
                    "type": "tool_use",
                    "id": str(call.get("id") or "toolu_grok"),
                    "name": str(function.get("name") or ""),
                    "input": _parse_tool_input(function.get("arguments")),
                }
            )
    usage = normalize_usage(payload.get("usage"))
    return {
        "id": _anthropic_id(payload.get("id")),
        "type": "message",
        "role": "assistant",
        "model": str(payload.get("model") or requested_model),
        "content": content,
        "stop_reason": _stop_reason(
            choice.get("finish_reason") if isinstance(choice, dict) else None
        ),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
        },
    }


def anthropic_error(status: int, message: str) -> dict[str, Any]:
    error_type = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        429: "rate_limit_error",
    }.get(status, "api_error")
    return {"type": "error", "error": {"type": error_type, "message": message}}


def _sse(event: str, data: dict[str, Any]) -> bytes:
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {encoded}\n\n".encode()


class AnthropicStreamTranslator:
    """Turn OpenAI chat-completion chunks into Anthropic Messages SSE."""

    def __init__(self, requested_model: str) -> None:
        self.requested_model = requested_model
        self.message_id = "msg_grok"
        self.model = requested_model
        self.started = False
        self.finalized = False
        self.text_index: int | None = None
        self.tool_indexes: dict[int, int] = {}
        self.open_indexes: set[int] = set()
        self.next_index = 0
        self.usage = normalize_usage({})
        self.finish_reason: Any = None

    def _start_message(self, payload: dict[str, Any]) -> list[bytes]:
        if self.started:
            return []
        self.started = True
        self.message_id = _anthropic_id(payload.get("id"))
        self.model = str(payload.get("model") or self.requested_model)
        return [
            _sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {
                            "input_tokens": self.usage["input_tokens"],
                            "output_tokens": 0,
                        },
                    },
                },
            )
        ]

    def _close_blocks(self) -> list[bytes]:
        events = [
            _sse("content_block_stop", {"type": "content_block_stop", "index": index})
            for index in sorted(self.open_indexes)
        ]
        self.open_indexes.clear()
        return events

    def feed(self, payload: dict[str, Any]) -> list[bytes]:
        if payload.get("usage"):
            current = normalize_usage(payload["usage"])
            for key in self.usage:
                self.usage[key] = max(self.usage[key], current[key])
        events = self._start_message(payload)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return events
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        text = delta.get("content")
        if isinstance(text, str) and text:
            for target_index in sorted(self.tool_indexes.values()):
                if target_index in self.open_indexes:
                    events.append(
                        _sse(
                            "content_block_stop",
                            {"type": "content_block_stop", "index": target_index},
                        )
                    )
                    self.open_indexes.remove(target_index)
            if self.text_index is None or self.text_index not in self.open_indexes:
                self.text_index = self.next_index
                self.next_index += 1
                self.open_indexes.add(self.text_index)
                events.append(
                    _sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": self.text_index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                )
            events.append(
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": self.text_index,
                        "delta": {"type": "text_delta", "text": text},
                    },
                )
            )

        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            if self.text_index is not None and self.text_index in self.open_indexes:
                events.append(
                    _sse(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": self.text_index},
                    )
                )
                self.open_indexes.remove(self.text_index)
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                source_index = int(call.get("index") or 0)
                function = (
                    call.get("function")
                    if isinstance(call.get("function"), dict)
                    else {}
                )
                if source_index not in self.tool_indexes:
                    target_index = self.next_index
                    self.next_index += 1
                    self.tool_indexes[source_index] = target_index
                    self.open_indexes.add(target_index)
                    events.append(
                        _sse(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": target_index,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": str(
                                        call.get("id") or f"toolu_grok_{source_index}"
                                    ),
                                    "name": str(function.get("name") or ""),
                                    "input": {},
                                },
                            },
                        )
                    )
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    target_index = self.tool_indexes[source_index]
                    events.append(
                        _sse(
                            "content_block_delta",
                            {
                                "type": "content_block_delta",
                                "index": target_index,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": arguments,
                                },
                            },
                        )
                    )

        finish = choice.get("finish_reason")
        if finish:
            self.finish_reason = finish
            events.extend(self.finish())
        return events

    def finish(self) -> list[bytes]:
        if self.finalized:
            return []
        self.finalized = True
        events: list[bytes] = []
        if not self.started:
            events.extend(self._start_message({}))
        events.extend(self._close_blocks())
        events.append(
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": _stop_reason(self.finish_reason),
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": self.usage["output_tokens"]},
                },
            )
        )
        events.append(_sse("message_stop", {"type": "message_stop"}))
        return events
