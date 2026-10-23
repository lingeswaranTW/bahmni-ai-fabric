# Observability design (OpenTelemetry, OpenLIT optional)

Status: Phase 0 spike done (`OBSERVABILITY_PHASE0_RESULT.md`); design revised after it, Phase 1 in progress.
Decisions are in section 10, the revised Phase 1 plan and task list in section 11. Written against the code as of this commit
(`pydantic-ai-slim 2.55`, `mcp 2.3`, `fastapi 0.143`, `opentelemetry-api 1.45` already installed transitively).

## 1. Findings from the repository

| Fact | Where | Consequence |
|---|---|---|
| Model calls go through one `Agent`, built once | `agent/factory.py` | One place to switch instrumentation on. |
| Every tool call passes through one wrapper with a defined decision order | `mcp/policy.py` `ServerToolset.call_tool` | One place for tool spans. Already computes server, tool, access, decision, outcome, latency. |
| Audit is a separate `AuditSink` Protocol writing a structured log | `audit/log.py`, injected in `main.py` | The audit seam already exists. Telemetry must not reuse or replace it. |
| MCP connections are opened per chat run | `mcp/registry.py` `open()` | Natural place for connect-failure metrics (`unavailable`). |
| Errors are mapped to SSE error codes in one `except` ladder | `agent/service.py` `_run` | One place for an error counter by code. |
| Logging already redacts by key and pattern (`JSESSIONID`, bearer, `token`, ...) | `logging.py` | Reusable redaction rules. Spans need their own scrubbing (logs and spans are separate pipelines). |
| Streaming: the HTTP handler returns an `EventSourceResponse` over an async generator | `api/chat.py` `_stream` | The FastAPI server span may end before the stream does. The chat turn needs its own root span. |
| No OpenTelemetry SDK, exporter or OpenLIT is installed | `pyproject.toml`, `uv.lock` | Telemetry must be an optional extra. |

Key discovery: **every layer already speaks OpenTelemetry (OTel) natively.**

- **Pydantic AI** emits agent run, model request and tool-execution spans following the OTel GenAI conventions
  (`Agent.instrument_all()` or `Agent(instrument=InstrumentationSettings(...))`). `InstrumentationSettings` has
  `include_content` (prompts, completions, tool arguments and results) and `include_binary_content`.
- **MCP Python SDK** creates client spans (`mcp/shared/_otel.py`, tracer `mcp-python-sdk`) and **injects W3C
  `traceparent`/`tracestate` into the request `_meta`** of every outgoing request, so trace context is sent to every
  MCP server, including third-party ones. Verified behaviour is in section 5.1.
- **FastAPI/ASGI** has the standard `opentelemetry-instrumentation-fastapi`.

So OTel itself is the vendor-neutral interface. OpenLIT is one *consumer* of OTLP data (and also an SDK that
monkey-patches provider libraries). We do not need OpenLIT inside the process to get traces, tokens and latency.

## 2. Alternatives

| Option | Verdict |
|---|---|
| A. `openlit.init()` in the app | Fastest start, but patches provider SDKs (Pydantic AI, MCP, OpenAI, Anthropic, Ollama instrumentations all exist), so spans may double up with Pydantic AI's own. `capture_message_content` defaults to **True** per its [PyPI page](https://pypi.org/project/openlit/): unsafe default for clinical data. Couples app startup to one vendor. Keep only as an optional adapter. |
| B. Pydantic Logfire | Same OTel underneath, but a vendor SDK and SaaS-first. No. |
| C. **Pydantic AI + MCP + FastAPI native OTel, OTel SDK, OTLP exporter; OpenLIT (or anything else) as backend** | **Recommended.** No vendor code in the app. Backend swap = change `OTEL_EXPORTER_OTLP_ENDPOINT`. |
| D. Our own `Telemetry` interface wrapping tracer, meter and each backend | Rejected as over-abstraction: it re-implements OTel, which is already the abstraction. |
| E. Logs only (structlog already exists) | Keep as a complement (trace ids in logs), but no latency breakdown or token metrics. |

## 3. Recommended architecture

