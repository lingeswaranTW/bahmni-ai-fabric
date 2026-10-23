# Docs vs Code discrepancies

Rule applied: **code wins**. Each row gives the doc claim, what the code actually does, and file references. Severity: **H** can mislead an operator into a failure, **M** wrong but low impact, **L** stale or cosmetic.

## Coverage of this check

Read and compared: `README.md`, `docs/PLAN.md`, `docs/REPOS.md`, `docs/OPERATIONS.md` (env table, API and error-code sections), `CHANGELOG.md`, `.env.example`, `pyproject.toml` comments, `standard-config/ai/README.md`. **Not compared line by line** (only existence-checked): `docs/OBSERVABILITY.md`, `docs/OBSERVABILITY_AUDIT.md`, `docs/OBSERVABILITY_PHASE0_RESULT.md`, `docs/PROVIDERS.md`, `docs/PROMPTS.md`, `docs/STAGE0_SPIKE_RESULT.md`, `deploy/openlit/README.md`. Treat their agreement with the code as **Unverified**.

## Discrepancies

| # | Sev | Doc claim | Actual behaviour (code) | References |
|---|---|---|---|---|
| 1 | H | `docs/REPOS.md`: standard-config ships `ai/mcp-servers.json` "(default: no servers)". | The shipped file declares three servers (`documents`, `fhir`, `confluence`) and needs `MCP_DOCUMENTS_URL`, `MCP_FHIR_URL` and `MCP_ATLASSIAN_TOKEN` to be non-empty or the service **will not start** (`resolve_plain`/`resolve` reject unset *and* empty values). The empty-server file is `dev-config/mcp-servers.json` in this repo. | `standard-config/ai/mcp-servers.json`; `src/bahmni_ai/mcp/credentials.py`; `mcp/auth.py::validate_credentials`; `dev-config/mcp-servers.json` |
| 2 | M | `standard-config/ai/README.md`: `trustToolAnnotations` "Set `true` only for first-party servers (here: `documents`, `fhir`)". | Only `documents` sets `trustToolAnnotations: true`. `fhir` does not (it relies on explicit `overrides`, so behaviour is unaffected, but the README is wrong). | `standard-config/ai/mcp-servers.json`; `mcp/policy.py::resolve_tool` |
| 3 | M | `docs/PLAN.md` Stage 8: "`db/` with Postgres models and migrations". | No migrations exist; schema comes from `Base.metadata.create_all` at startup, which never alters existing tables. | `db/postgres.py::init_postgres_stores` |
| 4 | M | `docs/PLAN.md` config contract uses `${DOCUMENTS_MCP_URL}`, `DOCUMENTS_MCP_TOKEN`, `${OPENMRS_MCP_URL}`. | Such names are rejected: only variables starting with `MCP_` (or in `CREDENTIAL_ALLOWLIST`) may be referenced. Real names are `MCP_DOCUMENTS_URL` etc. | `mcp/credentials.py::_allowed`; `.env.example` |
| 5 | M | `docs/PLAN.md`: stdio only "behind an explicit `allowStdio` setting". | The setting is the env var `MCP_ALLOW_STDIO` (`Settings.mcp_allow_stdio`). | `settings.py`; `main.py` |
| 6 | M | `docs/PLAN.md`: compose/env names `AI_SERVICE_URL`, `AI_SERVICE_IMAGE_TAG`, `AI_SERVICE_PORT`. | Implemented names are `BAHMNI_AI_SERVICE_URL`, `BAHMNI_AI_IMAGE_TAG`, `BAHMNI_AI_PORT`. | bahmni-docker `.env`; `docs/REPOS.md`; README |
| 7 | L | `docs/PLAN.md`: API is `POST /chat`, `POST /chat/confirm`, `GET /health`. | Also `GET /conversations`, `GET /conversations/{id}`, `DELETE /conversations/{id}`. | `api/conversations.py` |
| 8 | L | `docs/PLAN.md`: service name "still to decide (for example `bahmni-ai-chat`)"; "The folder is currently empty... greenfield". | Name settled as `bahmni-ai` (package, image `bahmni/bahmni-ai`, URL prefix). The plan header is historical. | `pyproject.toml`; `settings.py` |
| 9 | L | `docs/REPOS.md`: bahmni-core change is a "streaming proxy `POST .../ai/chat` and `/chat/confirm`". | The Java controller also proxies `GET /health`, `GET /conversations`, `GET /conversations/{id}`, `DELETE /conversations/{id}`. | `bahmni-core/.../AiServiceController.java` |
| 10 | M | README intro: "A tool runs only if the signed-in OpenMRS user holds the OpenMRS privilege mapped to it." | Every call needs the gate privilege (`Use AI Chatbot`); a *per-tool* privilege exists only if configured. A read override with no `requiredPrivilege` (allowed by validation) runs on the gate privilege alone. Only writes are forced to name a privilege. The role `System Developer` bypasses all privilege checks. | `mcp/policy.py::call_tool` (`needed = [gate, rule.privilege]`); `mcp/config.py::_transport_fields`; `auth/context.py::is_superuser` |
| 11 | L | README "How it fits" diagram: MCP servers `documents (service token)`, `jira (service token)`, `openmrs (user session)`. | That is illustrative; the shipped config is `documents` (no auth), `fhir` (user session), `confluence` (bearer). Jira/OpenMRS appear only in the example file. | `standard-config/ai/mcp-servers.json`, `mcp-servers.example.json` |
| 12 | M | README "Build the image": publish by pushing a `v*` tag; `docker build -t bahmni/bahmni-ai:0.1.0`; bahmni-docker defaults `BAHMNI_AI_IMAGE_TAG=0.1.0`. | `build_publish.yml` pushes `bahmni/bahmni-ai:${{ github.ref_name }}`, i.e. `v0.1.0` for tag `v0.1.0` (plus `latest`). A `0.1.0` tag is produced only if the git tag is literally `0.1.0`, which would not match the `v*` trigger. | `.github/workflows/build_publish.yml`; bahmni-docker `docker-compose.yml` |
| 13 | L | README/OPERATIONS settings tables. | `TIMEZONE` and `AI_INJECT_DATETIME` are not in the README/OPERATIONS tables (they are in `.env.example` and `docs/PROMPTS.md`); `PROMPT_HUB_*` are only referenced, not tabulated, in OPERATIONS; `TELEMETRY_SESSION_KEY` is in README but not OPERATIONS. | `settings.py`; `.env.example` |
| 14 | L | `pyproject.toml` comment: "OpenAI, Anthropic and Ollama are always installed." | Ollama has no own dependency; it uses the OpenAI SDK pulled by the `openai` extra, so the statement holds. The Dockerfile also installs only the `telemetry` extra, so Gemini is **not** available in the published image. | `pyproject.toml`; `Dockerfile` |
| 15 | L | `docs/PLAN.md` architecture sketch labels the service "ai-server" and a flat `chat`-only `api/`. | Current layout adds `providers/`, `prompts/`, `observability/`, `urlsafety.py`. | `src/bahmni_ai/` |

