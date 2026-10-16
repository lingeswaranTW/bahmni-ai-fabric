# Observability audit (final, end to end)

Scope: OpenTelemetry + OpenLIT integration (`src/bahmni_ai/observability/`, call sites in `agent/service.py`,
`mcp/policy.py`, `mcp/registry.py`, `main.py`). Method: read the code, query the live ClickHouse behind the pinned OpenLIT
2.1.0 (57 spans, 14 traces from the dev stack), inspect the OpenLIT bundle, and run mocked-model / stub-MCP tests.
**Verified** = observed in live data or by a test. **Assumed** = reasoned, not observed.

Findings below were written before any application change. Section 4 records what was changed afterwards.

## 1. Findings (before changes)

| # | Requirement | Result | Evidence |
|---|---|---|---|
| 1 | Parent-child structure | PASS | Live: 0 orphan spans, exactly 1 root per trace. Tree: `POST /bahmni-ai/chat/confirm` → `invoke_agent` → {`chat <model>`, `execute_tool <t>` → `tools/call` → `MCP send tools/call`}. |
| 2 | No duplicate spans | PARTIAL | Each library layer emits its own client span: FastMCP `tools/call X` wraps the MCP SDK's `MCP send tools/call X`. Nested, not orphaned, but two rows per tool call. Library behaviour; kept (hiding one would hide the layer that records the failure). |
| 3 | Session correlation | **FAIL** | `gen_ai.conversation.id` is on `invoke_agent`, `chat` and `execute_tool` (live: 13/13, 11/11, 6/6) but **not on the HTTP root span** (0/10) or MCP spans (0). Searching the root row (the one OpenLIT lists first) by session finds nothing. No `session.id` attribute exists, and the scrubber allowlist would drop one. |
| 4 | OpenLIT native sessions | PARTIAL (verified from bundle) | OpenLIT 2.1.0 reads `gen_ai.conversation.id`, then `coding_agent.session.id`, then `session.id` for its **coding-agents** pages only (`/api/coding-agents/sessions`); its general LLM queries explicitly exclude spans that carry `coding_agent.*`. No general chat "Sessions" screen exists. Attributes give filtering and SQL grouping, not a Sessions UI. **Assumed:** the coding-agents page does not list our spans (not opened in a browser). |
| 5 | Trace boundaries | PASS | Each HTTP request is its own trace (14 traces, confirm turn is its own trace); turns of a conversation are not merged. |
| 6 | LLM observability | PASS | `chat` spans carry provider, request/response model, response id, finish reasons, input/output/cache tokens, time-to-first-chunk; `gen_ai.client.token.usage` metric present. **Gap:** no `gen_ai.client.operation.duration` metric (not emitted by Pydantic AI 2.55 format v5; duration is the span). |
| 7 | Retries / errors (LLM) | PARTIAL | Provider errors raise `ModelHTTPError`; our handler returns a safe error but no test proved the `chat` span is ERROR with the exception text scrubbed. Library-level HTTP retries are not instrumented (**assumed** none configured). |
| 8 | Tool failures visible | **FAIL** | Policy returns `{"error": ...}` dicts for `tool_failed`, `timeout`, `invalid_arguments`, `permission_denied`. The `execute_tool` span stays UNSET for all of them, so a failed or denied tool call looks successful in OpenLIT. Only MCP transport exceptions mark a span ERROR (live: 4 error spans, all transport). |
| 9 | Metrics | PARTIAL | Present: `gen_ai.client.token.usage`, `gen_ai.client.operation.time_to_first_chunk`, `http.server.*`, `bahmni_ai.mcp.unavailable`. Missing: tool-call counter by outcome (Phase 2 item). |
| 10 | Logs | PARTIAL (by design) | stdout JSON logs carry `trace_id`/`span_id` (tested). Nothing is exported over OTLP (`otel_logs` has 0 rows); correlation is by id only. |
| 11 | Exceptions | PARTIAL | Transport exceptions keep `exception.type` with text removed (live). Tool-level failures: see 8. |
| 12 | Privacy | PASS | Allowlist scrubber, status text and exception messages dropped, no canary strings in ClickHouse (earlier runs and tests). Content capture is on in the dev stack only, deliberately. |
| 13 | Overhead | PASS | Export on the batch thread, 2 s export timeout, bounded shutdown, discovery spans filtered (18 → 8 spans per turn). **Assumed:** no load test run. |

## 2. Root causes

- **3:** the session id is only supplied to `Agent.run_stream_events`, which Pydantic AI applies to its own spans. The FastAPI
  server span and MCP library spans are created elsewhere and nothing tags them.
- **8:** the policy converts failures into model-visible results instead of exceptions, so the tracing layer never sees an error.

## 3. Plan