```
 FastAPI ──┐                                   ┌── OTLP/HTTP ──> OTel Collector (optional) ──> OpenLIT | Jaeger | Grafana | ...
 Pydantic AI (native OTel, content OFF) ──┐    │
 MCP SDK (native OTel, traceparent) ──────┼─> OTel SDK (TracerProvider, MeterProvider, BatchSpanProcessor)
 bahmni_ai.observability facade ──────────┘         └── scrubbing SpanExporter wrapper (allowlist, secrets)

 AuditSink (log)  ── completely separate path, unchanged ──> stdout JSON
```

### 3.1 The internal interface (kept deliberately tiny)

New package `src/bahmni_ai/observability/`; **the only place that imports `opentelemetry`**.

```python
# observability/__init__.py  (public surface)
def setup(settings) -> Shutdown                    # once, from create_app; no-op unless TELEMETRY_ENABLED
def instrumentation_for_agent() -> InstrumentationSettings | bool   # used by agent/factory.py (content OFF by default)
def propagation_scope(allowed: bool) -> ContextManager              # per-server trace-context gate (5.1)
def current_trace_ids() -> dict[str, str]          # log correlation only
# Phase 2 (domain signals): span(), record_tool_call(), record_error(), record_mcp_unavailable()
```

Rules:

1. Facade functions **never raise** (wrap in `try/except Exception`) and never return anything the caller branches on.
   Telemetry is write-only: it cannot influence an authorization, confirmation or audit outcome.
   **Phase 0 showed this is not enough:** the OTel SDK does not isolate `SpanProcessor` callbacks, so a processor that raises
   breaks the request (see 5.2). Therefore: no custom processor logic, every processor we add is wrapped in `SafeSpanProcessor`, and all
   scrubbing/filtering happens in the exporter, which runs on the batch thread.
2. When disabled or the extra is not installed, every function is a no-op (the OTel API itself is a no-op without an SDK).
3. Business modules (`agent/`, `mcp/`, `api/`, `auth/`) import `bahmni_ai.observability` only, never `opentelemetry` or `openlit`.
   Enforced by a test (section 7).
4. Backends (OpenLIT, Jaeger, ...) never appear in code. At most one optional adapter module
   `observability/openlit_adapter.py`, loaded only when `TELEMETRY_BACKEND=openlit-sdk`, for people who want OpenLIT's
   SDK features (cost tables, guardrails). It must be called with content capture off.

### 3.2 Call sites (small, additive)

| File | Change |
|---|---|
| `main.py` `create_app` | `observability.setup(settings)`; FastAPI instrumentation excluding `/bahmni-ai/health`. |
| `agent/factory.py` | pass `instrument=observability.instrumentation_for_agent()` (`include_content=False`, `include_binary_content=False`). |
| `agent/service.py` `_run` | root span `bahmni_ai.chat_turn` (attributes: conversation id hash, `confirmation=true/false`); `record_error(code)` next to each `ErrorEvent`. |
| `mcp/policy.py` `_audit` | one extra line next to the audit call: `record_tool_call(...)`. Audit call stays first and unconditional. |
| `mcp/registry.py` `open` | `record_mcp_unavailable(name)` in the failure branch; connect span. |
| `logging.py` | processor adding `trace_id` and `span_id` to log lines and audit records, for correlation only. |

### 3.3 What is captured

- **Traces:** chat turn → agent run → model request(s) → tool execution → MCP client call (→ MCP server span if it is
  also instrumented, via `traceparent`). Our attributes: `bahmni.server`, `bahmni.tool`, `bahmni.access`,
  `bahmni.decision`, `bahmni.outcome`, `bahmni.request_id`. Errors: exception **type** only.
- **Tokens:** `gen_ai.usage.input_tokens/output_tokens` on model spans and the `gen_ai.client.token.usage` metric from
  Pydantic AI, labelled by provider and model. Works the same for Anthropic, OpenAI and Ollama (providers that omit usage give zero).
  Cost: left to the backend (OpenLIT has pricing tables); not computed in the app.
- **Latency:** model request duration (`gen_ai.client.operation.duration`), tool latency histogram, chat turn duration, time to first text delta.
- **Metrics (low cardinality only):** `bahmni_ai.tool.calls{server,tool,decision,outcome}`, `bahmni_ai.tool.duration`,
  `bahmni_ai.mcp.unavailable{server}`, `bahmni_ai.chat.errors{code}`, `bahmni_ai.confirmations{state=created|approved|rejected|expired}`.
  No user, conversation or argument labels.
