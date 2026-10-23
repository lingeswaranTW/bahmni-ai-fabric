# Bahmni AI Chatbot Backend: Staged Plan

Target location once approved: `/Users/lingeswaransubramaniyam/Data/AIFabric/docs/PLAN.md`. The folder is currently empty, so this is greenfield.

## Goal
A reusable AI chatbot backend, running as one new service in the Bahmni compose file behind `bahmni-proxy`. It connects to any MCP server (Documents, OpenMRS, Slack, Jira, and others), works with multiple LLM providers, and runs tools only when the signed-in OpenMRS user holds the required privilege.

## Bahmni conventions found in the repos (this section overrides the earlier "same origin via proxy" idea)
- **Gateway pattern (BAH-4608):** `bahmni-core` hosts a thin pass-through controller. The browser calls `/openmrs/ws/rest/v1/bahmnicore/template/api/...`, which forwards to the service at `template.service.url` (`bahmnicore.properties`, with connect and read timeouts, set from the compose env var `TEMPLATE_SERVICE_URL`). It sends the caller's `JSESSIONID` as header **`X-OpenMRS-Session-Id`** and `Authorization` as `X-OpenMRS-Authorization`, and nothing else. The service never sees the browser cookie. Upstream 4xx/5xx and content type are passed through.
- **Our design therefore follows it:** add an `ai` proxy in bahmni-core (`/openmrs/ws/rest/v1/bahmnicore/ai/...` → `ai.service.url`), so OpenMRS authenticates the caller before the AI server sees anything. The AI server still validates the forwarded session by calling OpenMRS `/session`. The compose network is not a trust boundary.
- **Streaming gap:** the template proxy buffers responses. Chat needs SSE, so the core controller must stream (for example Spring `StreamingResponseBody` or `SseEmitter`) and disable buffering. This is new work in bahmni-core and is the main risk to this route.
- **Compose pattern (`bahmni-docker/bahmni-standard/docker-compose.yml`):** service `image: bahmni/<name>:${<NAME>_IMAGE_TAG:?}`, `profiles: ["emr","bahmni-standard"]`, env `TZ`, `PORT`, `OPENMRS_URL: http://${OPENMRS_HOST}:${OPENMRS_PORT}`, `depends_on: openmrs`, config volume `${CONFIG_VOLUME}:/etc/bahmni_config/:ro`, `logging: *log-config`, `restart: ${RESTART_POLICY}`, a `curl` healthcheck at `/<service>/health`, no published ports. Add `AI_SERVICE_URL` to the openmrs service environment, and `AI_SERVICE_IMAGE_TAG` and `AI_SERVICE_PORT` to `.env`.
- **Standard-config:** the template service reads `/etc/bahmni_config/print-templates` from the standard-config volume. Ours reads `/etc/bahmni_config/ai/mcp-servers.json` (a new `ai/` folder in `standard-config`) and takes secrets only from its own environment variables.
- **Code standard:** an existing FastAPI service, `bahmni-ai` (clinical insights), already sets the house style: Python 3.11+, `uv`, `pydantic-settings`, `structlog`, `httpx`, `ruff`, `mypy`, `pytest` with `respx`, MPL-2.0 headers, GitHub workflows `validate_pr` and `build_publish`, SonarCloud, `pre-commit`, and a Dockerfile. Our service is a **separate service** from it and copies those tools and layout. It must not extend it, because that service does a different job. The service name is still to decide (for example `bahmni-ai-chat`).
- **No `llmgate` exists** in these repos. `bahmni-ai` calls Anthropic and OpenAI SDKs directly, so Pydantic AI replaces that pattern for us.
- **Existing `bahmni-mcp-server` (not in scope, noted):** it wraps OpenMRS with a single shared Basic-auth account and `verify=False`, so it does not meet the per-user requirement. Do not use it as the OpenMRS MCP without changes.

