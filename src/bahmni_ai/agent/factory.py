# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Model selection. The provider and model come from configuration (``provider:model``)."""

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.models import Model

from bahmni_ai.agent.deps import AgentDeps
from bahmni_ai.agent.prompts import SYSTEM_PROMPT
from bahmni_ai.observability import instrumentation_for_agent


class ModelConfigError(RuntimeError):
    pass


def build_agent(
    model: str | Model, request_timeout_seconds: float = 60.0
) -> Agent[AgentDeps, str | DeferredToolRequests]:
    """Build the single shared agent. Tools are attached per run, never here.

    Provider keys use each provider's standard environment variable (OPENAI_API_KEY, ANTHROPIC_API_KEY,
    OLLAMA_BASE_URL, ...). Provider differences (tool calling, streaming, context size) are the
    implementer's responsibility; see docs/OPERATIONS.md.
    """
    try:
        agent: Agent[AgentDeps, str | DeferredToolRequests] = Agent(
            model,
            deps_type=AgentDeps,
            output_type=[str, DeferredToolRequests],
            instructions=SYSTEM_PROMPT,
            model_settings={"timeout": request_timeout_seconds},
            retries=1,
        )
        agent.instrument = instrumentation_for_agent()  # content-free settings, or False when telemetry is off
        return agent
    except Exception as exc:  # unknown provider, missing key, ... (never echo the exception: it may hold keys)
        raise ModelConfigError(f"Cannot initialise AI model '{model}': {exc.__class__.__name__}") from exc
