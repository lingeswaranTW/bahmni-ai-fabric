# bahmni-ai

A reusable AI chatbot backend for [Bahmni](https://www.bahmni.org/). It lets clinic staff chat with an assistant
that can use tools exposed by any [MCP](https://modelcontextprotocol.io/) server (documents, OpenMRS, Jira, Slack,
and so on), with a choice of model providers (OpenAI, Anthropic, Ollama, and others supported by
[Pydantic AI](https://ai.pydantic.dev/)).

- A tool runs only if the signed-in **OpenMRS user** holds the OpenMRS privilege mapped to it.
- Sensitive actions (writes) need the user's explicit confirmation, and every tool call is audited.
- MCP servers, their credentials (by reference) and the tool policy are configured in
  [standard-config](https://github.com/Bahmni/standard-config) (`ai/mcp-servers.json`). Adding a server needs no code.
- Switching model provider is a configuration change (`AI_MODEL`).

More docs: [Operator guide](docs/OPERATIONS.md) · [Observability design](docs/OBSERVABILITY.md) · [Where each piece lives](docs/REPOS.md) ·
[Plan and status](docs/PLAN.md) · [Stage 0 spike result](docs/STAGE0_SPIKE_RESULT.md)

## How it fits into Bahmni

```
Browser ──JSESSIONID──> bahmni-proxy ──> openmrs (bahmni-core)            bahmni-ai ──> MCP servers
                                           │ POST /openmrs/ws/rest/v1/        │ ├─ documents  (service token)
                                           │      bahmnicore/ai/chat (SSE)    │ ├─ jira       (service token)
                                           └─ X-OpenMRS-Session-Id ────────> /bahmni-ai/chat   └─ openmrs   (user session)
                                                                               │
                                                                    OpenMRS /session (validates user + privileges)
                                                                    bahmni-ai-db (Postgres: history, confirmations)
```

The browser never talks to this service directly. bahmni-core (already authenticated by OpenMRS) forwards the session
in `X-OpenMRS-Session-Id`, and this service re-validates it against OpenMRS before doing anything. Nothing here is
published on a host port in a Bahmni deployment.

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (development)
- Docker with Compose v2 (image, standalone stack and Bahmni deployment)
- A model provider key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) or a local Ollama (`OLLAMA_BASE_URL`)
- A running Bahmni/OpenMRS to use it for real (tests do not need one)

## Develop

```bash
uv sync                          # install dependencies (creates .venv; dev group includes the telemetry packages)
uv run pytest                    # tests (add TEST_DATABASE_URL=postgresql+asyncpg://... to include Postgres)
uv run ruff check .              # lint
uv run mypy src                  # type check (strict)
```

Run the service locally (no MCP servers, no real model, in-memory history):

```bash
AI_MODEL=test \
MCP_CONFIG_PATH=dev-config/mcp-servers.json \
OPENMRS_URL=http://localhost:8080 \
uv run python -m bahmni_ai.server

curl http://localhost:8090/bahmni-ai/health        # {"status":"UP"}
```

`AI_MODEL=test` is Pydantic AI's built-in fake model. Use a real one such as `anthropic:claude-sonnet-5-5` with the
provider's key set. Chat calls need a valid OpenMRS session id:

```bash
curl -N -X POST http://localhost:8090/bahmni-ai/chat \
  -H 'X-OpenMRS-Session-Id: <JSESSIONID from a logged-in Bahmni session>' \
  -H 'Content-Type: application/json' -d '{"message":"hello"}'
```

The user must hold the OpenMRS privilege `Use AI Chatbot` (setting `GATE_PRIVILEGE`).

## Build the image

```bash
docker build -t bahmni/bahmni-ai:0.1.0 .
```

The image is multi-stage, runs as a non-root user, listens on `PORT` (default `8090`) and serves
`/bahmni-ai/health`. To publish it, push a tag `v*`: `.github/workflows/build_publish.yml` builds and pushes
`bahmni/bahmni-ai:<tag>` (needs `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` repository secrets).

## Run it

### A. Standalone, with its own Postgres (development)

```bash
docker compose -f docker-compose.dev.yml up --build
curl http://localhost:8090/bahmni-ai/health
docker compose -f docker-compose.dev.yml down        # add -v to delete the database volume
```

With several Docker/Colima setups, select one explicitly, for example `docker --context std compose -f docker-compose.dev.yml up --build`.
With Colima, if `localhost:8090` does not answer, the published port may not be forwarded to the host; use the VM
address from `colima status -p <profile>` instead (for example `http://192.168.64.5:8090/bahmni-ai/health`).

This starts the service and a dedicated `bahmni-ai-db` Postgres. Defaults: `AI_MODEL=test`, MCP config from
`dev-config/mcp-servers.json` (no servers), OpenMRS at `host.docker.internal:8080`. Override with environment
variables, for example `AI_MODEL=anthropic:claude-sonnet-5-5 ANTHROPIC_API_KEY=... docker compose -f ... up`.

### B. Inside Bahmni (bahmni-docker, `bahmni-standard`)

The service is an opt-in compose profile, like the template service, and has its **own** database (not the OpenMRS one).

1. Make sure these changes are in place (see [docs/REPOS.md](docs/REPOS.md)): the bahmni-core proxy, the
   `ai.service.url` property in the OpenMRS image, `ai/` in the standard-config image, and the `bahmni-ai` services
   in `bahmni-docker/bahmni-standard/docker-compose.yml`.
2. Set values in `bahmni-docker/bahmni-standard/.env` (non-secret) and in your shell or an untracked env file (secrets):

   | Variable | Where | Meaning |
   |---|---|---|
   | `BAHMNI_AI_IMAGE_TAG`, `BAHMNI_AI_PORT` | `.env` | Image tag and internal port |
   | `BAHMNI_AI_SERVICE_URL` | `.env` | Read by OpenMRS: `http://bahmni-ai:8090/bahmni-ai` |
   | `AI_MODEL` | `.env` | `provider:model` |
   | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `OLLAMA_BASE_URL` | secret | Model provider |
   | `BAHMNI_AI_DB_NAME`, `BAHMNI_AI_DB_USER`, `BAHMNI_AI_DB_PASSWORD` | `.env` / secret | Dedicated Postgres |
   | `BAHMNI_AI_MCP_ENV_FILE` | shell | Path to an untracked file holding `MCP_*_URL` and `MCP_*_TOKEN` values |

3. Start it with the profile:

   ```bash
   cd bahmni-docker/bahmni-standard
   export BAHMNI_AI_DB_PASSWORD=...            # secret, keep out of git
   docker compose --profile emr --profile bahmni-ai up -d
   docker compose logs -f bahmni-ai
   ```

4. In OpenMRS, create the privilege `Use AI Chatbot` and one per sensitive action you map in the config, and assign
   them to roles. Users without `Use AI Chatbot` cannot use the assistant.

Without the `bahmni-ai` profile nothing changes: the proxy simply answers 502 for the AI endpoints.

## Configure MCP servers

Edit `ai/mcp-servers.json` in **standard-config** (not this repo). Credentials are never stored there: the file holds
URLs and *references*, and the values come from environment variables of this container.

```json
{
  "mcpServers": {
    "jira": {
      "url": "${MCP_JIRA_URL}",
      "transport": "streamable-http",
      "auth": {"type": "bearer", "credentialRef": "MCP_JIRA_TOKEN"},
      "requiredPrivilege": "AI: Read Jira",
      "tools": {
        "allow": ["search_*", "get_issue"],
        "overrides": {
          "create_issue": {"access": "write", "confirm": true,
                           "requiredPrivilege": "AI: Create Jira Issue",
                           "argumentConstraints": {"project": ["BAH"]}}
        },
        "default": "deny"
      }
    }
  }
}
```

Only variables starting with `MCP_` can be referenced. The service refuses to start if the file is missing or invalid,
or a referenced credential is not set. Full rules, auth types and the security model are in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## Telemetry (optional)

Traces and metrics (OpenTelemetry over OTLP) for model calls, tool calls and HTTP, with OpenLIT as the viewer. Off by
default; with it off nothing changes. Prompts, answers, tool data, user names, cookies and error text from MCP servers or
providers are **not** exported. Design and evidence: [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).

**1. Start the backend** (separate Docker project, pinned OpenLIT 2.1.0 + ClickHouse; add `DOCKER_CONTEXT=<ctx>` to pick a Docker context):

```bash
cd deploy/openlit
cp .env.example .env.local          # untracked. Set OPENLIT_DB_PASSWORD (any long random string) and OPENLIT_ADMIN_EMAIL
docker compose --env-file .env.local up -d
cd ../..
uv run --with bcrypt python deploy/openlit/harden.py     # once: replaces OpenLIT's default login, checks registration is blocked
```
UI: <http://127.0.0.1:3300>, sign in with `OPENLIT_ADMIN_EMAIL` and the `OPENLIT_ADMIN_PASSWORD` that `harden.py` stored in
`deploy/openlit/.env.local`. OTLP endpoint for the service: `http://127.0.0.1:4318` (loopback only by default).

**2. Point the service at it** (pick one):

```bash
# a) local process
TELEMETRY_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 \
AI_MODEL=test MCP_CONFIG_PATH=dev-config/mcp-servers.json OPENMRS_URL=http://localhost:8080 \
uv run python -m bahmni_ai.server

# b) standalone dev compose (container reaches the host's OpenLIT through host.docker.internal)
TELEMETRY_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318 \
docker compose -f docker-compose.dev.yml up --build

# c) inside Bahmni: in bahmni-docker/bahmni-standard/.env set
#      TELEMETRY_ENABLED=true
#      OTEL_EXPORTER_OTLP_ENDPOINT=http://host.docker.internal:4318     (or the OpenLIT host's internal address)
#    then recreate the service
cd bahmni-docker/bahmni-standard && docker compose --profile emr --profile bahmni-ai up -d bahmni-ai
```
The log line `telemetry_enabled` confirms it started; `telemetry_disabled` (with a reason) means a misconfiguration, and the
service keeps running without telemetry. If the backend needs OTLP headers (a secret), put `OTEL_EXPORTER_OTLP_HEADERS` in the
untracked `BAHMNI_AI_MCP_ENV_FILE`, never in `.env`. If OpenLIT runs on another host, set `OPENLIT_OTLP_BIND` in
`deploy/openlit/.env.local` to that machine's internal address; the OTLP receivers have no authentication.

**3. Use it:** send a chat message, then open the OpenLIT UI and look at traces and metrics for service `bahmni-ai`
(spans `invoke_agent`, `chat`, `execute_tool`, `tools/call`; token and latency metrics). To check without the UI:

```bash
cd deploy/openlit
echo "SELECT SpanName, count() FROM otel_traces WHERE ServiceName='bahmni-ai' GROUP BY SpanName" |
  docker compose --env-file .env.local exec -T clickhouse sh -c \
  'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --database "$CLICKHOUSE_DATABASE"'
```

Each chat message is its own trace, and every turn of one conversation carries the same `session.id` (also set as
`gen_ai.conversation.id`) on its request, agent, model and tool spans. OpenLIT 2.1.0 has no general "Sessions" screen, so
group by that attribute: filter the trace list on it, or in SQL
`SELECT TraceId, min(Timestamp) FROM otel_traces WHERE SpanAttributes['session.id'] = '<id>' GROUP BY TraceId ORDER BY 2`.
The value is a random id by default; `TELEMETRY_SESSION_ID=raw|hmac` makes it derive from the UI conversation id.
Failed tool calls show as ERROR on `execute_tool` (`bahmni.tool.outcome`), failed turns as ERROR on the request span
(`bahmni.error.code`).

**4. Operate:** retention settings, deleting data (`purge.py`), upgrades and stopping are in
[deploy/openlit/README.md](deploy/openlit/README.md). Settings are in the table below and in
[docs/OPERATIONS.md](docs/OPERATIONS.md#telemetry). Sending trace context to a trusted MCP server is opt-in per server
(`"propagateTrace": true` in `ai/mcp-servers.json`); the default is off.

## Configuration reference (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `AI_MODEL` | `anthropic:claude-sonnet-5-5` | `provider:model` (`openai:...`, `anthropic:...`, `ollama:...`, `test`) |
| `OPENMRS_URL` | `http://openmrs:8080` | Used to validate sessions |
| `MCP_CONFIG_PATH` | `/etc/bahmni_config/ai/mcp-servers.json` | MCP configuration file |
| `DATABASE_URL` | empty | `postgresql+asyncpg://user:pw@host:5432/db`. Empty means in-memory history (lost on restart) |
| `GATE_PRIVILEGE` | `Use AI Chatbot` | Privilege every user needs |
| `HISTORY_TOOL_RESULTS` | `none` | `none`, `truncated` or `full`: tool output kept in stored history |
| `MCP_ALLOW_STDIO` | `false` | Allow stdio MCP servers (local processes) |
| `MCP_CALL_TIMEOUT_SECONDS` | `30` | Per tool call |
| `AI_MAX_TOOL_STEPS` | `10` | Model/tool rounds per request |
| `CONFIRMATION_TTL_SECONDS` | `300` | How long a pending confirmation stays valid |
| `TELEMETRY_ENABLED` | `false` | OpenTelemetry traces and metrics (OTLP) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Required when telemetry is on (standard OpenTelemetry variable) |
| `TELEMETRY_CAPTURE_CONTENT` | `false` | Prompts and tool data in spans; synthetic data only |
| `TELEMETRY_EXPORT_TIMEOUT_SECONDS`, `TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS` | `2`, `5` | Bound exports and container stop |
| `TELEMETRY_DISCOVERY_SPANS` | `errors-only` | `all` keeps routine MCP discovery spans |
| `PORT`, `LOG_LEVEL` | `8090`, `info` | |

Tool results can contain clinical data and are sent to the model provider you choose. Use `ollama:` for local-only.

## API

Both endpoints answer with Server-Sent Events and require `X-OpenMRS-Session-Id` (added by bahmni-core).

- `POST /bahmni-ai/chat` body `{"message": "...", "conversationId": "optional"}`
- `POST /bahmni-ai/chat/confirm` body `{"confirmationId": "...", "approved": true, "decisions": {"<actionId>": false}}`
- `GET /bahmni-ai/health`

Events: `conversation`, `text`, `servers_unavailable`, `tool_started`, `tool_finished`, `confirmation_required`,
`error`, `done`. Error codes are listed in [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Project layout

```
src/bahmni_ai/
  main.py, settings.py, logging.py, server.py
  observability/  telemetry facade, scrubbing exporter, trace-context gate (only place that imports OpenTelemetry)
  api/        health, chat (SSE), request authentication
  auth/       OpenMRS session validation, user context, privilege checks
  agent/      Pydantic AI agent, chat service, events, history
  mcp/        config, credential resolver, auth strategies, registry, tool policy
  audit/      structured audit log
  db/         Postgres stores
tests/        pytest (real stub MCP servers over HTTP and stdio)
deploy/       openlit/: pinned, hardened OpenLIT + ClickHouse for telemetry (separate deployment)
spike/        Stage 0 proof of concept
```

## License

MPL-2.0 (see [LICENSE](LICENSE)).
