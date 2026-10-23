# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Uniform retry with backoff around any model. Providers differ in what their SDK retries; this layer gives every
provider the same policy and honours Retry-After on 429/503. Only the *start* of a request is retried: once a streamed
answer has begun, a failure is reported, never replayed (that would duplicate text and tool calls)."""

import asyncio
import random
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from bahmni_ai.logging import get_logger

log = get_logger("bahmni_ai.providers")

# Set per chat run by the service so a retry becomes visible to the user while the model call is still blocked.
# Called with ("waiting", attempt) before the backoff sleep and ("resumed", attempt) when the next attempt starts.
retry_listener: ContextVar[Callable[[str, int], None] | None] = ContextVar("retry_listener", default=None)

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


def is_retryable(exc: ModelAPIError) -> bool:
    """Rate limits, overload and server errors are transient; other 4xx (bad request, auth) are not.
    A ModelAPIError that is not an HTTP status (connection reset, timeout) is transient too."""
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in RETRYABLE_STATUS
    return True


def retry_after_seconds(exc: ModelAPIError) -> float | None:
    headers = exc.headers if isinstance(exc, ModelHTTPError) else None
    value = (headers or {}).get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError):
        return None


@dataclass(init=False)
class RetryingModel(WrapperModel):
    max_retries: int
    base_delay: float
    max_delay: float

    def __init__(
        self,
        wrapped: Model,
        *,
        max_retries: int = 2,
        base_delay: float = 1.0,
        max_delay: float = 20.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        super().__init__(wrapped)
        self.max_retries, self.base_delay, self.max_delay = max(0, max_retries), base_delay, max_delay
        self._sleep, self._jitter = sleep, jitter

    def _delay(self, exc: ModelAPIError, attempt: int) -> float:
        hinted = retry_after_seconds(exc)
        if hinted is not None:
            return min(hinted, self.max_delay)
        backoff = min(self.max_delay, self.base_delay * (2**attempt))
        return float(backoff * (0.5 + self._jitter() / 2))  # jitter in [0.5, 1.0] of the backoff

    async def _backoff_or_raise(self, exc: ModelAPIError, attempt: int) -> None:
        if attempt >= self.max_retries or not is_retryable(exc):
            raise exc
        delay = self._delay(exc, attempt)
        status = exc.status_code if isinstance(exc, ModelHTTPError) else None
        log.warning(
            "provider_retry", model=exc.model_name, status=status, attempt=attempt + 1, delay_seconds=round(delay, 2)
        )
        listener = retry_listener.get()
        if listener:
            listener("waiting", attempt + 1)
        await self._sleep(delay)
        if listener:
            listener("resumed", attempt + 1)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        attempt = 0
        while True:
            try:
                return await self.wrapped.request(messages, model_settings, model_request_parameters)
            except ModelAPIError as exc:
                await self._backoff_or_raise(exc, attempt)
                attempt += 1

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any = None,
    ) -> AsyncGenerator[StreamedResponse]:
        async with AsyncExitStack() as stack:
            attempt = 0
            while True:
                try:
                    stream = await stack.enter_async_context(
                        self.wrapped.request_stream(messages, model_settings, model_request_parameters, run_context)
                    )
                    break
                except ModelAPIError as exc:
                    await self._backoff_or_raise(exc, attempt)
                    attempt += 1
            yield stream
