# 04 - API and key flows

Derived from: `api/chat.py`, `api/conversations.py`, `api/health.py`, `api/deps.py`, `agent/service.py`, `agent/events.py`, `mcp/policy.py`, `mcp/registry.py`, `auth/session.py`, `main.py`, `providers/*`, `prompts/*`; bahmni-core `AiServiceController.java` for the front proxy.

## 1. Plain language

The browser calls `bahmni-core`; `bahmni-core` calls these endpoints. Chat answers are Server-Sent Events (a long-lived HTTP response with a stream of small named events). Conversation list/read/delete are normal JSON calls. Everything except health needs the session header.

## 2. Public surface

Prefix = `SERVICE_PATH` (`/bahmni-ai`). Router wiring is in `main.create_app`.

| Method and path | Body / params | Response | Auth |
|---|---|---|---|
| `GET /bahmni-ai/health` | none | `{"status":"UP"}` (constant) | none |
| `POST /bahmni-ai/chat` | `{message (<=8000), conversationId? (<=64, [A-Za-z0-9_-])}`; extra fields rejected | SSE stream | session + gate privilege |
| `POST /bahmni-ai/chat/confirm` | `{confirmationId (<=128), approved=true, decisions{actionId: bool}}` | SSE stream | session + gate privilege |
| `GET /bahmni-ai/conversations` | `limit` 1..100 (20), `offset` >=0 | `{conversations:[{id,title,createdAt,updatedAt}], limit, offset}` | same |
| `GET /bahmni-ai/conversations/{id}` | path id | summary + `messages:[{role,text,timestamp,tools?:[{server,tool}]}]` | same |
| `DELETE /bahmni-ai/conversations/{id}` | `?hard=false|true` | 204, or 404 `not_found` | same |

"Session" means header `X-OpenMRS-Session-Id` (constant `SESSION_HEADER` in `mcp/auth.py`). Auth failures are `HTTPException` with body `{"detail":{"code","message"}}`: `401 session_expired`, `403 forbidden`, `502 openmrs_unavailable`, `404 not_found`. Other request validation errors use FastAPI's default 422 body (not customised; **Unverified** shape because no override exists in the code).

No CORS middleware is installed (`test_no_cors_headers_and_unauthenticated_rejected`): browsers are not meant to call it.

The bahmni-core proxy (`AiServiceController`, mapping `/rest/v1/bahmnicore/ai`) exposes `GET /health`, `POST /chat`, `POST /chat/confirm`, `GET /conversations`, `GET /conversations/{id}`, `DELETE /conversations/{id}`. It checks `Context.isAuthenticated() && Context.hasPrivilege("Use AI Chatbot")` first (403 otherwise), forwards only the `JSESSIONID` value (as `X-OpenMRS-Session-Id`) and never the caller's `Authorization` or other cookies. (Header and privilege behaviour read from the Java source; streaming behaviour of `AiServiceClient` is **Unverified**.)

### SSE events (`agent/events.py`)

| Event | Payload | When |
|---|---|---|
| `conversation` | `{conversationId}` | first event of every turn |
| `servers_unavailable` | `{servers[]}` | some MCP servers could not be reached this turn |
| `activity` | `{id, kind: model|thinking|tool|retry, state: started|finished, server?, tool?, attempt?}` | temporary progress lines (`UI_ACTIVITY_EVENTS`); never content |
| `text` | `{delta}` | streamed answer text |
| `tool_started` / `tool_finished` | `{server, tool}` / `+ ok` | a tool call begins/ends; `ok=false` if the result is `{"error": ...}` |
| `confirmation_required` | `{confirmationId, expiresInSeconds, actions[{id,server,tool,arguments}]}` | a write needs approval; no tool has run |
| `error` | `{code, message}` | see below |
| `done` | `{}` | always last (also follows `confirmation_required`; not after `error`) |

Error codes emitted by `ChatService`: `invalid_request`, `confirmation_expired`, `session_expired`, `openmrs_unavailable`, `step_limit`, `rate_limited`, `provider_error`, `assistant_error`, `timeout`, `internal_error`. Messages are fixed strings; provider/tool error text is never echoed to the client.

