# Upstream extraction notes

Source repository: `https://github.com/NousResearch/hermes-agent.git`

Inspected commit: `c7e09f2`
License: MIT

This project is a clean, small extraction rather than a copy of the Hermes package graph:

| Upstream area | Minimal counterpart |
| --- | --- |
| `hermes_cli/auth.py` xAI constants and device-code functions | `src/supergrok_openai/auth.py` |
| `hermes_cli/auth.py` JWT expiry and rotating refresh-token handling | `src/supergrok_openai/auth.py` |
| `hermes_cli/proxy/adapters/xai.py` endpoint allowlist | `src/supergrok_openai/server.py` |
| `hermes_cli/proxy/server.py` streaming credential-attaching forwarder | `src/supergrok_openai/server.py` |
| `agent/chat_completion_helpers.py` xAI tool-schema restrictions | `src/supergrok_openai/xai_compat.py` |
| `agent/codex_responses_adapter.py` cross-provider Responses handling | `src/supergrok_openai/xai_compat.py` |

Codex Responses tool compatibility was cross-checked against four independent
sources rather than inferred from individual xAI errors:

| Reference | Commit | License | Behavior verified |
| --- | --- | --- | --- |
| `openai/codex` | `4aa950d456c6` | Apache-2.0 | Canonical `namespace`, `custom_tool_call`, client `tool_search_call`, hosted web-search fields, and multi-agent wire models |
| `lidge-jun/opencodex` | `16bef043bb58` | MIT | Namespaced wire-name flattening and restoration, no-reasoning xAI model registry, plaintext `encrypted_content` handling, and custom/tool-search response restoration |
| `bharat2808/codex-ollama-proxy` | `dea454b000bb` | MIT | Deferred tools from `tool_search_output`, namespace history replay, and streaming event handling |
| `7as0nch/mimo2codex` | `5cb6f5c9506c` | MIT | Independent custom/namespace/tool-search request translation and strict-schema compatibility |

The implementation in `xai_compat.py` is an independent Python implementation.
No source code from those projects is copied. The public `/v1/responses` surface
keeps Codex/OpenAI item semantics; only the private xAI transport hop uses
flattened function tools.

xAI request behavior was also checked against the official xAI Web Search,
Function Calling, and Reasoning documentation. In particular, xAI's hosted web
search accepts the minimal `{"type":"web_search"}` declaration, function
continuations use `function_call_output`, and reasoning effort is model-specific.

Deliberately excluded: Agent runtime, tools, skills, memory, gateways, dashboard, cron, credential pools, fallback providers, Nous auth, model configuration, and all non-xAI integrations.