## Decisions already made
| Topic | Decision |
|---|---|
| Deployment | New compose service following the `template-service` pattern. Browser reaches it through a new **bahmni-core proxy endpoint**, not a direct proxy route. |
| Login | `JSESSIONID` only. bahmni-core forwards it as `X-OpenMRS-Session-Id`. No Keycloak work. |
| User identity | Always an OpenMRS user, validated with `GET /openmrs/ws/rest/v1/session` |
| Non-OpenMRS MCPs (Jira, Slack, Documents) | One **service credential** per server. Users have no accounts there, so there is no per-user OAuth. |
| Access control | OpenMRS privileges gate each tool. A new shared AI privilege set is created in OpenMRS. |
| OpenMRS MCP | Optional. When one exists it must forward the user's session per request. |
| History | Postgres, with no credentials stored and sensitive results redacted or referenced |
| Clinical data to cloud LLMs | The implementer's configuration decides. The block provides redaction hooks. |
| Tenancy | Single tenant |
| Stack | FastAPI, Pydantic, Pydantic AI (after a spike), official MCP SDK through Pydantic AI. No llmgate or LiteLLM. |

## Architecture
```
Browser --JSESSIONID--> bahmni-proxy --> openmrs (bahmni-core ai proxy, streams SSE)
   --X-OpenMRS-Session-Id--> ai-server (FastAPI)
  auth/    validate session with OpenMRS -> UserContext
  agent/   Pydantic AI agent, model chosen by config
  mcp/     config · credentials · auth strategies · registry · policy wrapper
  audit/   structured JSON log
  api/     POST /chat (SSE), POST /chat/confirm, GET /health
           --> MCP servers (streamable-http) --> Documents / OpenMRS / Slack / Jira / ...
```
Per tool call the policy wrapper does, in order: allowlist → argument validation → privilege check → confirmation (writes) → execute with timeout (no write retry) → audit.

## Configuration contract (secrets by reference only)
```json
{
  "mcpServers": {
    "documents": {
      "url": "${DOCUMENTS_MCP_URL}", "transport": "streamable-http",
      "auth": {"type": "bearer", "credentialRef": "DOCUMENTS_MCP_TOKEN"},
      "tools": {"allow": ["search_*", "get_*"], "default": "deny"}
    },
    "jira": {
      "url": "${JIRA_MCP_URL}", "transport": "streamable-http",
      "auth": {"type": "bearer", "credentialRef": "JIRA_MCP_TOKEN"},
      "tools": {
        "overrides": {"create_issue": {"access": "write", "confirm": true, "requiredPrivilege": "AI: Create Jira Issue"}},
        "default": "deny"
      }
    },
    "openmrs": {
      "url": "${OPENMRS_MCP_URL}", "transport": "streamable-http",
      "auth": {"type": "openmrs-session"},
      "tools": {"overrides": {"search_patients": {"access": "read", "requiredPrivilege": "Get Patients"}}, "default": "deny"}
    }
  }
}
```
- Strict Pydantic validation, validated at startup, and the server refuses to start on invalid config.
- Only `streamable-http` by default. Stdio only behind an explicit `allowStdio` setting, with credentials injected from the resolver.
- `${VAR}` expansion only for allowlisted variables.
- Discovery never grants access. Tools not matched by `allow` or `overrides` are hidden from the model and refused at call time.
- MCP annotations such as `readOnlyHint` are untrusted. A tool not clearly read-only is treated as a write that needs confirmation.
- Tool names are namespaced `server__tool`, and server names cannot contain `__`.
- The credential resolver is an interface with an environment-variable implementation that fails closed and never logs values. A secrets-manager implementation can replace it later.

## Stages

### Stage 0: Spike (go or no-go on Pydantic AI)
- Build a throwaway app with Pydantic AI, two providers (one cloud, one Ollama), and two stub MCP servers.
- Prove: per-run `MCPToolset` from user context, two concurrent users with no credential mixing, a tool-call interceptor, an approval or pause mechanism, and cancellation behavior.
- **Done when:** a written result confirms or rejects Pydantic AI. Fallback is the raw MCP SDK with a small own tool loop and one provider interface.

