# Changelog

## Unreleased

### Added
- Prompt placeholders (one placeholder, `{{current_datetime}}`: weekday, date, time, zone) filled per
  request in the clinic `TIMEZONE`, plus an automatic date line (`AI_INJECT_DATETIME`). Guide: `docs/PROMPTS.md`.
- Provider layer (`bahmni_ai.providers`): adapter contract, registry, built-in `anthropic`, `openai`, `ollama`, `gemini`
  (optional extra) and `test`, entry-point plugins (`bahmni_ai.providers`). Guide: `docs/PROVIDERS.md`.
- `activity` SSE events (model / thinking / tool / retry, started / finished): temporary, content-free progress lines the UI shows
  while a reply is produced (shown verbatim, one line per event) and removes when it is ready. `UI_ACTIVITY_EVENTS=false` switches them off. The chat UI in
  bahmni-apps-frontend renders them.
- Optional system prompt from OpenLIT's Prompt Hub (`PROMPT_HUB_*`), `append` or `replace` mode, background refresh with
  last-good fallback, pluggable prompt sources (`bahmni_ai.prompt_sources`). Guide: `docs/PROMPTS.md`.
- `chat_usage` log line carries `prompt_version`.
- `openai-compat` provider for OpenAI Chat Completions compatible servers (`OPENAI_COMPAT_BASE_URL`).
- Live smoke test: `python -m bahmni_ai.providers.smoke`, opt-in `pytest -m live`, manual `live_providers` workflow.
- `AI_MODEL` accepts a comma-separated list: fallback to the next provider.
- Retry with exponential backoff and `Retry-After` for transient provider failures (`AI_MAX_RETRIES` and related settings).
- `rate_limited` error code; every provider failure type (not only HTTP errors) is mapped to a client-safe error.
- History token budget (`AI_HISTORY_MAX_TOKENS`), per-request token cap (`AI_MAX_TOTAL_TOKENS`), `chat_usage` log line.
- `HOST` setting; Docker image `HEALTHCHECK`, pinned base image and uv.
- MCP server option `trustToolAnnotations`.
- CI: Postgres service, coverage gate, dependency audit, image scan; Dependabot; publish waits for validation.

### Changed
- **Behaviour change:** a server's `readOnlyHint` is no longer believed unless the server sets `trustToolAnnotations: true`.
  Allowed tools of other servers now need confirmation unless listed under `overrides` with `"access": "read"`.
- **Behaviour change:** credentials and the user's session are refused over plain `http` to remote hosts (https, localhost,
  `host.docker.internal` or a container name only).
- **Behaviour change:** the default listen address is `127.0.0.1` outside the image (the image sets `HOST=0.0.0.0`).
- OpenMRS session debug log no longer includes the user's name and privileges.
- `store.py` moved to `db/store.py`.
- The agent run now executes in its own task and streams to the client through a queue (needed for live retry notices; it also
  cancels the run when the client disconnects).
- App startup closes anything it already started when a later startup step fails (it used to leak MCP/DB resources).
- Tests no longer read a developer's `.env` or exported variables.
- `.idea/` is no longer tracked; the `spike/` proof-of-concept folder was removed (results stay in `docs/`).
