# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Exporter-side scrubbing and filtering of spans (docs/OBSERVABILITY.md sections 5 and 5.2).

Runs on the batch-export thread, never in the request path. It is an allowlist: an attribute that is not named here is
not exported. It also removes the status description and exception text, because MCP servers and providers put their own
error text there (Phase 0 found a bearer token reach the backend that way with content capture off). If scrubbing a span
fails for any reason, the span is dropped, never exported raw.
"""

from collections.abc import Sequence
from typing import Any

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import StatusCode
from opentelemetry.trace.status import Status

from bahmni_ai.logging import get_logger, redact_value

log = get_logger("bahmni_ai.telemetry")

ALLOWED_PREFIXES = (
    "gen_ai.", "bahmni.", "rpc.", "http.request.method", "http.route", "http.response.status_code", "http.method",
    "http.status_code", "mcp.method.name", "error.type", "session.id",
)  # fmt: skip
# Only exported when TELEMETRY_CAPTURE_CONTENT is on.
CONTENT_KEYS = frozenset({
    "gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions", "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result", "final_result", "tool_arguments", "tool_response", "pydantic_ai.all_messages",
})  # fmt: skip
DISCOVERY_METHODS = frozenset({"server/discover", "tools/list", "initialize", "notifications/initialized"})


def _method(span: Any) -> str | None:
    method = (span.attributes or {}).get("mcp.method.name")
    if isinstance(method, str):
        return method
    name = span.name[len("MCP send ") :] if span.name.startswith("MCP send ") else span.name
    return name if name in DISCOVERY_METHODS else None


class ScrubbingExporter(SpanExporter):
    """Wraps a SpanExporter: filters noise, scrubs, then hands the spans on."""

    def __init__(
        self, inner: Any, *, keep_content: bool = False, discovery: str = "errors-only", slow_ms: int = 1000
    ) -> None:
        self._inner = inner
        self._keep_content = keep_content
        self._drop_ok_discovery = discovery == "errors-only"
        self._slow_ns = slow_ms * 1_000_000

    # -- filtering --------------------------------------------------------------------------------
    def _is_noise(self, span: Any) -> bool:
        """Successful, fast MCP discovery spans. Errors and slow ones are kept so failures stay visible."""
        if not self._drop_ok_discovery or _method(span) not in DISCOVERY_METHODS:
            return False
        if span.status.status_code == StatusCode.ERROR:
            return False
        duration = (span.end_time or 0) - (span.start_time or 0)
        return duration < self._slow_ns

    # -- scrubbing --------------------------------------------------------------------------------
    def _attributes(self, attrs: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in (attrs or {}).items():
            if key in CONTENT_KEYS:
                if not self._keep_content:
                    continue
            elif not key.startswith(ALLOWED_PREFIXES):
                continue
            out[key] = redact_value(value) if isinstance(value, str) else value
        return out

    def _events(self, events: Sequence[Any]) -> list[Any]:
        kept = []
        for event in events:
            if event.name == "exception":  # type only: the message and stacktrace carry foreign error text
                etype = (event.attributes or {}).get("exception.type")
                kept.append(Event("exception", {"exception.type": etype} if etype else {}, event.timestamp))
            elif self._keep_content and event.name.startswith("gen_ai."):
                kept.append(Event(event.name, self._attributes(event.attributes), event.timestamp))
        return kept

    def _clean(self, span: Any) -> Any:
        return ReadableSpan(
            name=span.name, context=span.context, parent=span.parent, resource=span.resource,
            attributes=self._attributes(span.attributes), events=self._events(span.events), links=span.links,
            kind=span.kind, instrumentation_scope=span.instrumentation_scope,
            status=Status(span.status.status_code),  # no description
            start_time=span.start_time, end_time=span.end_time,
        )  # fmt: skip

    # -- SpanExporter interface -------------------------------------------------------------------
    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        clean = []
        for span in spans:
            try:
                if not self._is_noise(span):
                    clean.append(self._clean(span))
            except Exception as exc:  # fail closed: a span we cannot scrub is not exported
                log.warning("span_scrub_failed", error=exc.__class__.__name__)
        result: SpanExportResult = self._inner.export(clean) if clean else SpanExportResult.SUCCESS
        return result

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        flush = getattr(self._inner, "force_flush", None)
        return bool(flush(timeout_millis)) if flush else True
