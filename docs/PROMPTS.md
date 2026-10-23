# System prompt source (prompt hub)

By default the assistant uses the built-in system prompt in `src/bahmni_ai/agent/prompts.py`. With the prompt hub switched
on, the prompt is managed in an observability or prompt-management service (OpenLIT today) and edited there, with no code
change, new image or restart. Like the model providers, the service is a plug-in: another service needs one adapter.

```
PROMPT_HUB_ENABLED=true
        │ startup: fetch (bounded by PROMPT_HUB_TIMEOUT_SECONDS)
        ▼
  PromptSource (prompts/openlit.py)  ──►  PromptResolver  ──current()──►  Agent (every run)
        ▲                                    │   ▲
        └──── refresh every PROMPT_HUB_REFRESH_SECONDS (background task) ┘
```

`current()` does no I/O: a slow or dead prompt service can never delay a chat.

## Switches

| Variable | Default | Meaning |
|---|---|---|
| `PROMPT_HUB_ENABLED` | `false` | Master switch. Off: the built-in prompt, exactly as before |
| `PROMPT_HUB_MODE` | `append` | `append`: the built-in safety prompt is kept and the hub text is added after it. `replace`: the hub text is the whole prompt |
| `PROMPT_HUB_SOURCE` | `openlit` | Which adapter to use (see "Adding another source") |
| `PROMPT_HUB_URL` | empty | Base URL of the service, e.g. `http://openlit:3000` |
| `PROMPT_HUB_PROMPT_NAME` | empty | Name of the prompt in the hub (required when enabled) |
| `PROMPT_HUB_PROMPT_VERSION` | empty | Pin a version, e.g. `1.2.0`. Empty = the latest published version |
| `PROMPT_HUB_REFRESH_SECONDS` | `300` | How often the prompt is re-fetched (minimum 5) |
| `PROMPT_HUB_TIMEOUT_SECONDS` | `3` | Timeout of one fetch |
| `TIMEZONE` | `UTC` | Clinic time zone for the date placeholders (see Placeholders). Works with or without the hub |
| `AI_INJECT_DATETIME` | `true` | Append a date line when the prompt has no date placeholder |
| `PROMPT_HUB_MAX_CHARS` | `8000` | A longer prompt is rejected (the previous one stays in use) |
| `OPENLIT_API_KEY` | none | The OpenLIT API key. A secret: untracked env file only (`BAHMNI_AI_MCP_ENV_FILE` in Bahmni), never in `.env` or git |

### append or replace?
- **`append`** (default) always includes the built-in rules (tool results are untrusted data, do not work around permission
  denials, ...) and puts the hub text after them under the heading "Additional instructions from the operator". Use it for
  tone, language, clinic-specific guidance. This is a convention to the model, not a hard guarantee: later text can still
  influence a model, so review hub edits like code.
- **`replace`** hands the whole prompt to whoever can edit the hub. The service logs `prompt_mode_replace` at startup. Use it
  only if you maintain the safety rules in your own prompt. Permissions, confirmations and the audit log are enforced by
  the service, not by the prompt, and are not affected either way.

## Placeholders

The model does not know today's date. Put `{{current_datetime}}` anywhere in the prompt (the built-in one, or one in the hub);
the service fills it in on **every request** from the server clock, in the clinic time zone (`TIMEZONE`). It is the only
placeholder, and it holds everything: weekday, date, time, time zone and UTC offset.

| Placeholder | Filled with |
|---|---|
| `{{current_datetime}}` | `Thursday, 2026-10-22 14:05 (Asia/Kolkata, UTC+05:30)` |

Example hub text: `It is {{current_datetime}}. Resolve "yesterday" or "next week" from this date.`

- Same `{{name}}` syntax as OpenLIT variables, but filled by this service, not by OpenLIT. The hub prompt is cached for
  `PROMPT_HUB_REFRESH_SECONDS`, so a date filled in there would go stale; keep the placeholder in the OpenLIT text as it is.
- Any other name (`{{user_name}}`, `{{current_date}}`) is left as written and logged once as `prompt_unknown_placeholder`, so a typo is visible.
- Only the prompt text is substituted, never user messages or tool results. Values come from the clock, not from users.
- **Automatic line:** if the final prompt uses no `{{current_datetime}}`, `Current date and time: <the same value>.` is appended at
  the end, so the model always knows the date, even for a `replace` prompt that forgot it. `AI_INJECT_DATETIME=false`
  turns that off (then only prompts with the placeholder get a date).
