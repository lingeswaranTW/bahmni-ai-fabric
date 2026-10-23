# Model providers

bahmni-ai talks to language models only through the **provider layer** in `src/bahmni_ai/providers/`. The chat service,
the policy code and the API never import a vendor SDK and never look at a provider name. Choosing, combining and adding
providers is configuration plus (for a new vendor) one small adapter.

```
AI_MODEL="anthropic:claude-sonnet-5-5, openai:gpt-4o"
        │  parse (providers/registry.py: resolve_model)
        ▼
  adapter.build(model_name, settings)  ──►  pydantic_ai Model        one per entry
        │  wrap (providers/retry.py)
        ▼
  RetryingModel(model)                      backoff + Retry-After for 408/409/425/429/5xx/connection errors
        │  more than one entry?
        ▼
  FallbackModel(first, second, ...)         next provider when the previous one has failed after its retries
        ▼
  Agent (agent/factory.py)                  tools, policy, history: identical for every provider
```

## Choosing a provider (operators)

`AI_MODEL` is one or more comma-separated `provider:model` entries, tried in order.

| Provider | Example | Credentials / config | Extra install | Notes |
|---|---|---|---|---|
| `anthropic` | `anthropic:claude-sonnet-5-5` | `ANTHROPIC_API_KEY` | built in | tools, streaming, vision |
| `openai` | `openai:gpt-4o` | `OPENAI_API_KEY` | built in | tools, streaming, vision |
| `ollama` | `ollama:llama3.2` | `OLLAMA_BASE_URL` (e.g. `http://ollama:11434/v1`) | built in | local: data stays on your network. Tool calling depends on the model; context is small, so lower `AI_HISTORY_MAX_TOKENS` |
| `openai-compat` | `openai-compat:my-model` | `OPENAI_COMPAT_BASE_URL` (required), `OPENAI_COMPAT_API_KEY` (optional) | built in | any server speaking OpenAI *Chat Completions* (vLLM, LM Studio, LiteLLM, ...). `openai:` uses OpenAI's newer Responses API, which most of these servers lack. Tool calling and context size depend on the server and model |
| `gemini` | `gemini:gemini-2.5-flash` | `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) | `pip install "bahmni-ai[gemini]"` (`uv sync --extra gemini`) | |
| `test` | `test` | none | built in | Pydantic AI's fake model, for development and tests |

Credentials are the provider's own standard environment variables. They are never in `AI_MODEL`, never in
`mcp-servers.json` (the credential resolver refuses names without the `MCP_` prefix), and never in error messages.

Related settings (all environment variables, defaults in `settings.py`):

| Variable | Default | Effect |
|---|---|---|
| `AI_REQUEST_TIMEOUT_SECONDS` | `60` | Timeout of one model request |
| `AI_MAX_RETRIES` | `2` | Extra attempts for a transient failure *before the answer starts streaming*. `0` disables |
| `AI_RETRY_BASE_DELAY_SECONDS`, `AI_RETRY_MAX_DELAY_SECONDS` | `1`, `20` | Exponential backoff with jitter, capped. A `Retry-After` header from the provider wins (also capped) |
| `AI_HISTORY_MAX_TOKENS` | `32000` | Approximate budget for stored history sent to the model. Oldest whole turns are dropped first; the database keeps everything. `0` disables |
| `AI_MAX_TOTAL_TOKENS` | `0` | Hard cap on input + output tokens of one request across all its rounds. `0` = no cap |
| `AI_MAX_TOOL_STEPS` | `10` | Model/tool rounds per request |

What the client sees when a provider fails: `rate_limited` (every attempt got 429), otherwise `provider_error`. The
provider's own error text is logged by status code only and never sent to the browser. Each request logs a `chat_usage`
line (requests, input/output tokens, tool calls).

Notes on the vendor SDKs: the OpenAI and Anthropic SDKs also retry a couple of times on their own. Our layer adds one
uniform policy on top, so worst case is (SDK retries + 1) × (`AI_MAX_RETRIES` + 1) attempts, each bounded by the delay cap.
Set `AI_MAX_RETRIES=0` if you want only the SDK behaviour.

## The contract

`providers/base.py`. This is everything an adapter must provide:

```python
class ProviderAdapter(Protocol):
    name: str                          # lowercase; the text before the first ':' in AI_MODEL
    capabilities: ProviderCapabilities # tools, streaming, vision, structured_output, local (informational)

    def build(self, model_name: str, settings: ProviderSettings) -> pydantic_ai.models.Model: ...
```

Rules for `build`:
- Return a ready model. Do **not** make a network call.
- Read credentials from the provider's own environment variable (or its SDK default). Do not add secrets to settings.
- Raise `ProviderError` for a bad model name or missing configuration. Messages must not contain secrets; the factory
  turns it into `ModelConfigError` and startup stops (fail closed).
- Everything else (retry, fallback, timeouts, history trimming, error mapping, telemetry) is applied *around* your model by
  the core. Do not implement them in the adapter.

`ProviderSettings` carries the request timeout and the retry numbers. An adapter that creates its own HTTP client should
use `request_timeout_seconds`.

## How to add a provider

### A. The provider is already supported by Pydantic AI (most vendors)

One line. Add an instance to `BUILTIN_ADAPTERS` in `providers/builtin.py`:

```python
PydanticAIAdapter("groq", "groq", ProviderCapabilities()),
```

Then:
1. Add the SDK to `pyproject.toml`, as an optional extra so default installs stay small:
   `groq = ["pydantic-ai-slim[groq]>=2.55,<3"]`, then `uv lock`.
2. Add a row to the table above and a line to `CHANGELOG.md`.
3. Add the provider to the contract test (see Testing) if it has special behaviour.

`name` is what operators type in `AI_MODEL`; `prefix` is Pydantic AI's provider prefix (they can differ, as in
`gemini` → `google`). If the provider takes no model name (like `test`), set `requires_model_name=False`.

### B. Any other model API: write an adapter

Create `src/bahmni_ai/providers/myvendor.py`:

```python
from dataclasses import dataclass, field

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from bahmni_ai.providers import ProviderCapabilities, ProviderError, ProviderSettings


