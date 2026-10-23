# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""OpenTelemetry SDK wiring. The only module that builds providers; imported lazily, only when telemetry is enabled."""

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from opentelemetry import metrics, propagate, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.status import Status, StatusCode
from pydantic_ai.models.instrumented import InstrumentationSettings

from bahmni_ai.logging import get_logger
from bahmni_ai.observability.propagation import build_gated_propagator
from bahmni_ai.observability.scrub import ScrubbingExporter
from bahmni_ai.settings import Settings

log = get_logger("bahmni_ai.telemetry")


class TelemetryConfigError(RuntimeError):
    pass


class SafeSpanProcessor(SpanProcessor):
    """The SDK does not isolate processor callbacks: an exception in on_start/on_end reaches the request (Phase 0).
    Every processor we register is wrapped so that telemetry can never fail a chat turn."""

    def __init__(self, inner: SpanProcessor) -> None:
        self._inner = inner
        self.failures = 0

    def _failed(self, hook: str, exc: Exception) -> None:
        self.failures += 1
        if self.failures == 1 or self.failures % 1000 == 0:
            log.warning("span_processor_failed", hook=hook, error=exc.__class__.__name__, count=self.failures)

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        try:
            self._inner.on_start(span, parent_context)
        except Exception as exc:
            self._failed("on_start", exc)

    def on_end(self, span: Any) -> None:
        try:
            self._inner.on_end(span)
        except Exception as exc:
            self._failed("on_end", exc)

    def shutdown(self) -> None:
        try:
            self._inner.shutdown()
        except Exception as exc:
            self._failed("shutdown", exc)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            return self._inner.force_flush(timeout_millis)
        except Exception as exc:
            self._failed("force_flush", exc)
            return False


@dataclass
class Runtime:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    settings: Settings
    processors: list[SpanProcessor]

    def shutdown(self) -> None:
        """Flush and stop, bounded: a dead or hanging backend must not stall container stop."""
        limit = self.settings.telemetry_shutdown_timeout_seconds
        millis = int(limit * 1000)

        def work() -> None:
            for step in (
                lambda: self.tracer_provider.force_flush(millis),
                self.tracer_provider.shutdown,
                lambda: self.meter_provider.force_flush(millis),
                lambda: self.meter_provider.shutdown(millis),
            ):
                try:
                    step()
                except Exception as exc:
                    log.warning("telemetry_shutdown_step_failed", error=exc.__class__.__name__)

        thread = threading.Thread(target=work, name="telemetry-shutdown", daemon=True)
        thread.start()
        thread.join(limit)
        if thread.is_alive():
            log.warning("telemetry_shutdown_timed_out", seconds=limit)


def _exporters(settings: Settings) -> tuple[Any, Any]:
    timeout = settings.telemetry_export_timeout_seconds
    if settings.telemetry_backend == "console":
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter

        return ConsoleSpanExporter(), ConsoleMetricExporter()
    if not (os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")):
        raise TelemetryConfigError("TELEMETRY_BACKEND=otlp needs OTEL_EXPORTER_OTLP_ENDPOINT")
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    # Endpoint and headers come from the standard OTEL_EXPORTER_OTLP_* variables; only the timeout is ours.
    return OTLPSpanExporter(timeout=timeout), OTLPMetricExporter(timeout=timeout)


def build(settings: Settings, *, span_exporter: Any = None, metric_reader: Any = None) -> Runtime:
    """Create providers and install them globally (the MCP SDK and FastMCP read the global ones).

    span_exporter/metric_reader let tests substitute in-memory ones; they still go through the scrubber."""
    if span_exporter is None or metric_reader is None:
        real_spans, real_metrics = _exporters(settings)
        span_exporter = span_exporter or real_spans
        metric_reader = metric_reader or PeriodicExportingMetricReader(real_metrics, export_interval_millis=15000)

    attrs = {} if os.environ.get("OTEL_SERVICE_NAME") else {"service.name": "bahmni-ai"}
    resource = Resource.create(attrs)
    provider = TracerProvider(resource=resource)
    processor = SafeSpanProcessor(BatchSpanProcessor(
        ScrubbingExporter(
            span_exporter, keep_content=settings.telemetry_capture_content,
            discovery=settings.telemetry_discovery_spans, slow_ms=settings.telemetry_discovery_slow_ms,
        ),
        schedule_delay_millis=1000,
        export_timeout_millis=int(settings.telemetry_export_timeout_seconds * 1000),
    ))  # fmt: skip
    provider.add_span_processor(processor)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])

    trace.set_tracer_provider(provider)
    metrics.set_meter_provider(meter_provider)
    propagate.set_global_textmap(build_gated_propagator())
    return Runtime(provider, meter_provider, settings, [processor])