- **Async:** `BatchSpanProcessor` (bounded queue, drops on overflow, exports on a background thread), so a slow or dead
  collector cannot block requests or SSE streams. Context is carried by `contextvars`; the chat turn span is started
  inside the generator (`_run`), so it parents correctly even after the HTTP span has ended. `confirm` starts a new turn
  span with a **link** to the original turn's span context, stored with the pending confirmation (no parent/child across a user pause).

## 4. Configuration (Bahmni conventions)

Environment variables only, passed through compose `environment:` like the other `bahmni-ai` settings; defaults in
`bahmni-standard/.env`. The telemetry *destination* is per-deployment infrastructure and may carry credentials (OTLP headers), so it is not in
`standard-config`. The one exception is `propagateTrace`, a per-MCP-server trust decision (section 5.1), which lives in
`standard-config/ai/mcp-servers.json` with the rest of the server policy.

| Variable | Default | Meaning |
|---|---|---|
| `TELEMETRY_ENABLED` | `false` | Master switch. Off means no SDK is created. |
| `TELEMETRY_CAPTURE_CONTENT` | `false` | Prompts, completions, tool args/results in spans. Logs a loud startup warning when `true`; for local debugging with synthetic data only. |
| `TELEMETRY_BACKEND` | `otlp` | `otlp` \| `console` (tests/debug). `openlit-sdk` adapter stays optional and unbuilt. |
| `TELEMETRY_EXPORT_TIMEOUT_SECONDS` | `2` | Per OTLP export request. Bounds shutdown to seconds (Phase 0: 13 to 19 s at the SDK default). |
| `TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS` | `5` | Hard cap on flush plus shutdown in the lifespan. |
| `TELEMETRY_DISCOVERY_SPANS` | `errors-only` | `errors-only` \| `all`: successful, fast MCP discovery spans (`server/discover`, `tools/list`, `initialize`) are dropped at export; errors and slow ones are kept. |
| `TELEMETRY_DISCOVERY_SLOW_MS` | `1000` | Discovery spans at least this long are kept. |
| `OTEL_PROPAGATORS` | unset | Leave unset: the gate in 5.1 controls injection per server. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Standard OTel var, read by the SDK. |
| `OTEL_EXPORTER_OTLP_HEADERS` | unset | Standard; **secret**, goes in the untracked env file (`BAHMNI_AI_MCP_ENV_FILE` pattern), never `.env`. |
| `OTEL_SERVICE_NAME` | `bahmni-ai` | Standard. |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | `parentbased_always_on` | Standard sampling. |

Compose: add the variables above to the `bahmni-ai` service `environment:` (`${TELEMETRY_ENABLED:-false}` etc.).
The OTel SDK reads `OTEL_*` from the process environment, not from `.env`, so compose must pass them.
The OTLP endpoint stays configurable and may point anywhere (OpenLIT, a platform collector, Jaeger).
The OpenLIT backend is a **separate, self-hosted deployment** (its own compose project or Helm release, not part of
the `bahmni-ai` service), with an image/chart version **pinned** (no `latest`). `bahmni-ai` only needs its OTLP URL.
Phase 0 runs it that way. Whether to ship an opt-in `bahmni-ai-observability` profile in bahmni-docker is decided after Phase 0.

Packaging: `pip install bahmni-ai[telemetry]` (`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`,
`opentelemetry-instrumentation-fastapi`). The Dockerfile installs the extra; the facade imports lazily and
degrades to no-op if missing.

## 5. Security and privacy

1. **Content off by default.** `include_content=False` removes prompts, answers and tool arguments/results from Pydantic AI spans.
   Tool results and chat text are where patient data lives. Verify in a test, do not trust the flag alone (section 7).
2. **Scrubbing exporter wrapper (defence in depth).** A `SpanExporter` wrapper that, before export, keeps only
   allowlisted attribute keys (`gen_ai.*` minus content keys, `bahmni.*`, `http.*` method/route/status, `rpc.*`,
   `error.type`), drops span events carrying `exception.message`/stack text, and runs `redact_value` over remaining strings.
   Needed because `record_exception` stores `str(exc)`, which can contain URLs or tokens. Mutating `ReadableSpan` in
   `on_end` is unreliable; wrapping the exporter is the supported seam. Stage 0 verifies it on the pinned versions.
