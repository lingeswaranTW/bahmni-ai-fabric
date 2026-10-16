# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Per-server gate for trace-context propagation to MCP servers.

The MCP SDK and the FastMCP client inject `traceparent`/`tracestate` into every request through the process-wide
propagator, and neither offers a per-server switch (docs/OBSERVABILITY_PHASE0_RESULT.md, section 4). So we install a
propagator that injects only while `propagation_scope(True)` is active, and the tool policy opens that scope only for
servers configured with `propagateTrace: true`. The default is off. This module has no OpenTelemetry import at module
level, so the scope helper costs nothing when telemetry is disabled.
"""

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_ALLOWED: contextvars.ContextVar[bool] = contextvars.ContextVar("bahmni_ai_propagate_trace", default=False)


@contextmanager
def propagation_scope(allowed: bool) -> Iterator[None]:
    token = _ALLOWED.set(allowed)
    try:
        yield
    finally:
        _ALLOWED.reset(token)


def propagation_allowed() -> bool:
    return _ALLOWED.get()


def build_gated_propagator() -> Any:
    """W3C trace-context propagator that injects only inside an allowed scope. Extraction is unchanged."""
    from opentelemetry.propagators.textmap import DefaultSetter, TextMapPropagator
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    class GatedPropagator(TextMapPropagator):
        def __init__(self) -> None:
            self._inner = TraceContextTextMapPropagator()

        def extract(self, carrier: Any, context: Any = None, getter: Any = None) -> Any:
            if getter is None:
                return self._inner.extract(carrier, context)
            return self._inner.extract(carrier, context, getter)

        def inject(self, carrier: Any, context: Any = None, setter: Any = DefaultSetter()) -> None:  # noqa: B008
            if _ALLOWED.get():
                self._inner.inject(carrier, context, setter)

        @property
        def fields(self) -> Any:
            return self._inner.fields

    return GatedPropagator()