Limits: `AI_MAX_TOOL_STEPS` (10) maps to `UsageLimits.request_limit`; `AI_MAX_TOTAL_TOKENS` (0 = none) to `total_tokens_limit`; `MCP_CALL_TIMEOUT_SECONDS` (30) per tool call; `AI_REQUEST_TIMEOUT_SECONDS` (60) per model call.

### Request lifecycle (controller -> service -> store)

`api/chat.py` (`chat`, `confirm`) -> `ChatService.chat/confirm` (`agent/service.py`) -> `McpRegistry.open` + Pydantic AI agent -> `ServerToolset.call_tool` (`mcp/policy.py`) -> MCP server; persistence via `ConversationStore` / `ConfirmationStore` (`db/`). There is no DAO layer: stores are used directly by `ChatService` and the conversations router.

## 3. Sequence diagrams

### 7a. Login: session validation

```mermaid
sequenceDiagram
    autonumber
    actor U as Browser
    participant P as bahmni-proxy
    participant C as AiServiceController (bahmni-core)
    participant A as api.deps.current_user
    participant V as auth.session.SessionValidator
    participant O as OpenMRS /session
    U->>P: POST /openmrs/ws/rest/v1/bahmnicore/ai/chat (JSESSIONID cookie)
    P->>C: forward
    C->>C: isAuthenticated() and Context.hasPrivilege("Use AI Chatbot")
    alt not allowed
        C-->>U: 403 {"detail":{"code":"forbidden"}}
    end
    C->>A: POST /bahmni-ai/chat, header X-OpenMRS-Session-Id (cookie not forwarded)
    A->>V: validate(header, request_id)
    alt cache hit (TTL 30 s, key sha256(session id))
        V-->>A: UserContext
    else miss
        V->>O: GET /openmrs/ws/rest/v1/session?v=custom:(...) Cookie JSESSIONID
        O-->>V: authenticated, user uuid, privileges, roles
        V-->>A: UserContext (cached)
    end
    alt 401/403 or not authenticated
        A-->>C: 401 session_expired
    else OpenMRS unreachable or other status
        A-->>C: 502 openmrs_unavailable
    else lacks gate privilege (unless role System Developer)
        A-->>C: 403 forbidden
    end
```

bahmni-core has already authenticated the user. bahmni-ai still calls OpenMRS because "the compose network is not a trust boundary" (`docs/PLAN.md`). The privileges and roles come from the OpenMRS response, never from the request or the model.

### 7b. Main workflow: chat turn with a read tool

```mermaid
sequenceDiagram
    autonumber
    participant API as api.chat.chat
    participant S as agent.service.ChatService
    participant ST as ConversationStore
    participant R as mcp.registry.McpRegistry
    participant AG as Pydantic AI Agent (RetryingModel)
    participant T as mcp.policy.ServerToolset
    participant PC as auth.privileges.PrivilegeChecker
    participant M as MCP server
    participant L as LLM provider
    API->>S: chat(user, message, conversationId)
    S-->>API: event conversation {conversationId}
    S->>ST: load(user_uuid, cid)
    S->>S: history.from_storage then trim_to_budget(AI_HISTORY_MAX_TOKENS)
    S->>S: asyncio.create_task(_produce), events through asyncio.Queue
    S->>R: open(user): per-run connections with auth headers
    R-->>S: toolsets + unavailable list
    opt some server failed to connect
        S-->>API: event servers_unavailable
    end
    S->>AG: run_stream_events(prompt, history, toolsets, UsageLimits)
    AG->>L: request (instructions rendered by PromptRenderer)
    L-->>AG: stream parts
    AG-->>S: PartDeltaEvent
    S-->>API: event text {delta}
    AG->>T: call_tool(server__tool, args)
    S-->>API: event tool_started
    T->>T: jsonschema validate + argumentConstraints
    T->>PC: missing([gate, tool privilege])
    PC-->>T: [] (cached session)
    T->>M: tools/call (timeout MCP_CALL_TIMEOUT_SECONDS, never retried)
    M-->>T: result
    T->>T: audit.record(ToolAudit)
    T-->>AG: result
    S-->>API: event tool_finished {ok}
    AG->>L: request with tool result
    L-->>AG: final text
    S->>ST: save(history.for_storage(messages, HISTORY_TOOL_RESULTS), title)
    S-->>API: event done
```

