# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Model selection. The provider and model come from configuration (``provider:model``)."""

from collections.abc import Callable

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.models import Model

from bahmni_ai.agent.deps import AgentDeps
from bahmni_ai.agent.prompts import SYSTEM_PROMPT
from bahmni_ai.observability import instrumentation_for_agent
from bahmni_ai.providers import ProviderError, ProviderSettings, resolve_model


class ModelConfigError(RuntimeError):
    pass


def build_agent(
    model: str | Model,
    request_timeout_seconds: float = 60.0,
    provider_settings: ProviderSettings | None = None,
    instructions: Callable[[], str] | None = None,
) -> Agent[AgentDeps, str | DeferredToolRequests]:
    """Build the single shared agent. Tools are attached per run, never here.

    ``model`` is ``AI_MODEL``: one or more comma-separated ``provider:model`` entries (fallback order), resolved by the
    provider registry (docs/PROVIDERS.md). Provider keys use each provider's standard environment variable.
    ``instructions`` is called on every run (the prompt resolver, docs/PROMPTS.md); default: the built-in prompt.
    """
    try:
        resolved = resolve_model(model, provider_settings) if isinstance(model, str) else model
        agent: Agent[AgentDeps, str | DeferredToolRequests] = Agent(
            resolved,
            deps_type=AgentDeps,
            output_type=[str, DeferredToolRequests],
            instructions=instructions or SYSTEM_PROMPT,
            model_settings={"timeout": request_timeout_seconds},
            retries=1,
        )
        agent.instrument = instrumentation_for_agent()  # content-free settings, or False when telemetry is off
        return agent
    except ProviderError as exc:  # our own messages never hold secrets
        raise ModelConfigError(f"Cannot initialise AI model '{model}': {exc}") from exc
    except Exception as exc:  # unknown provider, missing key, ... (never echo the exception: it may hold keys)
        raise ModelConfigError(f"Cannot initialise AI model '{model}': {exc.__class__.__name__}") from exc