def record_mcp_unavailable(runtime: Runtime, server: str, error: str) -> None:
    """A server we could not reach or initialise for this run. The MCP libraries mark a failed discovery as a span with
    status UNSET, which the discovery filter would drop, so failures are reported by our own span (never filtered)."""
    tracer = runtime.tracer_provider.get_tracer("bahmni_ai")
    with tracer.start_as_current_span(
        "bahmni_ai.mcp.unavailable", attributes={"bahmni.server": server, "error.type": error}
    ) as span:
        span.set_status(Status(StatusCode.ERROR))
    runtime.meter_provider.get_meter("bahmni_ai").create_counter(
        "bahmni_ai.mcp.unavailable", description="MCP servers skipped for a run because they were unavailable"
    ).add(1, {"bahmni.server": server})


def tag_session(session: str) -> None:
    """Put the session on the active span (the HTTP request span): `session.id` is the generic key and
    `gen_ai.conversation.id` is the one OpenLIT reads, the same attribute Pydantic AI sets on its own spans."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("session.id", session)
        span.set_attribute("gen_ai.conversation.id", session)


def record_chat_error(runtime: Runtime, code: str) -> None:
    """A chat turn that ends in an `error` event is still an HTTP 200 stream, and Pydantic AI does not mark the `chat`
    span of a failed provider call, so label the request span: the failure then shows in the trace list."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("bahmni.error.code", code)
        span.set_status(Status(StatusCode.ERROR))
    runtime.meter_provider.get_meter("bahmni_ai").create_counter(
        "bahmni_ai.chat.errors", description="Chat turns that ended with an error event"
    ).add(1, {"bahmni.error.code": code})


ERROR_OUTCOMES = frozenset({"timeout", "error"})


def record_tool_outcome(runtime: Runtime, server: str, tool: str, decision: str, outcome: str) -> None:
    """The policy layer turns failures into results the model can read, so tracing never sees an exception. Label the
    active `execute_tool` span (ERROR only for real failures: a denial is a policy decision, not a fault) and count."""
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("bahmni.tool.decision", decision)
        span.set_attribute("bahmni.tool.outcome", outcome)
        if outcome in ERROR_OUTCOMES:
            span.set_status(Status(StatusCode.ERROR))
    runtime.meter_provider.get_meter("bahmni_ai").create_counter(
        "bahmni_ai.tool.calls", description="Tool calls by policy decision and outcome"
    ).add(1, {"bahmni.server": server, "bahmni.tool": tool, "bahmni.decision": decision, "bahmni.outcome": outcome})


def instrument_fastapi(app: Any, runtime: Runtime) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # No header capture is configured, so Cookie and X-OpenMRS-Session-Id are never recorded.
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=runtime.tracer_provider,
        meter_provider=runtime.meter_provider,
        excluded_urls="health",
        exclude_spans=["receive", "send"],  # per-ASGI-message spans are noise
    )


def agent_instrumentation(runtime: Runtime) -> InstrumentationSettings:
    return InstrumentationSettings(
        tracer_provider=runtime.tracer_provider, meter_provider=runtime.meter_provider,
        include_content=runtime.settings.telemetry_capture_content, include_binary_content=False,
        version=INSTRUMENTATION_VERSION,
    )  # fmt: skip


INSTRUMENTATION_VERSION: Literal[5] = 5  # pinned: span names and attributes follow this Pydantic AI format version


def current_trace_ids() -> dict[str, str]:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {}
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}


Shutdown = Callable[[], None]