### 7c. Write with confirmation

```mermaid
sequenceDiagram
    autonumber
    actor U as User (chat UI)
    participant API as api.chat
    participant S as ChatService
    participant T as ServerToolset.call_tool
    participant PC as PrivilegeChecker
    participant CS as ConfirmationStore
    participant M as MCP server
    U->>API: POST /chat "create a FHIR Patient ..."
    API->>S: chat(...)
    S->>T: model asks fhir__create
    T->>PC: missing(gate + 'AI: Access FHIR Data')
    T->>T: rule.confirm and not tool_call_approved
    T-->>S: raise ApprovalRequired (audit: confirmation_required)
    S->>S: output is DeferredToolRequests
    S->>CS: put(PendingConfirmation id, user_uuid, messages_json, actions_json, expires_at = now + 300 s)
    S-->>U: event confirmation_required {confirmationId, expiresInSeconds, actions[server, tool, arguments]}
    S-->>U: event done (ChatService._run always yields Done after the queue ends, no tool has executed yet)
    U->>API: POST /chat/confirm {confirmationId, approved, decisions}
    API->>S: confirm(user, id, approved, decisions)
    S->>CS: take(user_uuid, id)  (DELETE ... RETURNING, single use)
    alt unknown, other user's or expired
        S-->>U: event error confirmation_expired
    else found
        S->>S: DeferredToolResults: True or ToolDenied per action
        S->>T: resume run, tool_call_approved = true
        T->>PC: missing(..., fresh=False)
        T->>PC: missing(..., fresh=True) bypass cache for writes
        T->>M: tools/call (once, with timeout)
        M-->>T: result
        T->>T: audit allowed/success
        S-->>U: text, tool_finished, done
    end
```

The model asking is not consent. The decision to run a write is made by the user through `/chat/confirm`, validated against the stored, user-bound, single-use record, and then the privilege is re-checked uncached right before execution.

### 7d. Startup and config loading

```mermaid
sequenceDiagram
    autonumber
    participant UV as server.main (uvicorn)
    participant CA as main.create_app
    participant LS as lifespan
    participant CFG as mcp.config.load_config
    participant CR as EnvCredentialResolver
    participant VC as mcp.auth.validate_credentials
    participant RG as McpRegistry
    participant DB as db.postgres.init_postgres_stores
    participant PR as prompts.PromptResolver
    UV->>CA: create_app(get_settings())  (env + .env)
    CA->>CA: configure_logging, observability.setup (never fatal)
    UV->>LS: startup
    LS->>LS: httpx.AsyncClient + SessionValidator
    LS->>CR: allowlist = CREDENTIAL_ALLOWLIST
    LS->>CFG: read MCP_CONFIG_PATH (/etc/bahmni_config/ai/mcp-servers.json)
    CFG->>CR: expand ${MCP_*} in url fields only
    CFG->>CFG: pydantic validation (extra=forbid), stdio gate, TLS rule, write needs privilege
    CFG-->>LS: McpConfig or ConfigError (startup aborts)
    LS->>VC: every credentialRef / envRef must resolve (fail closed)
    LS->>RG: start() (stdio servers only)
    LS->>DB: DATABASE_URL set? create_engine + create_all (else in-memory, warning)
    LS->>PR: build_resolver, start() initial fetch (optional hub)
    LS->>LS: build_agent(AI_MODEL) with PromptRenderer.render as instructions
    LS-->>UV: app.state.chat_service ready, serve requests
    Note over LS: shutdown: close stores, prompt refresher, registry, telemetry (bounded)
```

Any `ConfigError`, `CredentialError`, `ModelConfigError` or `PromptSourceError` aborts startup (nothing half-configured serves requests). Telemetry setup failure does not.

### 7e. Provider retry and fallback

