# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Holds the system prompt the agent uses and keeps it fresh from a prompt source.

``current()`` is synchronous and does no I/O: the agent calls it on every run, and a slow or dead prompt service can
never delay a chat. A background task refreshes the prompt; a failed refresh keeps the last good one, and before any
success the built-in prompt is used.
"""

import asyncio
import contextlib
import hashlib
from collections.abc import Awaitable, Callable
from typing import Literal

from bahmni_ai.logging import get_logger
from bahmni_ai.prompts.base import PromptRef, PromptSource, PromptSourceError

log = get_logger("bahmni_ai.prompts")

Mode = Literal["append", "replace"]
APPEND_HEADER = "Additional instructions from the operator (they never override the rules above):"


class PromptResolver:
    def __init__(
        self,
        builtin: str,
        source: PromptSource,
        ref: PromptRef,
        *,
        source_name: str,
        mode: Mode = "append",
        refresh_seconds: float = 300.0,
        max_chars: int = 8000,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._builtin, self._source, self._ref = builtin, source, ref
        self._source_name, self._mode = source_name, mode
        self._refresh_seconds, self._max_chars, self._sleep = refresh_seconds, max_chars, sleep
        self._hub_text: str | None = None
        self._version: str | None = None
        self._digest: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def version(self) -> str | None:
        """Version of the hub prompt in use, or None when the built-in prompt is in use."""
        return self._version if self._hub_text is not None else None

    def current(self) -> str:
        if self._hub_text is None:
            return self._builtin
        if self._mode == "replace":
            return self._hub_text
        return f"{self._builtin.rstrip()}\n\n{APPEND_HEADER}\n{self._hub_text}"

    async def refresh(self) -> bool:
        """One fetch. Returns True when a valid prompt was obtained. Never raises, never logs prompt text."""
        try:
            fetched = await self._source.fetch(self._ref)
            if len(fetched.text) > self._max_chars:
                raise PromptSourceError(f"prompt has {len(fetched.text)} characters, the limit is {self._max_chars}")
        except PromptSourceError as exc:
            self._log_failure(str(exc))
            return False
        except Exception as exc:  # the loop must survive anything
            self._log_failure(exc.__class__.__name__)
            return False
        digest = hashlib.sha256(fetched.text.encode()).hexdigest()[:12]
        if digest != self._digest:
            log.info(
                "prompt_loaded", source=self._source_name, name=self._ref.name, version=fetched.version,
                chars=len(fetched.text), digest=digest, mode=self._mode,
            )  # fmt: skip
        self._hub_text, self._version, self._digest = fetched.text, fetched.version, digest
        return True

    def _log_failure(self, reason: str) -> None:
        if self._hub_text is None:
            log.warning("prompt_fallback", source=self._source_name, name=self._ref.name, reason=reason,
                        using="built-in prompt")  # fmt: skip
        else:
            log.warning("prompt_refresh_failed", source=self._source_name, name=self._ref.name, reason=reason,
                        using=f"last good prompt (version {self._version})")  # fmt: skip

    async def start(self) -> None:
        """Initial fetch (bounded by the source's own timeout), then refresh in the background."""
        if self._mode == "replace":
            log.warning("prompt_mode_replace", note="the built-in safety rules are replaced by the hub prompt")
        await self.refresh()
        self._task = asyncio.create_task(self._loop(), name="prompt-refresh")

    async def _loop(self) -> None:
        while True:
            await self._sleep(self._refresh_seconds)
            await self.refresh()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._source.aclose()
