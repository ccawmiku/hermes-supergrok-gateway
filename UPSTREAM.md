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

Deliberately excluded: Agent runtime, tools, skills, memory, gateways, dashboard, cron, credential pools, fallback providers, Nous auth, model configuration, and all non-xAI integrations.
