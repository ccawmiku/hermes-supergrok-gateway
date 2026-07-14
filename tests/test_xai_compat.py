from supergrok_openai.xai_compat import adapt_xai_payload, resolve_xai_model


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
