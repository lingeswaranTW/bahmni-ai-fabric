# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""The prompt-source contract. A source is any service that stores prompts (an observability platform's prompt hub, a
config service, ...). Core code depends on this module only."""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


class PromptSourceError(RuntimeError):
    """A prompt could not be fetched or configured. Messages never contain credentials or prompt text."""


@dataclass(frozen=True)
class PromptRef:
    name: str
    version: str | None = None  # None = the source's latest
    variables: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchedPrompt:
    text: str
    version: str | None = None


@dataclass(frozen=True)
class PromptSourceSettings:
    """Everything an adapter may need besides the prompt reference. Credentials are NOT here: an adapter reads its
    own environment variable (OPENLIT_API_KEY, ...)."""

    url: str
    timeout_seconds: float = 3.0


@runtime_checkable
class PromptSource(Protocol):
    async def fetch(self, ref: PromptRef) -> FetchedPrompt:
        """Return the prompt text. Raise PromptSourceError on any failure (the caller keeps the last good prompt)."""
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class PromptSourceAdapter(Protocol):
    """One adapter per service. Adding a source means adding one of these and registering it."""

    @property
    def name(self) -> str:
        """Lowercase; the value of PROMPT_HUB_SOURCE."""
        ...

    def create(self, settings: PromptSourceSettings) -> PromptSource:
        """Validate configuration (URL, credentials) and return a source. Raise PromptSourceError when invalid.
        Must not make a network call."""
        ...