## Verified consistent (no discrepancy found)

- SSE event names and the error-code list in `docs/OPERATIONS.md` match `agent/events.py` and `ChatService`.
- Endpoint list, parameter limits and soft/hard delete semantics in the README match `api/*`.
- Defaults in the OPERATIONS env table for `PORT`, `OPENMRS_URL`, `MCP_CALL_TIMEOUT_SECONDS`, `CONFIRMATION_TTL_SECONDS`, `HISTORY_TOOL_RESULTS`, `GATE_PRIVILEGE`, `TELEMETRY_*` match `settings.py`.
- README project layout matches `src/bahmni_ai/`.
- CHANGELOG "store.py moved to db/store.py", `HOST` default `127.0.0.1` and image `HOST=0.0.0.0`, and the `trustToolAnnotations` behaviour change match the code.
- `standard-config/ai/README.md` rules (server names, auth types, write needs confirm + privilege, https/local-http rule, `MCP_*` only) match `mcp/config.py` and `mcp/credentials.py`.

## Unverified

- Whether `bahmni-proxy` buffers SSE, and the `ai.service.url` template in `openmrs-distro-bahmni` (a search for `ai.service` there found nothing; the property may live under another file name).
- Runtime behaviour of the full Bahmni stack with the `bahmni-ai` profile (not started).
- `AiServiceClient`'s streaming implementation (only the controller's routes and header handling were read).
