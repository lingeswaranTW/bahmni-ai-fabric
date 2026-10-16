# Observability Phase 0 spike: results

Date: 2026-10-14. Scope: validate `docs/OBSERVABILITY.md` assumptions. **No application code changed**
(`git diff` on `src/`, `pyproject.toml`, `uv.lock` is empty); spike code and raw results are in `spike/observability/`.
Synthetic data only. **Phase 1 not started; waiting for review.**

## Environment
- Pinned and hardened per OBSERVABILITY.md 10.1: `ghcr.io/openlit/openlit:2.1.0`
  (`sha256:94552ccd…84fa8`), `clickhouse/clickhouse-server:24.4.1` (`sha256:2e6587b8…d5b01`), Docker context `std`.
- App side: `pydantic-ai-slim 2.55.0`, `mcp 2.3.0`, `fastmcp-slim 4.1.0`, `fastapi 0.143.0`,
  `opentelemetry-api/sdk/exporter-otlp-proto-http 1.45.1`. No `openlit.init()`, no OpenLIT SDK.
- Workload: the real `ChatService`, `McpRegistry`, `ServerToolset` policy and audit code, against real streamable-HTTP stub
  MCP servers (`tests/stubs.py`) and a scripted `FunctionModel`. The spans, MCP wire traffic and OTLP export are real.

## Summary

| Area | Result |
|---|---|
| OpenLIT + ClickHouse start, pinned, hardened | **Pass** |
| OTLP traces into ClickHouse | **Pass** (spans in process = rows stored, 63/63 in every run) |
| OTLP metrics | **Partial**: token usage and time-to-first-chunk arrive; **no model-call duration metric** is emitted |
| Content off: patient text and tool arguments absent | **Pass** |
| Content off: no secrets in spans | **Fail**: MCP tool error text leaks through the span status message |
| Prototype scrubbing exporter | **Pass** (zero canaries, content on or off) |
| MCP trace propagation: library switch exists | **No** (confirmed) |
| Per-server `propagateTrace` gate | **Pass** for tool calls, incl. 20 concurrent runs |
| Backend down or hanging | **Pass**: chat unaffected. **Issue**: shutdown blocks 13 to 19 s (fix verified) |
| A buggy span processor | **Fail**: exceptions reach the chat request and break it. A buggy exporter is safe |
| OpenLIT defaults | **Issues**: default login account, 30-day retention, derived tables |

## 1. OpenLIT and ClickHouse (pinned)
- Starts healthy with the hardened compose (`openlit/docker-compose.yml`). The OpenLIT image bundles its own OTLP collector
  (4317/4318) that writes to ClickHouse tables `otel_traces`, `otel_metrics_*`, `otel_logs`.
- Exposure: ClickHouse publishes nothing. UI and OTLP only on `127.0.0.1`; `192.168.64.5:3300/4318/8123` unreachable.
- `TELEMETRY_ENABLED=false` is set in the container. **Not verified:** whether OpenLIT still makes outbound calls.
- **Default account:** the image seeds `user@openlit.io` on every start (`prisma/seed.js`) with a fixed bcrypt hash; I
  confirmed the hash matches the password `openlituser` (published default). The login flow itself was not exercised.
  Every deployment has this known credential until changed. Needs a first-boot step: change the password, then restrict signup.
- Retention default is **730 h (30 days)** on all `otel_*` tables (`clickhouse-init.sh`, collector `ttl: 730h`).
  `ALTER TABLE otel_traces MODIFY TTL … 168 h` works. Not tested: whether it survives a container restart (init script uses `IF NOT EXISTS`, so it should).
- Deletion on request works (`ALTER TABLE … DELETE WHERE ServiceName=…`, `mutations_sync=2`: 819 rows to 0) but **derived
  tables are not cleaned**: `otel_traces_trace_id_ts` (TraceId plus time range, no content) kept all 487 rows. The deletion
  runbook must cover it, and the metric/log tables.

## 2. Traces and metrics (OTLP over HTTP to the OpenLIT collector)
- A 3-run scenario (read tool; write tool needing confirmation; confirm) produced 63 spans: `invoke_agent`, `chat`,
  `execute_tool`, `tools/call`, `MCP send …`, plus **~20 `server/discover` and `tools/list` spans per run before any tool
  runs**, because MCP connections are opened per run. Sampling or dropping discovery spans is worth doing in Phase 1.