```mermaid
sequenceDiagram
    autonumber
    participant S as ChatService._produce
    participant F as FallbackModel
    participant R1 as RetryingModel(anthropic)
    participant R2 as RetryingModel(openai)
    participant L1 as Anthropic API
    participant L2 as OpenAI API
    participant U as UI
    S->>F: request (AI_MODEL="anthropic:..., openai:...")
    F->>R1: request
    R1->>L1: call
    L1-->>R1: 429 / 5xx / connection error (before streaming started)
    R1-->>U: activity retry started (attempt n) via retry_listener
    R1->>R1: sleep Retry-After or exponential backoff (max AI_MAX_RETRIES)
    R1->>L1: call again
    L1-->>R1: still failing
    R1-->>F: ModelAPIError
    F->>R2: next model
    R2->>L2: call
    L2-->>R2: stream
    R2-->>S: stream
    Note over S,U: if all fail: FallbackExceptionGroup -> error rate_limited (all 429) or provider_error. Once streaming has begun a failure is reported, never replayed.
```

### 7f. Prompt hub refresh and rendering

```mermaid
sequenceDiagram
    autonumber
    participant LS as lifespan
    participant PR as PromptResolver
    participant OL as OpenLITPromptSource
    participant HUB as OpenLIT /api/prompt/get-compiled
    participant RN as PromptRenderer
    participant AG as Agent (instructions callable)
    LS->>PR: start()
    PR->>OL: fetch(PromptRef name, version)
    OL->>HUB: POST shouldCompile=true, Bearer OPENLIT_API_KEY
    HUB-->>OL: 200 {err, res}
    OL-->>PR: FetchedPrompt(text, version)
    PR->>PR: length <= PROMPT_HUB_MAX_CHARS else keep previous
    loop every PROMPT_HUB_REFRESH_SECONDS (background task)
        PR->>OL: fetch
        alt failure
            PR->>PR: keep last good prompt (or built-in)
        end
    end
    Note over AG,RN: on every chat run
    AG->>RN: render()
    RN->>PR: current() (no I/O): append or replace mode
    RN->>RN: fill {{current_datetime}} in TIMEZONE, else append date line
    RN-->>AG: final instructions
```

### 7g. Conversation list, read, delete

```mermaid
sequenceDiagram
    autonumber
    actor U as UI via bahmni-core
    participant API as api.conversations
    participant D as api.deps.current_user
    participant ST as ConversationStore
    participant H as agent.history
    U->>API: GET /bahmni-ai/conversations?limit&offset
    API->>D: validate session + gate privilege
    API->>ST: list(user_uuid, limit, offset) WHERE deleted_at IS NULL ORDER BY updated_at DESC
    API-->>U: {conversations[{id,title,createdAt,updatedAt}]}
    U->>API: GET /conversations/{id}
    API->>ST: get(user_uuid, id)
    API->>H: to_transcript(from_storage(messages_json)) (no tool args or results)
    API-->>U: summary + messages[]
    U->>API: DELETE /conversations/{id}?hard=false|true
    API->>ST: delete(user_uuid, id, hard)
    alt not found or someone else's
        API-->>U: 404 not_found
    else
        API-->>U: 204 (soft = deleted_at set, hard = row removed)
    end
```

## 4. Flowcharts

### 8a. Tool visibility (discovery)

```mermaid
flowchart TD
    A["Tool discovered on MCP server<br/>ServerToolset.get_tools -> resolve_tool(server, tool, annotations)"] --> B{"matches tools.deny?<br/>(fnmatch)"}
    B -->|yes| H["Hidden from the model"]
    B -->|no| C{"entry in tools.overrides?"}
    C -->|yes| D["Visible with the override's access, confirm,<br/>requiredPrivilege or server.requiredPrivilege, argumentConstraints"]
    C -->|no| E{"matches tools.allow?"}
    E -->|no| H
    E -->|yes| F{"server.trustToolAnnotations and readOnlyHint true<br/>and destructiveHint not true?"}
    F -->|yes| G["read, no confirm, privilege = server.requiredPrivilege (may be none)"]
    F -->|no| I{"server.requiredPrivilege set?"}
    I -->|yes| J["treated as write + confirm<br/>(even if it is only a search)"]
    I -->|no| H
```