@dataclass(frozen=True)
class MyVendorAdapter:
    name: str = "myvendor"
    capabilities: ProviderCapabilities = field(default_factory=lambda: ProviderCapabilities(tools=True))

    def build(self, model_name: str, settings: ProviderSettings) -> Model:
        import os

        key = os.environ.get("MYVENDOR_API_KEY")
        if not key:
            raise ProviderError("MYVENDOR_API_KEY is not set")
        provider = OpenAIProvider(base_url="https://api.myvendor.example/v1", api_key=key)  # OpenAI-compatible API
        return OpenAIChatModel(model_name, provider=provider)
```

Register it, with **no change to core code**, in one of two ways:

1. **In this repo:** append `MyVendorAdapter()` to `BUILTIN_ADAPTERS` in `providers/builtin.py`.
2. **From another package (plugin):** publish a package with an entry point. The core discovers it at startup.

   ```toml
   # the plugin's pyproject.toml
   [project.entry-points."bahmni_ai.providers"]
   myvendor = "my_package.adapter:MyVendorAdapter"   # an adapter instance, or a class/factory taking no arguments
   ```
   Install the plugin into the same environment (or image) as bahmni-ai. A plugin that fails to import is logged
   (`provider_plugin_failed`) and skipped; it cannot stop the built-in providers. A plugin whose name matches a built-in
   replaces it, which is how you can customise a vendor without forking.

Then set `AI_MODEL=myvendor:some-model`.

### Checklist before shipping an adapter
- [ ] Missing or invalid credentials raise `ProviderError` / a clear SDK error, and nothing secret appears in the message.
- [ ] `build` makes no network call.
- [ ] Tool calling works with at least one tool (the policy layer depends on it). If the model cannot call tools, set
      `capabilities.tools=False` and say so in the docs; do not enable write tools with it.
- [ ] Streaming works (`/chat` is always streamed).
- [ ] `python -m bahmni_ai.providers.smoke <provider:model>` passes all checks.
- [ ] Unit test like `tests/test_providers.py::test_new_provider_needs_only_an_adapter`.
- [ ] Docs row and changelog entry.

## Testing

### Live smoke test (real provider)
Unit tests use mocked providers. To check a real provider and model, run the smoke test. It uses the same
`build_agent` path as the service and makes a few small, billable model calls with synthetic prompts:

```bash
uv run python -m bahmni_ai.providers.smoke ollama:qwen3:4b          # local, free
OPENAI_API_KEY=... uv run python -m bahmni_ai.providers.smoke openai:gpt-4o
uv run python -m bahmni_ai.providers.smoke "anthropic:claude-sonnet-5-5,openai:gpt-4o"   # first entry is exercised
```

| Check | Passes when |
|---|---|
| plain answer | the model returns non-empty text |
| streaming | at least one streamed text event arrives |
| tool call | the model calls an `add` tool and the final answer contains `42` |
| forced failure | with the provider's credential/endpoint deliberately broken, the failure maps to `provider_error` (or `rate_limited`) |

The exit code is non-zero if any check fails; use `--skip-errors` to skip the forced-failure check. A model that fails
the tool-call check should not be used with write tools.

The same checks run as pytest, skipped unless you opt in, and as a manual GitHub workflow (`Live provider smoke test`,
needs the repository secrets `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `OPENAI_COMPAT_BASE_URL`,
`OPENAI_COMPAT_API_KEY` as applicable):

```bash
LIVE_PROVIDER_MODELS="ollama:qwen3:4b" OLLAMA_BASE_URL=http://localhost:11434/v1 uv run pytest -m live
```

Verified so far (2026-10-16, local): `ollama:qwen3:4b` and `openai-compat:qwen3:4b` (Ollama's OpenAI-compatible endpoint)
pass all four checks. OpenAI, Anthropic and Gemini have not been exercised live yet: run the smoke test with your key.

### Unit tests

`tests/test_providers.py` shows the pattern, with no network and no real keys:
- A throw-away adapter registered in the test proves a new provider needs no core change.
- `FunctionModel` plays the provider. `RetryingModel` is exercised with an injected `sleep`, so 429 / `Retry-After` /
  connection-error cases run instantly.
- `tests/test_chat_api.py` checks the end-to-end error mapping and that storage keeps the full history while the model
  only sees the trimmed window.

For a real provider, run a manual smoke test with a test conversation: one plain answer, one tool call, one confirmation
(write) flow, and a forced failure (wrong key) to see `provider_error`.

## Design notes and limits
- Pydantic AI is the model-API abstraction underneath. The adapter layer decouples *selection, combination and policy* from
  it (names, fallback, retry, limits, plugins). Replacing Pydantic AI itself would mean returning a different object from
  `build`, and the agent code in `agent/` would also change; that is not a goal.
- Fallback happens only when a request fails to start or fails as a whole. A stream that breaks half way is reported as an
  error and not replayed (it would duplicate text or tool calls).
- Token counts for trimming are an estimate (about 4 characters per token), deliberately vendor-neutral.
  `AI_MAX_TOTAL_TOKENS` uses the provider's own reported usage.
- Capabilities are informational today (shown in docs and available to code through `get_adapter(name).capabilities`).
  Nothing enforces them yet.