3. **No session or credential leakage.** `X-OpenMRS-Session-Id` and `Cookie` are never recorded: the FastAPI
   instrumentor captures no headers unless configured, and we never configure `OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_*`.
   MCP auth headers are not span attributes. A canary test enforces it.
4. **Fully anonymous traces.** No user id, username, patient id or free text in any span, metric or log-to-trace field.
   Correlation with a user goes through the audit log (`request_id`, and `trace_id` added there), never the other way.
   A keyed-HMAC pseudonym (`bahmni.user`) is **not built**; it is added only if user-level correlation becomes necessary and
   is explicitly approved (key in a secret, rotated, never in code or `.env`).
5. **Trace-context propagation to MCP servers: off by default, per-server opt-in.** See section 5.1.
6. **The backend is now a data store.** Even without content, traces show who used which tool and when. Put OpenLIT
   or any collector on the internal network, behind authentication, with retention set (days). OpenLIT's own UI login
   and ClickHouse credentials are separate secrets, not shared with Bahmni.
7. **Authorization and audit stay independent.** Authorization never reads telemetry. `AuditSink.record` is unchanged,
   runs before any telemetry call, and works with telemetry disabled, broken or slow. Telemetry exceptions are swallowed
   by the facade. Audit records may *gain* `trace_id` for correlation, never the reverse.
8. **Fail safe:** invalid telemetry config (for example `TELEMETRY_BACKEND=otlp` with no endpoint) logs a warning and
   disables telemetry; it must not stop the chat service. (Opposite of MCP config, where failing closed is right.)

### 5.1 Trace-context propagation to MCP servers (verified in the installed code; **built in Phase 1**)

What the code does today (`mcp 2.3.0`, `fastmcp-slim 4.1.0`, `pydantic-ai-slim 2.55.0`; `MCPToolset` is built on the FastMCP client):

| Layer | Behaviour | Switch |
|---|---|---|
| MCP SDK `JSONRPCDispatcher.send_request` (`mcp/shared/jsonrpc_dispatcher.py:387-395`) | Creates a CLIENT span and calls `propagate.inject()` into `_meta` for **every** request | **None.** No setting, no per-session or per-server option. |
| FastMCP client (`fastmcp/client/mixins/tools.py:188`, `fastmcp/telemetry.py:152`) | Also injects `traceparent`/`tracestate` into `_meta` on `call_tool` | Global only: `fastmcp.settings.telemetry_mode = "off"` stops *its* layer, not the SDK's. |
| Both | Use the process-wide OTel propagator (`OTEL_PROPAGATORS`) | `OTEL_PROPAGATORS=none` stops both, but globally, so no per-server opt-in and inbound extraction is lost too. |

Conclusion: **the libraries offer no per-server control**, so the control is ours. Design:

1. Register a small global propagator (`observability/propagation.py`) that wraps the default W3C propagator. Its
   `inject()` does nothing unless a `ContextVar[bool]` named `propagate_allowed` is true; `extract()` is unchanged.
   It is installed with `propagate.set_global_textmap(...)` in `observability.setup`, so it covers both injection layers.
2. `mcp-servers.json` gains an optional per-server field `"propagateTrace": false` (default `false`; Phase 1). Startup logs which servers have it on. It is access
   policy for a destination, so it belongs with the other per-server policy in **standard-config**, validated by the
   existing strict config model (unknown fields still rejected).
3. `ServerToolset.call_tool` and `McpRegistry.open` set `propagate_allowed` to the server's flag for the duration of
   the call (the same wrapper that already knows which server is being called). The default for every server, and for
   anything else running in the process (model provider HTTP calls are not instrumented and never carry it), is off.
4. Our own client spans still exist for all servers (we keep local visibility); only the outbound header content changes.
5. Only trusted destinations get `propagateTrace: true`: first-party servers on the compose network. Document that
   third-party/SaaS MCP servers must stay `false`.
6. Also set `OTEL_INSTRUMENTATION_HTTP_*` nothing and do **not** enable the httpx instrumentor, which would inject a
   `traceparent` HTTP header on MCP and OpenMRS calls outside this gate.

Phase 0 must prove, with a stub MCP server that records the `_meta` it receives: (a) default config sends no
`traceparent`; (b) `propagateTrace: true` sends one; (c) the `ContextVar` value is visible where the SDK injects
(the dispatcher runs some work in separate tasks; the injection at line 393 should run in the caller's context, but this is
unproven); (d) two concurrent runs against a trusted and an untrusted server never leak the flag.
If (c) fails, the fallback is a per-server `httpx` event hook plus `telemetry_mode="off"` and our own `_meta` injection through `MCPToolset`'s call path.

