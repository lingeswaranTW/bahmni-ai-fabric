# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Optional external source for the system prompt (docs/PROMPTS.md)."""

from bahmni_ai.agent.prompts import SYSTEM_PROMPT
from bahmni_ai.prompts.base import (
    FetchedPrompt,
    PromptRef,
    PromptSource,
    PromptSourceAdapter,
    PromptSourceError,
    PromptSourceSettings,
)
from bahmni_ai.prompts.registry import get_adapter, register, registered_names
from bahmni_ai.prompts.resolver import PromptResolver
from bahmni_ai.settings import Settings

__all__ = [
    "FetchedPrompt",
    "PromptRef",
    "PromptResolver",
    "PromptSource",
    "PromptSourceAdapter",
    "PromptSourceError",
    "PromptSourceSettings",
    "build_resolver",
    "get_adapter",
    "register",
    "registered_names",
]


def build_resolver(settings: Settings) -> PromptResolver | None:
    """None when the prompt hub is off. Raises PromptSourceError when it is on but misconfigured (startup stops)."""
    if not settings.prompt_hub_enabled:
        return None
    if not settings.prompt_hub_prompt_name.strip():
        raise PromptSourceError("PROMPT_HUB_PROMPT_NAME is required when PROMPT_HUB_ENABLED=true")
    adapter = get_adapter(settings.prompt_hub_source)
    source = adapter.create(PromptSourceSettings(settings.prompt_hub_url.strip(), settings.prompt_hub_timeout_seconds))
    return PromptResolver(
        SYSTEM_PROMPT,
        source,
        PromptRef(settings.prompt_hub_prompt_name.strip(), settings.prompt_hub_prompt_version.strip() or None),
        source_name=adapter.name,
        mode=settings.prompt_hub_mode,
        refresh_seconds=settings.prompt_hub_refresh_seconds,
        max_chars=settings.prompt_hub_max_chars,
    )
