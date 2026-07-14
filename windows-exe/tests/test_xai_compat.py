import json

from supergrok_openai.xai_compat import (
    adapt_xai_payload,
    resolve_xai_model,
    transform_xai_response_payload,
    transform_xai_sse_line,
)


MODELS = ["grok-build-0.1", "grok-composer-2.5-fast"]


def test_foreign_model_aliases_use_hermes_default() -> None:
    assert resolve_xai_model("claude-sonnet-5", MODELS) == "grok-build-0.1"
    assert resolve_xai_model("gpt-5.6-sol", MODELS) == "grok-build-0.1"
    assert resolve_xai_model("grok-composer-2.5-fast", MODELS) == (
        "grok-composer-2.5-fast"
    )


def test_codex_responses_payload_is_sanitized_for_xai() -> None:
    payload, adaptation = adapt_xai_payload(
        {
            "model": "gpt-5.6-sol",
            "input": [
                {"role": "user", "content": "hello"},
                {
                    "type": "reasoning",
                    "encrypted_content": "sealed-by-openai",
                    "_issuer_kind": "openai_responses",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "inspect",
                    "output_schema": {"type": "object"},
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "format": "path"},
                            "model": {
                                "type": "string",
                                "pattern": "^[a-z]+$",
                                "enum": ["Qwen/Qwen3.5", "other"],
                            },
                        },
                    },
                }
            ],
            "include": ["reasoning.encrypted_content"],
            "text": {"verbosity": "low"},
            "reasoning": {"effort": "high", "context": "all_turns"},
            "client_metadata": {"source": "codex"},
            "stream_options": {"reasoning_summary_delivery": "sequential_cutoff"},
        },
        endpoint="/responses",
        available_models=MODELS,
    )

    assert payload["model"] == "grok-build-0.1"
    assert payload["input"] == [{"role": "user", "content": "hello"}]
    assert "include" not in payload
    assert "text" not in payload
    assert payload["reasoning"] == {"effort": "high"}
    assert "client_metadata" not in payload
    assert "stream_options" not in payload
    tool = payload["tools"][0]
    assert "output_schema" not in tool
    path_schema = tool["parameters"]["properties"]["path"]
    model_schema = tool["parameters"]["properties"]["model"]
    assert "format" not in path_schema
    assert "pattern" not in model_schema
    assert "enum" not in model_schema
    assert adaptation.model_was_mapped is True


def test_chat_completions_nested_function_schema_is_sanitized() -> None:
    payload, _ = adapt_xai_payload(
        {
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": "hello"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "open_file",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "format": "path"}
                            },
                        },
                    },
                }
            ],
        },
        endpoint="/chat/completions",
        available_models=MODELS,
    )

    assert payload["model"] == "grok-build-0.1"
    schema = payload["tools"][0]["function"]["parameters"]
    assert "format" not in schema["properties"]["path"]


def test_codex_custom_tools_are_bridged_bidirectionally() -> None:
    patch = "*** Begin Patch\n*** Add File: hello.txt\n+hello\n*** End Patch"
    payload, adaptation = adapt_xai_payload(
        {
            "model": "gpt-5.6-codex",
            "tools": [
                {
                    "type": "custom",
                    "name": "apply_patch",
                    "description": "Apply a patch as raw text.",
                    "format": {
                        "type": "grammar",
                        "syntax": "lark",
                        "definition": "start: PATCH",
                    },
                }
            ],
            "tool_choice": {"type": "custom", "name": "apply_patch"},
            "input": [
                {
                    "type": "custom_tool_call",
                    "id": "ctc_1",
                    "call_id": "call_1",
                    "name": "apply_patch",
                    "input": patch,
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_1",
                    "name": "apply_patch",
                    "output": "Done!",
                },
            ],
        },
        endpoint="/responses",
        available_models=MODELS,
    )

    tool = payload["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == "apply_patch"
    assert tool["parameters"]["required"] == ["input"]
    assert "format" not in tool
    assert payload["tool_choice"] == {"type": "function", "name": "apply_patch"}
    assert payload["input"][0]["type"] == "function_call"
    assert json.loads(payload["input"][0]["arguments"]) == {"input": patch}
    assert payload["input"][1]["type"] == "function_call_output"
    assert adaptation.custom_tool_names == ("apply_patch",)

    upstream_event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_2",
            "name": "apply_patch",
            "arguments": json.dumps({"input": patch}),
            "status": "completed",
        },
    }
    transformed = transform_xai_response_payload(
        upstream_event, adaptation.custom_tool_names
    )
    assert transformed["item"]["type"] == "custom_tool_call"
    assert transformed["item"]["input"] == patch
    assert "arguments" not in transformed["item"]

    line = b"data: " + json.dumps(upstream_event).encode() + b"\r\n"
    rewritten = transform_xai_sse_line(line, adaptation.custom_tool_names)
    assert rewritten.endswith(b"\r\n")
    event = json.loads(rewritten[6:].strip())
    assert event["item"]["type"] == "custom_tool_call"
    assert event["item"]["input"] == patch