### 5.2 Phase 0 findings that changed the design

- **`include_content=False` is not enough.** MCP server error text is copied into span status (and exception events). The scrubbing
  exporter is mandatory and ships in Phase 1: allowlisted attributes only, no status description, no exception message or stacktrace,
  `redact_value` over remaining strings.
- **Span processors are not exception-safe.** `on_start`/`on_end` exceptions reach the request path. We add no custom processor logic and
  wrap every processor in `SafeSpanProcessor` (swallows and counts errors).
- **Bounded export and shutdown.** Exporter timeout 2 s; `shutdown()` runs in a daemon thread joined with a timeout, so a dead
  backend cannot stall container stop (Compose grace is 10 s).
- **Discovery noise.** About 20 discovery spans per run. The exporter drops OK, fast discovery spans and keeps errors and slow ones, so
  failures stay visible and the data volume falls.
- **No model-duration metric** from Pydantic AI: use span durations. Cost is a backend concern.

## 6. Trade-offs

- Using native OTel means the exact span names/attributes follow Pydantic AI's `version` setting and the GenAI
  conventions, which are still evolving. Pin `pydantic-ai-slim` and set `InstrumentationSettings(version=...)` explicitly.
- No in-app OpenLIT features (guardrails, evals, cost in-process). Backend-side cost tables cover cost; the rest is out of scope.
- Content capture off means debugging bad answers needs the audit log plus reproducing with synthetic data. That is the point.
- Per-run MCP connections create many short MCP spans; sampling handles volume.
- Allowlist scrubbing may hide a useful new attribute until added: the safe direction to fail.

## 7. Testing strategy

Use `InMemorySpanExporter` / `InMemoryMetricReader` with the existing stub MCP servers and `AI_MODEL=test`.

| Test | Asserts |
|---|---|
| Disabled | No SDK objects created; chat responses identical; no `opentelemetry.sdk` import needed. |
| Spans emitted | One chat turn → agent, model, tool, MCP spans in one trace; our `bahmni.*` attributes present. |
| No content | With default settings, canary strings in the user message, tool arguments and tool results appear in **no** span attribute, event or metric label. Repeat with the scrubbing exporter against a deliberately leaky span. |
| No secrets | Canary `JSESSIONID`, bearer token, `MCP_*_TOKEN` value and exception text containing them never exported. |
| Policy denial | Denied and invalid-argument calls produce `bahmni.decision` accordingly, with identical audit records. |
| Independence | Audit record is emitted with telemetry off, with a facade that raises, and with an exporter that hangs. Authorization result unchanged in all three. |
| Fault injection | Collector unreachable: request latency within a small bound, SSE completes, process memory bounded (queue drops). |
| Concurrency | Alice and Bob in parallel: spans never cross traces; confirm turn links to the original. |
| Layering | A test greps `src/bahmni_ai/{agent,mcp,api,auth}` for `import opentelemetry` / `openlit` and fails. |
| Provider switch | `AI_MODEL=test` and a second provider stub both produce token-usage metrics. |
| Smoke (manual) | Compose profile with OpenLIT or Jaeger: trace visible, tokens and latency charts populated, swap to the other backend by endpoint only. |

## 8. Phased plan

Superseded by section 11 (revised after Phase 0). Original phase list removed to avoid two sources of truth.

## 9. Remaining open questions

None open on versions. Remaining: confirm Phase 0 shows the pinned platform ingests our GenAI spans and metrics.

## 10. Decisions (from review)

1. **Anonymity:** traces are fully anonymous by default; HMAC pseudonyms only on explicit approval later (5, item 4).
2. **Backend governance, to be completed before rollout.** The OTLP endpoint is configurable; the following must be named and agreed:

   | Item | Decision needed | Owner |
   |---|---|---|
   | Who runs the backend (OpenLIT, ClickHouse, collector), patches and upgrades it | | TBD |
   | Who may view traces and metrics (roles, SSO or local accounts, no shared admin login) | | TBD |
   | Network exposure: internal only, authenticated, TLS to the OTLP endpoint | | TBD |
   | Retention (default proposal: 7-14 days for traces, 30-90 days for aggregated metrics) | | TBD |
   | Deletion: how a record is purged on request or incident, and who executes it | | TBD |
   | Backups: excluded, or same retention as the data | | TBD |
   | Incident handling if content capture was ever enabled by mistake (purge procedure, who decides) | | TBD |