### Stage 1: Project skeleton and compose wiring
- Files: `pyproject.toml` (uv, ruff, mypy, pytest as in `bahmni-ai`), `app/main.py` with lifespan, `Dockerfile`, `.github/workflows` (validate_pr, build_publish), MPL headers, `/<service>/health`, structlog with request IDs, `.env.example`, `.gitignore` for secrets.
- Compose entry in `bahmni-docker` following the `template-service` block, `.env` variables, `AI_SERVICE_URL` for openmrs, and the `ai/` config folder in `standard-config`.
- **Done when:** the container starts under compose and its health check passes.

### Stage 1b: bahmni-core AI proxy (separate repo and PR)
- New controller and client modeled on `TemplateRenderController` and `TemplateServiceClient`, with `ai.service.url` and timeouts in `bahmnicore.properties`, forwarding `X-OpenMRS-Session-Id` and `X-OpenMRS-Authorization`.
- Streaming SSE pass-through for `/chat`, plus normal pass-through for the other endpoints. Add a privilege check (the gate privilege) in the controller as the first layer.
- Tests: unit tests as in BAH-4608, plus a streaming test and a client-disconnect test.
- **Done when:** the browser can reach the AI service's health and a streamed chat through `/openmrs/ws/rest/v1/bahmnicore/ai/...`.

### Stage 2: Configuration and credentials
- Files: `mcp/config.py`, `mcp/credentials.py`, `mcp/auth.py` (`AuthStrategy` with `none`, `bearer`, `api-key-header`, `openmrs-session`).
- Tests: unknown auth type, missing credential fails closed, stdio rejected by default, disallowed env var, invalid schema, secrets never appear in logs or errors.
- **Done when:** bad config stops startup with a clear, secret-free error.

### Stage 3: Authentication
- Files: `auth/session.py`, `api/deps.py`. Read `X-OpenMRS-Session-Id`, validate it against OpenMRS `/session`, build `UserContext`, cache briefly keyed by a hash, and return `session_expired` on 401 (the template service maps 401 the same way).
- The service accepts requests only with that header. Browser cookies are never read, so CSRF on the service is not a concern. CSRF on the core endpoint follows the existing OpenMRS behavior (still to verify for POSTs).
- Tests: no cookie, invalid cookie, expired session, spoofed identity headers ignored, cookie never logged.
- **Done when:** identity comes only from a validated OpenMRS session.

### Stage 4: Agent and provider abstraction
- Files: `agent/factory.py`, `agent/events.py`, `api/chat.py` (SSE).
- Model chosen by config (`provider:model`). Event types: `text`, `tool_started`, `tool_finished`, `confirmation_required`, `error`, `done`.
- Tests: switch provider by configuration only, with no code change. Streaming and timeout behavior per provider. Provider capability differences documented.
- **Done when:** the same chat request works against two providers.

### Stage 5: MCP registry and discovery
- Files: `mcp/registry.py`. Shared connections for service credentials, per-run connections for per-user auth. Discovery on connect, namespacing, allow and deny matching, timeouts, failure isolation (one server down does not break the others).
- Tests: two servers with the same tool name, unlisted tool hidden and refused, server unavailable, clean shutdown.
- **Done when:** tools from at least two servers are usable without server-specific code.

### Stage 6: Authorization and policy wrapper
- Files: `mcp/policy.py`, `auth/privileges.py`. Check `requiredPrivilege` against OpenMRS for every call, fail closed, and validate arguments against the tool's JSON schema. Optional per-tool argument constraints, such as an allowed Jira project list.
- Create the AI privileges in OpenMRS (names to be chosen).
- Tests: user without the privilege is denied, model-supplied identity or privilege ignored, a later step in a multi-step run is authorized independently, OpenMRS denial surfaced cleanly.
- **Done when:** protected tools cannot run without the privilege, and OpenMRS enforcement is still the backstop for OpenMRS operations.