- Attributes match what OpenLIT queries (I grepped its built app): `gen_ai.conversation.id`, `request.model`,
  `usage.input_tokens/output_tokens`, `agent.name`, `operation.name`, `tool.name`, `system`/`provider.name`.
  `gen_ai.usage.cost` is **not** emitted by Pydantic AI, so cost columns will be empty unless the backend computes it (unverified).
- Metrics: only `gen_ai.client.token.usage` (by input/output, model, provider) and
  `gen_ai.client.operation.time_to_first_chunk`, both stored in `otel_metrics_histogram`. **`gen_ai.client.operation.duration`
  is not emitted** (checked streaming and non-streaming). Model latency must come from span durations.
- **Not verified:** the OpenLIT UI itself (I checked storage and attribute compatibility, not screens), real providers
  (Anthropic, Ollama), and provider error spans.

## 3. Sensitive data (canaries: patient text, session id, bearer token, secret in an exception message)

| Run | Our spans (memory) | ClickHouse | Metrics | Audit records |
|---|---|---|---|---|
| content off, no scrub | secret leak (status message) | **secret leak** | clean | clean |
| content ON, no scrub (control) | patient text and secret | patient text and secret | clean | clean |
| content off, scrubber | clean | clean | clean | clean |
| content ON, scrubber | clean | clean | clean | clean |

- `include_content=False` removes prompts, answers, tool arguments and results: patient canary absent. Message structure
  (`{"type":"text"}` without text) remains.
- **Leak found.** With content off, `sekret-token-123` reached ClickHouse in the **status message of the fastmcp CLIENT
  span** `tools/call flaky_write` (`Error calling tool …: boom with Bearer sekret-token-123`). Cause: the text of an MCP
  server's error is copied into the span status. Our own redaction (`logging.redact_value`) never sees it. The MCP server
  controls this text. So `include_content=False` is **not sufficient**; the scrubbing exporter is required, not optional.
- The prototype (`lib.ScrubbingExporter`) keeps an attribute allowlist, drops exception message/stacktrace and the status
  description, and rebuilds the spans. Result: zero canaries in both stores in both content modes. Surviving attributes
  include `gen_ai.usage.*`, `gen_ai.request.model`, `gen_ai.tool.name`, `mcp.method.name`, `gen_ai.conversation.id`
  (a random id, not our conversation id). `gen_ai.tool.definitions` (tool schemas) is kept.
- `JSESSIONID`/`Cookie` never appeared (no header capture; no HTTP instrumentation was enabled). FastAPI instrumentation
  was **not** tested.
- The in-process stub servers also emit SERVER spans into the same provider, which contained the same error text. In
  production, a first-party MCP server sending traces to the same backend would do the same: **servers that receive
  trace context can put their own error text into the shared backend.** Another reason to gate propagation.

## 4. MCP trace-context propagation (what the MCP server actually receives)
Recorder middleware on the stub servers captured every JSON-RPC request.

| Scenario | `server/discover` | `tools/list` | `tools/call` |
|---|---|---|---|
| baseline (default) | `traceparent` in `_meta` on all | all | all (trusted and untrusted server) |
| `fastmcp.settings.telemetry_mode="off"` | all | all | all (**no effect**: the MCP SDK injects too) |
| `OTEL_PROPAGATORS=none` | none | none | none |
| **gate** (propagator + ContextVar, trusted=docs) | none | docs 2/4, jira 0 | docs 1/1, **jira 0/1** |

- Confirmed: `_meta` only; no HTTP `traceparent` header. No library option exists for per-server control.
- Gate: a wrapping global propagator that injects only when a ContextVar is true, set around a server's `get_tools` and
  `call_tool`. (c) the variable **is visible at the injection site**. (d) 20 concurrent mixed runs: trusted `tools/call` 10/10 carried
  context, untrusted 0/10, no bleed.
- Gate behaviour to note: discovery (`server/discover`, part of `tools/list`) is **not** propagated even for trusted servers
  because it happens in `registry.open`, outside the wrapper. That is the safe direction. Joined traces for trusted servers
  start at `tools/call`. Extending the gate to `registry.open` is possible if wanted.
- `OTEL_PROPAGATORS=none` is a valid launch default for "off everywhere". The gate is only needed when the first opt-in exists.

## 5. Failure isolation (40 chat turns each; audit expected 40)

