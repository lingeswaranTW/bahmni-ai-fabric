# bahmni-ai: operator guide

## Request path and trust
Browser → `POST /openmrs/ws/rest/v1/bahmnicore/ai/chat` (bahmni-core, SSE) → `bahmni-ai` (`/bahmni-ai/chat`).
bahmni-core has already authenticated the caller. It forwards only `JSESSIONID` as `X-OpenMRS-Session-Id`
(plus `X-Request-Id`). bahmni-ai **does not trust that header**: it calls OpenMRS `GET /ws/rest/v1/session`
and takes identity and privileges from the answer. Other identity headers (`X-User-Id`, ...) are ignored.
The service has no CORS and reads no browser cookies. Do not publish its port; keep it on the compose network.

## Configuration
| Variable | Meaning |
|---|---|
| `AI_MODEL` | `provider:model`, for example `anthropic:claude-sonnet-5-5`, `openai:gpt-4o`, `ollama:llama3.2`. Provider keys use the standard variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OLLAMA_BASE_URL`). |
| `OPENMRS_URL` | Base URL of OpenMRS for session validation. |
| `MCP_CONFIG_PATH` | Default `/etc/bahmni_config/ai/mcp-servers.json` (from standard-config). Missing/invalid file stops startup. |
| `MCP_*` | The only variables the configuration may reference: `${MCP_X_URL}` in `url`, and `credentialRef` names. Add others via `CREDENTIAL_ALLOWLIST=NAME1,NAME2`. |
| `MCP_ALLOW_STDIO` | Default `false`. When `true`, stdio servers may be configured; they run as shared local processes and receive only variables listed in `envRefs`. |
| `GATE_PRIVILEGE` | OpenMRS privilege every user needs (default `Use AI Chatbot`). |
| `DATABASE_URL` | `postgresql+asyncpg://...`. If unset, history is in memory and lost on restart. |
| `HISTORY_TOOL_RESULTS` | `none` (default), `truncated`, `full`: how much tool output is stored with conversation history. |
| `MCP_CALL_TIMEOUT_SECONDS`, `AI_MAX_TOOL_STEPS`, `CONFIRMATION_TTL_SECONDS` | Limits. |
| `TELEMETRY_ENABLED` | Default `false`. OpenTelemetry traces and metrics; see [Telemetry](#telemetry). |
| `TELEMETRY_CAPTURE_CONTENT` | Default `false`. Prompts, answers and tool data in spans. Synthetic data only. |
| `TELEMETRY_EXPORT_TIMEOUT_SECONDS`, `TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS` | Defaults `2` and `5`. Bound exports and container stop. |
| `TELEMETRY_DISCOVERY_SPANS`, `TELEMETRY_DISCOVERY_SLOW_MS` | `errors-only` (default) or `all`; slow threshold `1000`. |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME` | Standard OpenTelemetry variables. Headers are a secret: untracked env file only. |

## mcp-servers.json (application convention, not the generic MCP format)
See `ai/mcp-servers.example.json` in the standard-config repo (mirrored for tests in `tests/fixtures/`). Rules:
- Server names: lowercase letters, digits and `-` (tools are exposed as `server__tool`).
- `auth.type`: `none`, `bearer`, `api-key-header` (shared service credential via `credentialRef`),
  `openmrs-session` (the user's OpenMRS session is sent as `X-OpenMRS-Session-Id`; the MCP server must use it
  when calling OpenMRS, and must be first-party and on a trusted network).
- **Discovery grants nothing.** A tool is visible only if matched by `allow` or `overrides` and not by `deny`.
  A tool matched only by `allow` is treated as a read if the server marks it read-only (a hint from an untrusted
  server), otherwise as a write that needs the server's `requiredPrivilege` and confirmation, otherwise hidden.
- `write` tools must set `confirm: true` and have a `requiredPrivilege` (tool or server level).
- `propagateTrace` (default `false`): send W3C trace context (`traceparent`) to this server. Set `true` only for trusted,
  first-party servers; see [Telemetry](#telemetry).
- `argumentConstraints` pin argument values (for example a Jira project). A missing argument is refused.
- Secrets never go in this file. The file is rejected if it has unknown fields.

## Security model
Every tool call: allowlist → JSON-schema argument validation → constraints → privilege check with OpenMRS
(gate + tool privilege) → confirmation for writes → fresh privilege re-check immediately before a write →
single execution with a timeout (no automatic retry) → audit record.
- For OpenMRS operations OpenMRS enforces its own privileges again under the user's session.
- For other servers (Jira, Slack, ...) only this service enforces access: the downstream sees one service account.
  Privileges gate **tools**, not records: scope the service credential to least privilege.
  Actions appear as the service account; the audit log names the OpenMRS user.
- Confirmation: `confirmation_required` event → user approves via `POST /chat/confirm`. The pending run is stored
  server-side, bound to the user, single-use, and expires after `CONFIRMATION_TTL_SECONDS`. The client cannot alter
  the arguments.
- Tool output is untrusted data (prompt injection): it cannot widen permissions because decisions never come from
  the model. Instructions in the system prompt are a backstop, not the control.

## Data and privacy
Tool results can contain clinical data and are sent to whichever model provider you configure. Choosing a provider
for clinical data is the implementer's decision (use `ollama:` for local-only). History stores no credentials or
session ids; tool results are not stored by default. Pending confirmations briefly hold the full run state.
Logs and audit records contain argument *names* and a hash, never values; secrets are redacted by key and pattern.

## API (SSE)
`POST /bahmni-ai/chat {message, conversationId?}`, `POST /bahmni-ai/chat/confirm {confirmationId, approved?, decisions?}`.
Events: `conversation`, `text`, `servers_unavailable`, `tool_started`, `tool_finished`, `confirmation_required`,
`error` (`session_expired`, `openmrs_unavailable`, `provider_error`, `assistant_error`, `step_limit`, `timeout`,
`confirmation_expired`, `invalid_request`, `internal_error`), `done`.

## Provider differences to account for
Tool-calling support, streaming behaviour, context size and structured-output reliability vary by model. Small local
models may call tools poorly or ignore the tool-result-is-data rule: test your model with your tools before enabling
writes. Switching providers needs only `AI_MODEL` and the provider's key variable.

## Known limits
- Connections to MCP servers are opened per chat run (simple and isolates users); pooling is a future optimisation.
- The schema is created with `create_all`; there is no migration tool yet.
- Verify on your OpenMRS version that `/session` lists effective privileges (inherited roles). If not, privilege
  checks need another OpenMRS call. `System Developer` is treated as a super user.
- Verify that `bahmni-proxy` (Apache) does not buffer SSE for `/openmrs/...`.

## Telemetry
Off by default. Design, trade-offs and Phase 0 evidence: [OBSERVABILITY.md](OBSERVABILITY.md),
[OBSERVABILITY_PHASE0_RESULT.md](OBSERVABILITY_PHASE0_RESULT.md).
- Enable with `TELEMETRY_ENABLED=true` and `OTEL_EXPORTER_OTLP_ENDPOINT`. A misconfiguration (no endpoint, missing SDK) logs
  `telemetry_disabled` and the service runs without telemetry; it never stops the service.
- **What is exported:** span names, timings, status, token counts, model and tool names, HTTP route and status. **Not exported:**
  prompts, answers, tool arguments and results, user names, cookies, session ids, URLs, error text from MCP servers or providers.
  Everything passes an allowlist exporter (`observability/scrub.py`); a span that cannot be scrubbed is dropped.
- Routine, successful MCP discovery spans are dropped; failed or slow ones are kept, and an unreachable MCP server always
  produces a `bahmni_ai.mcp.unavailable` error span and a counter.
- Export and shutdown are bounded (2 s per export, 5 s shutdown), so an unreachable backend cannot stall requests or container stop.
- Trace context is not sent to MCP servers unless the server sets `propagateTrace: true`.
- The backend is a separate deployment: [`deploy/openlit/`](../deploy/openlit/README.md) (pinned OpenLIT and ClickHouse).