3. **Deployment form:** Docker Compose for the separate OpenLIT deployment.
4. **Propagation:** off by default for every MCP server; per-server opt-in `propagateTrace` (5.1) is **built in Phase 1** (the libraries have no such switch, so we provide it). Enable only for trusted servers.
5. **OpenLIT:** pinned version, separate self-hosted deployment for Phase 0; application stays OTel-first and does not call `openlit.init()` unless testing proves it necessary.
6. **Platform pin:** `ghcr.io/openlit/openlit:2.1.0` (tag exists in the registry; GitHub release `openlit-2.1.0`, published 2026-09-10). Pin the ClickHouse image to the tag its compose file uses (`clickhouse/clickhouse-server:24.4.1` on `main` at the time of writing) and re-check both at upgrade. Never `latest`. The Python SDK `1.45.0` is not used by the app.

### 10.1 Hardening the upstream OpenLIT compose file (read from `openlit/openlit` `docker-compose.yml` on `main`)

Do not run it as shipped. Our separate Compose deployment must change:

| Upstream default | Our deployment |
|---|---|
| `image: ghcr.io/openlit/openlit:latest` | Pinned `2.1.0`. |
| `TELEMETRY_ENABLED: "true"` (OpenLIT's own product telemetry, name suggests it reports usage to the vendor; unverified) | Set to `"false"` unless its behaviour is checked and approved. |
| ClickHouse user `default`, password `OPENLIT`; DB ports `8123` and `9000` published | Strong password from an untracked env file; **do not publish** ClickHouse ports. |
| UI `3000`, OTLP `4317`/`4318` published on all interfaces | Bind to the internal network only; put the UI behind TLS and authentication; OTLP reachable only from `bahmni-ai` (and a collector if used). |
| Optional Google/GitHub OAuth variables | Use named accounts (no shared admin), per 10.2. |
| Named volumes, no retention setting | Set ClickHouse TTL per the retention decision in 10.2. |
7. **Phase 1 priorities (after Phase 0 review):** (1) exporter scrubbing, (2) span-processor safety, (3) bounded export and shutdown,
   (4) OpenLIT default-credential and open-registration hardening. All six Phase 0 changes are in Phase 1 (section 11).
8. **Deferred to follow-up validation:** HTTP/SSE path, real providers, OpenLIT UI and outbound-telemetry checks (11.3).

## 11. Phase 1 plan and tasks (revised after Phase 0)

### 11.1 Scope
Foundation plus the six Phase 0 changes. Domain signals (chat-turn span, tool-call metrics, error counters) are Phase 2.

### 11.2 Tasks (status: `[x]` done, `[ ]` open). Phase 1 implemented; evidence below

Priority 1 to 4 first, in this order.

1. **Exporter scrubbing** (`observability/scrub.py`)
   - [x] Allowlist exporter wrapper (attributes, events, status description, exception text), `redact_value` on remaining strings.
   - [x] `TELEMETRY_CAPTURE_CONTENT=true` keeps content attributes only; still drops exception text and status description; startup warning.
   - [x] Tests: canary in message, tool args and results, tool error text, bearer, `JSESSIONID`; metrics labels; content on and off.
2. **Span-processor safety** (`observability/_otel.py`)
   - [x] `SafeSpanProcessor` wrapper for every processor added; no custom processor logic.
   - [x] Tests: raising processor (start, end, both) leaves chat and audit intact; raising exporter likewise; test that fails if an unwrapped processor is registered.
3. **Bounded export and shutdown**
   - [x] Exporter timeout setting; daemon-thread bounded flush and shutdown in the lifespan.
   - [x] Tests: refused and hanging backend, shutdown within the cap, requests unaffected.
4. **OpenLIT hardening** (`deploy/openlit/`)
   - [x] Pinned compose (OpenLIT 2.1.0, ClickHouse 24.4.1), no published ClickHouse, loopback ports, `TELEMETRY_ENABLED=false`.
   - [x] `harden.py`: replace the seeded `user@openlit.io` account (email and bcrypt password) and verify the default login fails.
   - [x] Registration blocked: OpenLIT has open self-registration (Phase 0b finding); front the UI with a pinned nginx that denies the register endpoints.
   - [x] Retention TTL parameter (default 168 h traces) and a documented deletion runbook including `otel_traces_trace_id_ts` and metric tables.
5. **`propagateTrace` per MCP server** (default `false`)
   - [x] `ServerConfig.propagateTrace`, gated propagator, `propagation_scope` in `ServerToolset.get_tools/call_tool`.
   - [x] Tests: default sends no `traceparent`; trusted server only on `tools/call` and `tools/list` via `get_tools`; 20 concurrent mixed runs; startup log of enabled servers.
6. **Discovery-span reduction without losing errors**
   - [x] Exporter filter by `mcp.method.name` (name fallback): drop OK spans under `TELEMETRY_DISCOVERY_SLOW_MS`, keep ERROR and slow.
   - [x] Tests: failing server and slow discovery still exported; volume drop measured.
7. **Foundation**
   - [x] `observability/` package and no-op default; settings; `[telemetry]` extra (SDK, OTLP HTTP exporter, FastAPI instrumentation); Dockerfile installs it.
   - [x] Agent instrumentation (content off, explicit `version`); FastAPI instrumentation (excluding health); log correlation (`trace_id`).
   - [x] Fail-safe config: enabled but misconfigured (no SDK, no endpoint) logs a warning and disables telemetry.
   - [x] Layering test: no `opentelemetry`/`openlit` import in `agent/`, `mcp/`, `api/`, `auth/`.
   - [x] Compose pass-through in bahmni-docker (`TELEMETRY_*`, `OTEL_*`), `.env` defaults, operator docs.

Evidence and deviations:
- Tests: `tests/test_observability.py` (31 tests; 96 in the suite), ruff and mypy clean.
- End-to-end through the real app against the pinned, hardened OpenLIT: 8 spans per chat turn (18 before the filters),
  no canaries in ClickHouse, HTTP span carries only method, route and status, `trace_id` appears on log and audit lines.
- Phase 0 left a gap that Phase 1 closed: a failed MCP connection produces a library discovery span with status UNSET, which the
  discovery filter would hide. Registry failures now emit their own `bahmni_ai.mcp.unavailable` ERROR span and counter
  (`record_mcp_unavailable`, pulled forward from Phase 2).
- Extra finding: FastAPI per-message `receive`/`send` spans are noise; excluded.
- OpenLIT has open self-registration; blocked by an nginx front (`deploy/openlit/assets/nginx.conf`); `harden.py` also reports and can remove accounts that registered earlier.
- ClickHouse TTLs are now parameterised and re-applied at every start (`TRACE_RETENTION_HOURS` 168, `METRIC_RETENTION_HOURS` 720).
- `purge.py` clears all `otel_*` tables including the derived trace index.
- Processor safety is enforced by construction (only `SafeSpanProcessor` registered, tested) and by tests documenting that an unwrapped processor breaks requests.
- Not done in Phase 1 (Phase 2): chat-turn span, tool-call counters and histograms, error counters, confirmation counters.

### 11.3 Follow-up validation (tracked, not blocking)
- [ ] HTTP/SSE path through bahmni-core and the proxy. In-process evidence: with `TestClient`, one trace `POST /bahmni-ai/chat` to `invoke_agent` to `chat` to `execute_tool` to `tools/call`, correct parenting across the SSE generator. Still to check: real SSE streaming, client disconnect mid-stream, and `X-Request-Id` handling end to end.
- [ ] Real providers (Anthropic, Ollama): token metrics, provider-error spans and their exception text through the scrubber.
- [ ] OpenLIT UI: dashboards show our spans (storage and attribute names verified; screens not); outbound traffic of the OpenLIT container with `TELEMETRY_ENABLED=false`.
- [x] TTL survives a ClickHouse restart (verified: 168 h and 720 h after `docker restart`).
- [x] Default-login flow exercised in Phase 0b: `user@openlit.io` / `openlituser` logged in before hardening; rejected after.
- [ ] Governance table in 10.2 filled in with named owners before any rollout.
- [ ] Exercise `propagateTrace: true` with a real first-party MCP server that exports traces (joined trace, and what that server exports on errors).
