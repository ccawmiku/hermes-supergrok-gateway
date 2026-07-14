from __future__ import annotations

import json

from supergrok_openai.anthropic_compat import (
    AnthropicStreamTranslator,
    anthropic_to_openai,
    openai_to_anthropic,
)


def test_anthropic_request_maps_system_tools_and_tool_results() -> None:
    converted = anthropic_to_openai(
        {
            "model": "grok-4.5",
            "max_tokens": 512,
            "system": [{"type": "text", "text": "Be concise."}],
            "messages": [
                {"role": "user", "content": "Weather?"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "weather",
                            "input": {"city": "SG"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "31 C",
                        },
                        {"type": "text", "text": "Summarize."},
                    ],
                },
            ],
            "tools": [
                {
                    "name": "weather",
                    "description": "Get weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                }
            ],
            "tool_choice": {"type": "any"},
        }
    )

    assert [message["role"] for message in converted["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert converted["messages"][2]["tool_calls"][0]["function"]["name"] == "weather"
    assert converted["messages"][3]["tool_call_id"] == "toolu_1"
    assert converted["tools"][0]["function"]["parameters"]["required"] == ["city"]
    assert converted["tool_choice"] == "required"


def test_openai_response_maps_to_anthropic_message() -> None:
    message = openai_to_anthropic(
        {
            "id": "chatcmpl-abc",
            "model": "grok-4.5",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Checking.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "weather",
                                    "arguments": '{"city":"SG"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27},
        },
        requested_model="grok-4.5",
    )

    assert message["id"] == "msg_abc"
    assert message["stop_reason"] == "tool_use"
    assert message["content"][0] == {"type": "text", "text": "Checking."}
    assert message["content"][1]["input"] == {"city": "SG"}
    assert message["usage"] == {"input_tokens": 20, "output_tokens": 7}


def test_stream_translator_emits_anthropic_sse_and_usage() -> None:
    translator = AnthropicStreamTranslator("grok-4.5")
    events = translator.feed(
        {
            "id": "chatcmpl-stream",
            "model": "grok-4.5",
            "choices": [{"delta": {"content": "Hi"}, "finish_reason": None}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 1, "total_tokens": 10},
        }
    )
    events += translator.feed(
        {
            "choices": [{"delta": {"content": "!"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        }
    )
    text = b"".join(events).decode()

    assert "event: message_start" in text
    assert text.count("event: content_block_delta") == 2
    assert "event: message_delta" in text
    assert "event: message_stop" in text
    assert translator.usage["input_tokens"] == 9
    assert translator.usage["output_tokens"] == 2
    assert (
        json.loads(text.split("data: ", 1)[1].split("\n", 1)[0])["type"]
        == "message_start"
    )
