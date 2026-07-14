import json

from supergrok_openai.xai_compat import (
    XaiSseTransformer,
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


def test_codex_tool_variants_are_bridged_bidirectionally() -> None:
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
                },
                {
                    "type": "namespace",
                    "name": "collaboration",
                    "description": "Team tools.",
                    "tools": [
                        {
                            "type": "function",
                            "name": "spawn_agent",
                            "description": "Start a sub-agent.",
                            "parameters": {
                                "type": "object",
                                "properties": {"task": {"type": "string"}},
                                "required": ["task"],
                            },
                        }
                    ],
                },
                {
                    "type": "tool_search",
                    "description": "Find deferred tools.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "number"},
                        },
                        "required": ["query"],
                    },
                },
            ],
            "tool_choice": {
                "type": "allowed_tools",
                "mode": "required",
                "tools": [
                    {
                        "type": "function",
                        "namespace": "collaboration",
                        "name": "spawn_agent",
                    },
                    {"type": "custom", "name": "apply_patch"},
                    {"type": "tool_search"},
                ],
            },
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
                {
                    "type": "function_call",
                    "id": "fc_history",
                    "call_id": "call_history",
                    "namespace": "collaboration",
                    "name": "spawn_agent",
                    "arguments": '{"task":"review"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_history",
                    "output": "started",
                },
                {
                    "type": "tool_search_call",
                    "id": "tsc_1",
                    "call_id": "search_1",
                    "execution": "client",
                    "arguments": {"query": "browser", "limit": 2},
                },
                {
                    "type": "tool_search_output",
                    "call_id": "search_1",
                    "status": "completed",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "browser",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "open",
                                    "parameters": {"type": "object"},
                                }
                            ],
                        }
                    ],
                },
            ],
        },
        endpoint="/responses",
        available_models=MODELS,
    )

    assert {tool["type"] for tool in payload["tools"]} == {"function"}
    tools = {tool["name"]: tool for tool in payload["tools"]}
    assert set(tools) == {
        "apply_patch",
        "collaboration__spawn_agent",
        "tool_search",
    }
    assert tools["apply_patch"]["parameters"]["required"] == ["input"]
    assert payload["tool_choice"] == "required"
    assert payload["input"][0]["type"] == "function_call"
    assert json.loads(payload["input"][0]["arguments"]) == {"input": patch}
    assert payload["input"][1]["type"] == "function_call_output"
    assert payload["input"][2]["name"] == "collaboration__spawn_agent"
    assert "namespace" not in payload["input"][2]
    assert payload["input"][4]["type"] == "function_call"
    assert payload["input"][4]["name"] == "tool_search"
    assert payload["input"][5]["type"] == "function_call_output"
    assert "browser__open" in payload["input"][5]["output"]
    assert adaptation.tool_bridge.custom_tool_names == ("apply_patch",)
    assert adaptation.tool_bridge.tool_search is True

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
        upstream_event, adaptation.tool_bridge
    )
    assert transformed["item"]["type"] == "custom_tool_call"
    assert transformed["item"]["input"] == patch
    assert "arguments" not in transformed["item"]

    line = b"data: " + json.dumps(upstream_event).encode() + b"\r\n"
    rewritten = transform_xai_sse_line(line, adaptation.tool_bridge)
    assert rewritten.endswith(b"\r\n")
    event = json.loads(rewritten[6:].strip())
    assert event["item"]["type"] == "custom_tool_call"
    assert event["item"]["input"] == patch

    namespace_event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "id": "fc_ns",
            "call_id": "call_ns",
            "name": "collaboration__spawn_agent",
            "arguments": '{"task":"review"}',
            "status": "completed",
        },
    }
    namespaced = transform_xai_response_payload(
        namespace_event, adaptation.tool_bridge
    )["item"]
    assert namespaced["type"] == "function_call"
    assert namespaced["namespace"] == "collaboration"
    assert namespaced["name"] == "spawn_agent"

    search_event = {
        "type": "response.output_item.done",
        "item": {
            "type": "function_call",
            "id": "fc_search",
            "call_id": "call_search",
            "name": "tool_search",
            "arguments": '{"query":"browser","limit":"2"}',
            "status": "completed",
        },
    }
    search = transform_xai_response_payload(search_event, adaptation.tool_bridge)["item"]
    assert search["type"] == "tool_search_call"
    assert search["execution"] == "client"
    assert search["arguments"] == {"query": "browser", "limit": 2}
    assert "name" not in search


def test_sse_drops_incompatible_deltas_after_non_function_item_rewrite() -> None:
    _, adaptation = adapt_xai_payload(
        {
            "model": "gpt-5-codex",
            "tools": [{"type": "custom", "name": "apply_patch"}],
            "input": "edit",
        },
        endpoint="/responses",
        available_models=MODELS,
    )
    transformer = XaiSseTransformer(adaptation.tool_bridge)
    added = {
        "type": "response.output_item.added",
        "item": {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "apply_patch",
            "arguments": "",
        },
    }
    rewritten = transformer.transform_line(
        b"data: " + json.dumps(added).encode() + b"\n"
    )
    assert json.loads(rewritten[6:])["item"]["type"] == "custom_tool_call"
    delta = {
        "type": "response.function_call_arguments.delta",
        "item_id": "fc_1",
        "call_id": "call_1",
        "delta": '{"input":"*** Begin',
    }
    assert transformer.transform_line(
        b"data: " + json.dumps(delta).encode() + b"\n"
    ) == b""


def test_xai_never_receives_unsupported_codex_tool_enums() -> None:
    payload, _ = adapt_xai_payload(
        {
            "model": "gpt-5-codex",
            "input": "hello",
            "tools": [
                {"type": "custom", "name": "apply_patch"},
                {
                    "type": "namespace",
                    "name": "apps",
                    "tools": [
                        {"type": "function", "name": "read", "parameters": {}}
                    ],
                },
                {"type": "tool_search"},
                {"type": "web_search_preview"},
                {"type": "future_client_tool", "name": "future", "parameters": {}},
                {"type": "unknown_without_name"},
            ],
        },
        endpoint="/responses",
        available_models=MODELS,
    )
    allowed = {
        "function", "web_search", "x_search", "image_generation",
        "collections_search", "file_search", "code_execution",
        "code_interpreter", "mcp", "shell",
    }
    assert {tool["type"] for tool in payload["tools"]} <= allowed
    assert {tool.get("name") for tool in payload["tools"] if tool["type"] == "function"} == {
        "apply_patch", "apps__read", "tool_search", "future"
    }
