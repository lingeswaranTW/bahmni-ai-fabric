# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Adapter for any server that speaks the OpenAI *Chat Completions* protocol (vLLM, LM Studio, LiteLLM, Azure-style
gateways, ...). ``openai:`` uses OpenAI's newer Responses API, which most of these servers do not implement.

``AI_MODEL=openai-compat:<model>`` with ``OPENAI_COMPAT_BASE_URL`` (required) and ``OPENAI_COMPAT_API_KEY`` (optional).
"""

import os
from dataclasses import dataclass, field

from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from bahmni_ai.providers.base import ProviderCapabilities, ProviderError, ProviderSettings

BASE_URL_VAR = "OPENAI_COMPAT_BASE_URL"
API_KEY_VAR = "OPENAI_COMPAT_API_KEY"


@dataclass(frozen=True)
class OpenAICompatAdapter:
    name: str = "openai-compat"
    # Tool calling and vision depend on the server and model behind the URL; verify before enabling writes.
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    def build(self, model_name: str, settings: ProviderSettings) -> Model:
        if not model_name:
            raise ProviderError("provider 'openai-compat' needs a model name, e.g. 'openai-compat:<model>'")
        base_url = os.environ.get(BASE_URL_VAR, "").strip()
        if not base_url.startswith(("http://", "https://")):
            raise ProviderError(f"{BASE_URL_VAR} must be set to an http(s) URL")
        # Many local servers need no key, but the OpenAI client insists on a non-empty one.
        api_key = os.environ.get(API_KEY_VAR) or "not-needed"
        return OpenAIChatModel(model_name, provider=OpenAIProvider(base_url=base_url, api_key=api_key))