| Backend state | OK turns | Audit records | Latency median / p95 | Shutdown |
|---|---|---|---|---|
| no telemetry (baseline) | 40 | 40 | 15.8 / 20.4 ms | n/a |
| healthy OpenLIT | 40 | 40 | 17.9 / 26.1 ms | 0 s |
| connection refused | 40 | 40 | 17.5 / 26.3 ms | **13.8 s** |
| accepts, never answers | 40 | 40 | 17.6 / 25.9 ms | **19.4 s** |
| real OpenLIT container stopped mid-test | 20 | 20 | 17.9 / 42.0 ms | 13.2 s |
| refused, exporter timeout 2 s | 40 | 40 | 17.5 / 20.2 ms | **2.3 s** |
| hang, exporter timeout 2 s | 40 | 40 | 18.1 / 25.9 ms | **5.0 s** |
| exporter raises | 40 | 40 | 17.4 / 19.9 ms | 0 s |
| **span processor raises (on_start, on_end, or both)** | **0** | **0** | 570 to 696 ms | 0 s |

- Backend outage never affected requests, SSE completion, or audit. After OpenLIT restarted, the next run exported again
  (+460 rows for 20 turns). Memory grew ~20 MB over 40 turns in all modes including baseline (no leak signal from the queue).
- **Shutdown can block 13 to 19 s** when the backend is down or hanging (default 10 s exporter timeout plus retries).
  Compose's default stop grace is 10 s, so the container would be killed. Setting the exporter timeout to 2 s bounds it
  to 2 to 5 s. Phase 1 should set a low `OTEL_EXPORTER_OTLP_TIMEOUT` and bound `shutdown()` explicitly.
- **A custom `SpanProcessor` that raises breaks the chat request** (`internal_error` on every turn, no tool runs, no audit
  record). The SDK does not isolate processor callbacks. The facade's `try/except` does not cover this. A buggy
  **exporter** is isolated (it runs on the batch thread).

## 6. Issues and proposed changes to the design (for review)
1. **Scrubbing is mandatory** (finding 3): ship the exporter wrapper in Phase 1, not Phase 3, with the status-description and exception rules, plus canary tests that include a server error containing a secret.
2. **No custom SpanProcessor logic.** Put all redaction in the exporter wrapper. If any processor is ever needed, wrap its `on_start/on_end` in `try/except`. Add the "raising processor" test from this spike to the suite.
3. **Bound shutdown and export time:** `OTEL_EXPORTER_OTLP_TIMEOUT` about 2 s, explicit shutdown timeout in the lifespan.
4. **Propagation:** launch with `OTEL_PROPAGATORS=none`; build `propagateTrace` when the first server needs it (design verified). This reverses the earlier decision to build it now; either is workable, your call.
5. **Model latency:** use span durations; do not plan on a `gen_ai.client.operation.duration` metric. Cost needs the backend.
6. **Per-run discovery spans:** drop or sample `server/discover` and `tools/list` spans to cut volume (about 20 per run here).
7. **OpenLIT deployment runbook:** change the seeded `user@openlit.io` credential on first boot; set retention (TTL) per the governance decision; include `otel_traces_trace_id_ts` and metric tables in deletion; keep `TELEMETRY_ENABLED=false`.

## 7. Not tested or caveats
- OpenLIT UI screens; outbound traffic of the OpenLIT container; TTL after restart; default-login flow.
- Real model providers; Pydantic AI exception text for provider/model errors; HTTP/SSE path (`ChatService` was driven directly, so FastAPI instrumentation and span parenting across the SSE generator are untested).
- No OTel Collector in between (direct OTLP/HTTP to OpenLIT's bundled collector); gRPC not used.
- Stub MCP servers run in-process, so their server spans share the provider; wire-level propagation results are unaffected.
- Latency numbers use a scripted model and local stubs; they show overhead differences, not production latency.
- The scrubber is a prototype in `spike/`, not production code.

## 8. Follow-ups from Phase 0b (found while preparing Phase 1)
- The default login `user@openlit.io` / `openlituser` was exercised: it logged in. **OpenLIT also allows open self-registration**
  (a registered user lands in an empty organisation of their own and could not read the default organisation's data, but accounts can
  be created by anyone who reaches the UI). Both are addressed in `deploy/openlit/` (see OBSERVABILITY.md section 11).
- A failed MCP connection leaves a span with status UNSET, so error-only discovery filtering needed our own failure span (done in Phase 1).

## 9. State left behind
The Phase 0 spike stack was removed (`down -v`). Phase 1 results and the productised OpenLIT deployment are described in
`docs/OBSERVABILITY.md` section 11 and `deploy/openlit/README.md`.
