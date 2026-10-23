# 03 - Data model

Derived from: `src/bahmni_ai/db/postgres.py`, `db/store.py`, `agent/history.py`, `agent/service.py`, `docker-compose.dev.yml`.

## 1. Plain language

The service keeps two small things in its **own** Postgres database (never the OpenMRS one): the chat history of each user, and confirmations that are waiting for the user's click. It stores no credentials and no session ids. The history is only model context, never a source of authorisation. Everything is looked up by the OpenMRS user uuid first, so one user can never reach another's rows.

## 2. Tables

Two tables, declared with SQLAlchemy 2 declarative models (`Base`, `ConversationRow`, `PendingRow`) and created by `Base.metadata.create_all` at startup. **There are no migrations** (no Alembic/Liquibase/Flyway files in the repo).

### `ai_conversation` (`ConversationRow`)

| Column | Type | Notes |
|---|---|---|
| `user_uuid` | `String(64)` PK (part 1) | OpenMRS user uuid |
| `id` | `String(64)` PK (part 2) | Conversation id. Server-generated `uuid4().hex`, or client-supplied matching `^[A-Za-z0-9_-]+$` (max 64) |
| `messages_json` | JSONB on Postgres, JSON elsewhere | Pydantic AI `ModelMessage` list as JSON, tool results redacted per `HISTORY_TOOL_RESULTS` |
| `title` | `String(200)` | First user message, max 80 chars (`history.title_of`); set only at creation |
| `created_at`, `updated_at` | `DateTime(timezone=True)` | `updated_at` has `onupdate`. Note: `save()` of an existing live row only sets `messages_json`; the ORM `onupdate` refreshes `updated_at` |
| `deleted_at` | `DateTime(timezone=True)` NULL | Soft delete marker |

Index: `ix_ai_conversation_user_updated (user_uuid, updated_at)`.

### `ai_pending_confirmation` (`PendingRow`)

| Column | Type | Notes |
|---|---|---|
| `id` | `String(64)` PK | `secrets.token_urlsafe(24)` (`store.new_id`) |
| `user_uuid` | `String(64)` indexed | Owner; `take()` filters on it |
| `conversation_id` | `String(64)` | Logical reference only (no foreign key) |
| `messages_json` | JSON/JSONB | **Full-fidelity** run state (`history.to_json`), not redacted |
| `actions_json` | `Text` | JSON list of `PendingAction{id, server, tool, arguments}` |
| `expires_at` | `Float` indexed | Epoch seconds, `now + CONFIRMATION_TTL_SECONDS` (300) |

## 3. ER diagram

```mermaid
erDiagram
    ai_conversation {
        string user_uuid PK "String(64), OpenMRS user uuid"
        string id PK "String(64), client or server generated"
        json messages_json "JSONB on Postgres, JSON elsewhere"
        string title "String(200)"
        datetime created_at "timezone aware"
        datetime updated_at "onupdate"
        datetime deleted_at "NULL = live, soft delete marker"
    }
    ai_pending_confirmation {
        string id PK "secrets.token_urlsafe(24)"
        string user_uuid "indexed"
        string conversation_id "logical reference, no FK"
        json messages_json "full fidelity run state"
        text actions_json "list of PendingAction as JSON text"
        float expires_at "epoch seconds, indexed"
    }
    ai_conversation ||..o{ ai_pending_confirmation : "same user_uuid + conversation_id (not enforced)"
```

There is no database-level relationship: `ai_pending_confirmation.conversation_id` points at a conversation by convention, and a confirmation can outlive or precede the conversation row (a brand-new conversation is only saved after the turn finishes without confirmation needed, see `ChatService._finish`).

## 4. Store contracts

`db/store.py` defines two `Protocol`s, each with an in-memory and a Postgres implementation:

| Protocol | Memory | Postgres | Behaviour |
|---|---|---|---|
| `ConversationStore` (`load`, `save`, `list`, `get`, `delete`) | `MemoryConversationStore` | `PostgresConversationStore` | Every call is owner scoped. Soft-deleted rows are invisible to all methods. Saving to a soft-deleted id resets the row (fresh conversation). |
| `ConfirmationStore` (`put`, `take`) | `MemoryConfirmationStore` | `PostgresConfirmationStore` | `take` is single use: Postgres does one `DELETE ... WHERE id AND user_uuid RETURNING`, so two instances cannot both win. Unknown id and someone else's id are indistinguishable (`None`). `put` first deletes expired rows. |

`init_postgres_stores(database_url)` creates the async engine (`pool_pre_ping=True`) and calls `create_all`. Any SQLAlchemy async dialect works; tests use SQLite via `aiosqlite` (`tests/test_db_store.py` parametrises sqlite and, if `TEST_DATABASE_URL` is set, postgres).

