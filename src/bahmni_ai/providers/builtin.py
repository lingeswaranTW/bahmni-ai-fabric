# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Built-in adapters. Each delegates model construction to Pydantic AI using the provider's standard environment
variables, so there is no per-provider code in the core."""

from dataclasses import dataclass, field
from importlib.util import find_spec

from pydantic_ai.models import Model, infer_model

from bahmni_ai.providers.base import ProviderAdapter, ProviderCapabilities, ProviderError, ProviderSettings
from bahmni_ai.providers.openai_compat import OpenAICompatAdapter


@dataclass(frozen=True)
class PydanticAIAdapter:
    """Adapter for any provider Pydantic AI can build from a "<prefix>:<model>" string.

    Subclass-free on purpose: a new provider that Pydantic AI already supports is one more instance of this class.
    """

    name: str
    prefix: str  # Pydantic AI's provider prefix, which can differ from our name (gemini -> google)
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requires_model_name: bool = True
    requires_module: str | None = None  # SDK module that ships in an optional extra
    extra: str | None = None  # name of that extra, for the error message

    def build(self, model_name: str, settings: ProviderSettings) -> Model:
        if self.requires_module and find_spec(self.requires_module) is None:
            raise ProviderError(f"provider '{self.name}' needs its SDK: install bahmni-ai[{self.extra or self.name}]")
        if self.requires_model_name and not model_name:
            raise ProviderError(f"provider '{self.name}' needs a model name, e.g. '{self.name}:<model>'")
        spec = f"{self.prefix}:{model_name}" if model_name else self.prefix
        return infer_model(spec)


BUILTIN_ADAPTERS: tuple[ProviderAdapter, ...] = (
    PydanticAIAdapter("openai", "openai", ProviderCapabilities(vision=True)),
    PydanticAIAdapter("anthropic", "anthropic", ProviderCapabilities(vision=True)),
    # Gemini needs the optional extra: pip install "bahmni-ai[gemini]"
    PydanticAIAdapter(
        "gemini", "google", ProviderCapabilities(vision=True), requires_module="google.genai", extra="gemini"
    ),
    # Ollama speaks the OpenAI protocol; OLLAMA_BASE_URL selects the server. Tool support depends on the model.
    PydanticAIAdapter("ollama", "ollama", ProviderCapabilities(local=True)),
    # Any OpenAI Chat Completions compatible server (vLLM, LM Studio, LiteLLM, ...): OPENAI_COMPAT_BASE_URL
    OpenAICompatAdapter(),
    # Pydantic AI's built-in fake model: development and tests.
    PydanticAIAdapter("test", "test", ProviderCapabilities(local=True), requires_model_name=False),
)
