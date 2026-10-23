# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""The provider contract. Core code depends on this module only, never on a vendor SDK."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic_ai.models import Model


class ProviderError(RuntimeError):
    """A provider name or model could not be turned into a model. Messages never contain secrets."""


@dataclass(frozen=True)
class ProviderCapabilities:
    """What a provider's models can do. Informational: shown at startup and used by operators to pick a model."""

    tools: bool = True
    streaming: bool = True
    vision: bool = False
    structured_output: bool = True
    local: bool = False  # data never leaves the deployment (relevant for clinical data)


@dataclass(frozen=True)
class ProviderSettings:
    """Everything an adapter may need besides the model name. Credentials are NOT here: adapters read the provider's
    own standard environment variable (OPENAI_API_KEY, ...), so no secret passes through our settings."""

    request_timeout_seconds: float = 60.0
    max_retries: int = 2
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 20.0


@runtime_checkable
class ProviderAdapter(Protocol):
    """One adapter per provider. Adding a provider means adding one of these and registering it."""

    @property
    def name(self) -> str:
        """Lowercase; the part of AI_MODEL before the first ':'."""
        ...

    @property
    def capabilities(self) -> ProviderCapabilities: ...

    def build(self, model_name: str, settings: ProviderSettings) -> Model:
        """Return a ready model. Raise ProviderError (or let the SDK raise) when the model or its credentials are
        invalid. Must not make a network call."""
        ...