- The date is not stored in the conversation history, so an old conversation reopened tomorrow gets tomorrow's date.
- Keep the placeholder near the end of the prompt: the stable text before it stays cacheable by providers that cache prompts.
- `TIMEZONE` must be an IANA name (`Asia/Kolkata`, `Europe/Paris`); anything else stops startup. Default `UTC`.

## Failure behaviour
- **Misconfiguration stops startup** (fail closed): enabled but no name, unknown source, missing `OPENLIT_API_KEY`, a bad
  URL, or plain `http` to a remote host (the key would travel in clear text; `https`, `localhost`, `host.docker.internal`
  and single-label container names are allowed).
- **An outage never stops the service.** Never fetched: the built-in prompt is used (`prompt_fallback` is logged). A later
  refresh fails: the last good prompt stays in use (`prompt_refresh_failed`). An unpublished, missing, empty or oversized
  prompt counts as a failure.
- The prompt text is never logged. `prompt_loaded` shows source, name, version, length, a short hash and the mode; each
  `chat_usage` line carries `prompt_version`. Telemetry stays content-free (a test checks the hub text is not exported).

## Setting it up with OpenLIT (`deploy/openlit`)
1. In the OpenLIT UI create an API key (Settings → API keys) and put it in the untracked env file as `OPENLIT_API_KEY`.
2. Create the prompt in the Prompt Hub (name = `PROMPT_HUB_PROMPT_NAME`) and **publish** it. A draft is invisible to the API
   (the service logs `OpenLIT reported an error: No such prompt exists or isn't released yet!`).
3. Set `PROMPT_HUB_ENABLED=true`, `PROMPT_HUB_URL` and `PROMPT_HUB_PROMPT_NAME`. For the `deploy/openlit` stack the UI
   proxy forwards the prompt API (it only blocks self-registration), so from another compose project use
   `http://host.docker.internal:3300` (the same host as the telemetry endpoint). On the OpenLIT compose network itself the
   service is `http://openlit:3000`. Keep the key off public networks: use https if the hub is on another host.
4. Edit the prompt (publish a new version): the service picks it up within `PROMPT_HUB_REFRESH_SECONDS`.

The service sends `POST {PROMPT_HUB_URL}/api/prompt/get-compiled` with `Authorization: Bearer <key>` and the prompt name
(and version when pinned). OpenLIT answers HTTP 200 even for errors, with `{"err": ..., "res": {...}}`; the adapter checks
the body, and uses `compiledPrompt`. `{{variables}}` are left as written (no variables are sent).

Verified on 2026-10-16 against the pinned OpenLIT 2.1.0 image (a throwaway stack): fetch latest and pinned versions, pick up a
new published version, draft prompt rejected, unknown prompt, wrong key (HTTP 401), and the service starting with the hub on.

## Adding another source (any service that stores prompts)
Same pattern as [model providers](PROVIDERS.md). Implement two small things from `prompts/base.py`:

```python
from dataclasses import dataclass
import os
from bahmni_ai.prompts import FetchedPrompt, PromptRef, PromptSource, PromptSourceError, PromptSourceSettings


class MyHubSource:                                   # PromptSource
    def __init__(self, url: str, token: str, timeout: float): ...
    async def fetch(self, ref: PromptRef) -> FetchedPrompt:
        ...  # raise PromptSourceError on any failure; never put the token or the prompt text in the message
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class MyHubAdapter:                                  # PromptSourceAdapter
    name: str = "myhub"                              # the value of PROMPT_HUB_SOURCE

    def create(self, settings: PromptSourceSettings) -> PromptSource:
        token = os.environ.get("MYHUB_TOKEN", "")    # credentials come from the adapter's own variable
        if not token:
            raise PromptSourceError("MYHUB_TOKEN is not set")
        return MyHubSource(settings.url, token, settings.timeout_seconds)   # no network call here
```

Register it with no core change, either by adding it to `prompts/registry.py` in this repo, or from another package:

```toml
[project.entry-points."bahmni_ai.prompt_sources"]
myhub = "my_package.adapter:MyHubAdapter"
```

Then `PROMPT_HUB_SOURCE=myhub`. Retry, caching, last-good fallback, size limit, modes and logging are applied by the
resolver; do not implement them in the adapter. See `tests/test_prompt_hub.py` (`test_another_service_can_be_plugged_in...`)
for a worked example, and use `respx` to test your HTTP calls.