Discovery grants nothing: a tool is visible only if config says so. Note the shipped `fhir` server uses `overrides` (so `allow` is empty), and `confluence` uses `allow: ["*"]` without `trustToolAnnotations`, which lands every tool in the "write + confirm" branch.

### 8b. Tool call policy pipeline

```mermaid
flowchart TD
    S["ServerToolset.call_tool(name, args)"] --> R{"name in self.resolved?"}
    R -->|no| D1["denied: unknown tool (audit)"]
    R -->|yes| V{"jsonschema.validate(args, tool schema)"}
    V -->|fail| IV["return error invalid_arguments (audit)"]
    V -->|ok| AC{"argumentConstraints satisfied?"}
    AC -->|no| D2["denied: argument not permitted (audit)"]
    AC -->|yes| P{"PrivilegeChecker.missing(gate + tool privilege)<br/>validated against OpenMRS, cached up to 30 s"}
    P -->|missing| D3["denied: permission_denied (audit)"]
    P -->|ok| CF{"rule.confirm and not ctx.tool_call_approved?"}
    CF -->|yes| AR["raise ApprovalRequired<br/>-> confirmation_required event"]
    CF -->|no| W{"rule.access == write?"}
    W -->|yes| FR{"missing(..., fresh=True)?"}
    FR -->|missing| D4["denied at execution (audit)"]
    FR -->|ok| EX
    W -->|no| EX["execute once inside asyncio.timeout(MCP_CALL_TIMEOUT_SECONDS)"]
    EX -->|TimeoutError| TO["return timeout error: result unknown, do not repeat"]
    EX -->|exception| ER["return tool_failed with redacted message[:300]"]
    EX -->|ok| OK["audit allowed/success, return result"]
```

### 8c. Chat turn outcome and error mapping

```mermaid
flowchart TD
    A["ChatService.chat"] --> B{"message empty or > 8000 chars?"}
    B -->|yes| E0["error invalid_request"]
    B -->|no| C["conversation event, load + trim history"]
    C --> D["_produce task: open MCP servers, run agent"]
    D --> X{"outcome"}
    X -->|"final text"| S["save conversation (redacted), then done"]
    X -->|"DeferredToolRequests"| CR["store PendingConfirmation, confirmation_required, then done"]
    X -->|SessionExpiredError| E1["error session_expired"]
    X -->|OpenmrsUnavailableError| E2["error openmrs_unavailable"]
    X -->|UsageLimitExceeded| E3["error step_limit"]
    X -->|"ModelAPIError / FallbackExceptionGroup"| P{"all statuses 429?"}
    P -->|yes| E4["error rate_limited"]
    P -->|no| E5["error provider_error"]
    X -->|UnexpectedModelBehavior| E6["error assistant_error"]
    X -->|TimeoutError| E7["error timeout"]
    X -->|other| E8["error internal_error (details only in logs)"]
```

### 8d. History trimming and storage redaction

See [03-data-model.md](03-data-model.md#5-what-goes-into-messages_json).

## 5. State diagrams

Confirmation, conversation and prompt/server states are in [03-data-model.md](03-data-model.md#6-lifecycles).

## 6. Security-relevant behaviours

- Identity is taken only from the validated session. `test_spoofed_identity_headers_are_ignored` covers `X-User-Id`-style headers.
- A validated session id longer than 256 chars, non-printable, empty or containing `;` is rejected before any OpenMRS call (`SessionValidator.validate`).
- The user's session id is sent to MCP servers only for `auth.type = "openmrs-session"` and only over https/local http (`ServerConfig._transport_fields`).
- Trace context goes to MCP servers only when `propagateTrace: true`.
- A tool that times out returns `timeout` with "result is unknown, do not repeat" and is not retried (writes are not assumed idempotent).

## Observations

- The SSE stream always ends with `done` except after `error`; the activity docstring says the UI clears activities on `done`, `error` or `confirmation_required`. Because `done` follows `confirmation_required`, a client that waits for `done` still works.
- `chat()` yields `ConversationStarted` before loading history, so a failing store load surfaces as a stream error after the first event rather than an HTTP error.
- A client can choose any `conversationId`; isolation comes from the `(user_uuid, id)` primary key, not from the id's secrecy.