### Stage 7: Writes, confirmation, and audit
- Files: `api/confirm.py`, `audit/log.py`. Single-use confirmation token bound to user, conversation, and argument hash. No automatic retry of writes. Audit record per call: user, server, tool, decision, resource ID, outcome, latency, request ID. Redact secrets and clinical content.
- Tests: confirmation required, token replay rejected, argument tampering rejected, write not retried after a connection failure, audit contains no secrets.
- **Done when:** consequential writes require confirmation and leave an audit trail.

### Stage 8: History persistence
- Files: `db/` with Postgres models and migrations. Conversation IDs, redacted tool results, no credentials or session cookies. History is never used as an authorization source.
- **Done when:** a conversation can be resumed and the stored data passes a secrets and PHI review checklist.

### Stage 9: Hardening and acceptance
- Concurrency tests (Alice and Bob in parallel, streaming and cancellation), session expiry mid-run, load and timeout tests, redaction hooks documented, local and cloud config check, operator docs (config reference, privilege setup, and the warning that clinical data reaches whichever provider is configured).
- **Done when:** all 13 acceptance criteria in the original brief are covered by automated tests or documented manual checks.

## Known trade-offs and risks
- For non-OpenMRS servers the backend is the **only** enforcer. Jira and Slack see only the service account, so the privilege check must always run and fail closed.
- Privileges gate tools, not rows. Scope each service credential to least privilege and use argument constraints where needed.
- Actions in Jira and Slack appear as the service account. Audit the OpenMRS user, and optionally add attribution text to the action.
- An OpenMRS MCP that uses a shared service account would bypass per-user enforcement. Only a session-forwarding one is supported for user-scoped actions.
- Pydantic AI MCP and approval APIs have changed names recently. Pin a version, and let Stage 0 decide.
- Still to verify on your OpenMRS version: whether `/session` returns the complete effective privilege list (otherwise use another trusted OpenMRS call).
- Open items: exact privilege names, the service name, and whether bahmni-core can stream through its current REST stack.

## Verification
`pytest` for unit, policy, and concurrency tests. Then run the compose stack with a fake OpenMRS and two stub MCP servers, switch the provider with an environment variable only, and run a confirmed write end to end.


---

## Status (updated after implementation)
| Stage | State | Evidence |
|---|---|---|
| 0 Spike | Done: GO | `docs/STAGE0_SPIKE_RESULT.md` |
| 1 Skeleton, Docker, compose | Done | image builds; container ran against real Postgres (health + SSE chat) |
| 1b bahmni-core proxy | Done (uncommitted) | `docs/REPOS.md`, 9 Java tests pass |
| 2 Config + credentials | Done | `tests/test_config_credentials.py` |
| 3 Authentication | Done | `tests/test_auth.py` |
| 4 Agent + providers | Done | `tests/test_provider_switch.py`, `tests/test_chat_api.py` |
| 5 Registry + discovery | Done | `tests/test_registry_policy.py`, `tests/test_startup.py` (stdio) |
| 6 Authorization | Done | `tests/test_registry_policy.py` |
| 7 Writes, confirmation, audit | Done | `tests/test_chat_api.py`, audit assertions |
| 8 History | Done | `tests/test_db_store.py` (SQLite and real Postgres) |
| 9 Hardening | Done for what can run here | `tests/test_hardening.py`; see "Not verified" below |

Not verified (needs a real Bahmni): behaviour against a live OpenMRS `/session` response shape (including effective
privileges and the `System Developer` role), SSE through `bahmni-proxy` (Apache), a real LLM provider call, a real
Jira/Slack MCP server, the bahmni-docker, distro and Helm edits (listed in `docs/REPOS.md`; compose syntax validated with `docker compose config` only).
