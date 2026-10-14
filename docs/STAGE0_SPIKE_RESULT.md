# Stage 0 – Spike result: **GO for Pydantic AI**

Versions tested: `pydantic-ai-slim 2.55.0`, `mcp 2.3.0`, `fastmcp-slim 4.1.0`, Python 3.13 (project supports >=3.11).
Tests: `spike/test_spike.py` (5 passing, real streamable-HTTP stub MCP server).

| Requirement | Result |
|---|---|
| Per-user credentials without sharing a session | `MCPToolset(url, headers={...})` built **per run**. 8 overlapping runs each saw only their own `X-OpenMRS-Session-Id`. |
| Namespacing | `MCPToolset(...).prefixed("jira")` gives `jira_create_issue`. Planned: our own `server__tool` naming in the registry (see PLAN). |
| Approval / pause for writes | `ApprovalRequiredToolset` + `output_type=[str, DeferredToolRequests]`; resume with `DeferredToolResults`. Works. |
| Interception | `MCPToolset(process_tool_call=...)` can veto a call (raised error propagates with `tool_error_behavior="error"`). |
| Provider switch | `provider:model` string resolves OpenAI, Anthropic, Ollama with no code change. |
| Cancellation | Cancelling a run mid-tool is clean; a later run on the same server works. |

Notes / constraints found
- The class is `MCPToolset` (client = URL / transport / `fastmcp.Client`), not `MCPServerStreamableHTTP`.
- `load_mcp_toolsets()` expands any env var and can launch commands: **not used**. Our own validated loader is used instead.
- MCP sampling/elicitation are off unless handlers are passed; we never pass them.
- Shared toolsets hold one session, so they are only used for service credentials.