1. `observability.session_id(cid, stored)` always returns a value when telemetry is on: our mode's value, else the id Pydantic AI
   already stored in history, else a fresh random id. `observability.tag_session(sid)` sets `session.id` and
   `gen_ai.conversation.id` on the active (HTTP root) span. Scrubber allows `session.`. Default stays anonymous.
2. `observability.record_tool_outcome(decision, outcome)` called from `ServerToolset._audit` (the single place every outcome
   passes): sets `bahmni.tool.decision` / `bahmni.tool.outcome` on the `execute_tool` span, ERROR for `timeout` and `error`,
   and increments a `bahmni_ai.tool.calls` counter.
3. `observability.record_chat_error(code)` (called from `ChatService._observed` for every `error` event): marks the request span
   ERROR with `bahmni.error.code` and counts `bahmni_ai.chat.errors`. Added after a test showed the provider-failure gap (row 7).
4. Tests with synthetic data, mocked models and stub MCP servers for each row marked FAIL or PARTIAL.
5. Not changed: duplicate MCP client spans (library), OTLP log export, `gen_ai.client.operation.duration`.

## 4. Changes and final results

Code: `observability/__init__.py` + `_otel.py` (`session_id(cid, stored)`, `tag_session`, `record_tool_outcome`,
`record_chat_error`), `observability/scrub.py` (allow `session.id`), `agent/service.py` (session per turn, `_observed`),
`mcp/policy.py` (`_audit` reports the outcome). Tests: section 10 of `tests/test_observability.py` (mocked models, stub MCP
servers, synthetic canaries; no provider calls). Full suite: 107 passed, 3 skipped; ruff and mypy clean.

| # | Requirement | Final | Evidence |
|---|---|---|---|
| 1 | Parent-child structure | PASS | `test_each_turn_is_one_connected_trace_…`: one root, no orphans, no repeated span ids; agent under HTTP root, tool under agent, MCP call under tool, LLM spans under agent. Live ClickHouse: 0 orphans. |
| 2 | No duplicate spans | PASS (note) | One `invoke_agent` per turn, one `execute_tool` per call. FastMCP and MCP SDK each emit a nested client span per call (library, kept). |
| 3 | Session correlation | PASS | `test_every_turn_root_span_carries_the_conversation_session`: same conversation → same `session.id` across turns, other conversation → different, equal to `gen_ai.conversation.id` on agent/LLM spans, anonymous by default; `raw` follows the UI id. |
| 4 | OpenLIT native sessions | PARTIAL | No general Sessions UI in 2.1.0 (coding-agents pages only, read from the bundle). Attributes are on every turn's root, agent, LLM and tool spans; grouping is by filter/SQL (README query). **Not verified in a browser.** |
| 5 | Trace boundaries | PASS | Three requests → three traces. With `propagateTrace` off, a stub server's own span starts a separate trace (asserted). |
| 6 | LLM observability | PASS | Asserted: model, provider, input/output tokens on each `chat` span. Live data also shows response model, finish reason, cache tokens, time-to-first-chunk. |
| 7 | Retries and errors (LLM) | PARTIAL | The application has no retry layer (grep). Provider errors: `chat` span stays UNSET (**verified in Pydantic AI source**: `set_status_on_exception=False`), now visible on `invoke_agent` (ERROR + exception type) and the request span (ERROR + `bahmni.error.code`); provider text never exported (asserted). Real-provider behaviour is still a follow-up (11.3). |
| 8 | Tool failures visible | PASS | `execute_tool` ERROR + `bahmni.tool.outcome=error` for a failing tool; denial labelled `denied`, not ERROR; error text not exported. |
| 9 | Metrics | PARTIAL | Added `bahmni_ai.tool.calls{server,tool,decision,outcome}` (asserted) and `bahmni_ai.chat.errors`. Still no `gen_ai.client.operation.duration` (not emitted by the library version). |
| 10 | Logs | PARTIAL (by design) | JSON logs carry `trace_id`/`span_id`; no OTLP log export. |
| 11 | Exceptions | PASS | Exception type kept, message dropped, on agent and MCP spans (live and tests). |
| 12 | Privacy | PASS | Canary secret and session id absent from spans and metric attributes in all new tests; allowlist unchanged apart from `session.id`. |
| 13 | Overhead | PARTIAL | Export off the request path, bounded timeouts, discovery filtered. **No load measurement was made.** |

## 5. Not verified / still to do

- The running dev container predates these changes; rebuild to see `session.id`, tool outcomes and the new counters in OpenLIT.
- OpenLIT screens (list filter on `session.id`, coding-agents page) and real providers (Anthropic, Ollama) were not exercised.
- Overhead under load; HTTP/SSE through the proxy (see OBSERVABILITY.md 11.3).