## 5. What goes into `messages_json`

Pydantic AI `ModelMessagesTypeAdapter` JSON: user prompts, assistant text, tool calls, tool returns, timestamps, and the stored `conversation_id` (the telemetry session id). `agent/history.py` operates on it:

```mermaid
flowchart TD
    A["Stored history (full) from ConversationStore.load"] --> B["trim_to_budget(AI_HISTORY_MAX_TOKENS, ~4 chars/token)"]
    B --> C{"budget 0?"}
    C -->|yes| D["send everything"]
    C -->|no| E["keep newest turn always,<br/>add older whole turns (turn = starts at a user prompt) while they fit"]
    D --> M["model run"]
    E --> M
    M --> N["messages = full_history + new_messages (storage keeps everything)"]
    N --> O{"HISTORY_TOOL_RESULTS"}
    O -->|none default| P["ToolReturnPart.content = '[tool result not stored]'"]
    O -->|truncated| Q["redact_value, cut to 2000 chars"]
    O -->|full| R["unchanged"]
    P --> Z["ConversationStore.save"]
    Q --> Z
    R --> Z
    N -->|"confirmation needed"| F["history.to_json full fidelity -> ai_pending_confirmation"]
```

`to_transcript` (used by `GET /conversations/{id}`) shows only user text, assistant text and the names of tools used, never tool arguments or results, system prompts or retry prompts.

## 6. Lifecycles

### Confirmation

```mermaid
stateDiagram-v2
    [*] --> Pending: ChatService._finish stores PendingConfirmation (ttl = CONFIRMATION_TTL_SECONDS, default 300)
    Pending --> Consumed: POST /chat/confirm -> ConfirmationStore.take (atomic, single use)
    Pending --> Expired: expires_at passed
    Expired --> [*]: take() returns None, error confirmation_expired. Row purged on next put()
    Consumed --> Executed: action approved (decisions[id] or approved)
    Consumed --> Denied: ToolDenied "The user declined this action."
    Executed --> [*]: run resumes, conversation saved
    Denied --> [*]: model told the user declined
```

### Conversation

```mermaid
stateDiagram-v2
    [*] --> Live: save() creates row (deleted_at NULL)
    Live --> Live: save() on later turns (messages_json, updated_at)
    Live --> SoftDeleted: DELETE /conversations/{id} sets deleted_at
    SoftDeleted --> Live: save() with the same id (fresh conversation, old content overwritten)
    SoftDeleted --> HardDeleted: DELETE ?hard=true
    Live --> HardDeleted: DELETE ?hard=true
    HardDeleted --> [*]
    note right of SoftDeleted: invisible to load, get, list. No retention job removes these rows
```

### Prompt text and MCP server availability (non-persisted)

```mermaid
stateDiagram-v2
    state "Prompt resolver" as PRS {
        [*] --> Builtin: start (hub disabled or first fetch pending)
        Builtin --> HubPrompt: refresh() succeeded
        HubPrompt --> HubPrompt: refresh() succeeded (new text or version)
        HubPrompt --> HubPrompt: refresh() failed, last good prompt kept
        Builtin --> Builtin: refresh() failed
    }
    state "MCP server per chat run" as SRV {
        [*] --> Connecting: McpRegistry.open
        Connecting --> Available: auth headers built and initialised within connect timeout
        Connecting --> Unavailable: any exception, listed in servers_unavailable
        Available --> [*]: run ends, connection closed
        Unavailable --> [*]
    }
```

## 7. Other data (not in the database)

| Data | Where | Lifetime |
|---|---|---|
| Session -> `UserContext` | `SessionValidator._cache` (memory), key sha256(session id) | 30 s, max 1024 entries, cleared when full |
| Tool audit trail | structured log line `tool_audit` on stdout | Whatever the log pipeline keeps |
| Hub prompt text | `PromptResolver._hub_text` (memory) | Until next refresh |
| Telemetry | OpenLIT/ClickHouse (separate deployment, `TRACE_RETENTION_HOURS` 168, `METRIC_RETENTION_HOURS` 720 by default) | Retention env vars in `deploy/openlit/docker-compose.yml` |

## Observations

- No migration tool: schema changes will not apply to an existing database; `create_all` never alters tables.
- Soft-deleted conversations and expired confirmations have no scheduled cleanup (expired confirmations are removed lazily on the next `put`).
- Messages are stored as one JSON document per conversation and rewritten in full each turn; fine for small chats, grows linearly with history (storage keeps everything even though the model sees a trimmed window).
- The pending confirmation row contains un-redacted tool results for up to 5 minutes.
