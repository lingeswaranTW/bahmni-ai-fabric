# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Observability facade. The only place the rest of the application touches telemetry.

Everything here is write-only and never raises: telemetry cannot change an authorization, confirmation or audit outcome,
and the service runs identically with it disabled. OpenTelemetry (and any backend such as OpenLIT) is imported only in
this package; see docs/OBSERVABILITY.md.
"""

import hashlib
import hmac
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from bahmni_ai.logging import get_logger
from bahmni_ai.observability.propagation import propagation_scope
from bahmni_ai.settings import Settings

__all__ = [
    "current_trace_ids",
    "instrument_app",
    "instrumentation_for_agent",
    "propagation_scope",
    "record_chat_error",
    "record_mcp_unavailable",
    "record_tool_outcome",
    "session_id",
    "setup",
    "tag_session",
]

if TYPE_CHECKING:
    from pydantic_ai.models.instrumented import InstrumentationSettings

log = get_logger("bahmni_ai.telemetry")
_runtime: Any = None


def _noop() -> None:
    return None


def setup(settings: Settings, *, span_exporter: Any = None, metric_reader: Any = None) -> Callable[[], None]:
    """Configure telemetry once. Returns a bounded, never-raising shutdown callable. A misconfiguration disables
    telemetry with a warning; it never stops the service (unlike MCP configuration, which fails closed)."""
    global _runtime
    _runtime = None
    if not settings.telemetry_enabled:
        return _noop
    try:
        from bahmni_ai.observability import _otel

        runtime = _otel.build(settings, span_exporter=span_exporter, metric_reader=metric_reader)
    except Exception as exc:
        log.warning("telemetry_disabled", reason=exc.__class__.__name__, detail=str(exc)[:200])
        return _noop
    _runtime = runtime
    if settings.telemetry_session_id == "hmac" and not settings.telemetry_session_key:
        log.warning("telemetry_session_id_disabled", reason="TELEMETRY_SESSION_ID=hmac needs TELEMETRY_SESSION_KEY")
    if settings.telemetry_session_id == "raw":
        log.warning("telemetry_session_id_raw", note="conversation ids are sent to the telemetry backend")
    if settings.telemetry_capture_content:
        log.warning("telemetry_content_capture_enabled", note="prompts and tool data may be exported")
    log.info("telemetry_enabled", backend=settings.telemetry_backend, discovery=settings.telemetry_discovery_spans)
    return runtime.shutdown


def instrumentation_for_agent() -> "InstrumentationSettings | bool":
    """Value for Agent(instrument=...): content-free settings when enabled, False otherwise."""
    if _runtime is None:
        return False
    try:
        from bahmni_ai.observability import _otel

        settings: InstrumentationSettings = _otel.agent_instrumentation(_runtime)
        return settings
    except Exception as exc:
        log.warning("telemetry_agent_instrumentation_failed", error=exc.__class__.__name__)
        return False


def instrument_app(app: Any) -> None:
    if _runtime is None:
        return
    try:
        from bahmni_ai.observability import _otel

        _otel.instrument_fastapi(app, _runtime)
    except Exception as exc:
        log.warning("telemetry_fastapi_instrumentation_failed", error=exc.__class__.__name__)


def current_trace_ids() -> dict[str, str]:
    """trace_id/span_id of the active span, for log correlation only."""
    if _runtime is None:
        return {}
    try:
        from bahmni_ai.observability import _otel

        return _otel.current_trace_ids()
    except Exception:
        return {}


def record_mcp_unavailable(server: str, error: str) -> None:
    """Report a skipped MCP server (span with ERROR status plus a counter). Never raises."""
    if _runtime is None:
        return
    try:
        from bahmni_ai.observability import _otel

        _otel.record_mcp_unavailable(_runtime, server, error)
    except Exception as exc:
        log.warning("telemetry_record_failed", error=exc.__class__.__name__)


def session_id(conversation_id: str, stored: str | None = None) -> str | None:
    """Session id for one chat turn, or None when telemetry is off. Every turn of a conversation gets the same value.

    ``raw`` sends our conversation id, ``hmac`` a keyed hash of it. Otherwise (the anonymous default) it is the id
    Pydantic AI stored in the conversation history (``stored``), or a new random one on the first turn: it links the
    turns to each other and to nothing else."""
    if _runtime is None:
        return None
    try:
        settings = _runtime.settings
        if settings.telemetry_session_id == "raw":
            return conversation_id
        if settings.telemetry_session_id == "hmac" and settings.telemetry_session_key:
            key = settings.telemetry_session_key.encode()
            return hmac.new(key, conversation_id.encode(), hashlib.sha256).hexdigest()[:32]
    except Exception as exc:
        log.warning("telemetry_session_id_failed", error=exc.__class__.__name__)
    return stored or uuid.uuid4().hex


def tag_session(session: str | None) -> None:
    """Label the active (request) span with the session so a turn's root row can be found by session. Never raises."""
    if _runtime is None or not session:
        return
    try:
        from bahmni_ai.observability import _otel

        _otel.tag_session(session)
    except Exception as exc:
        log.warning("telemetry_tag_session_failed", error=exc.__class__.__name__)


def record_tool_outcome(server: str, tool: str, decision: str, outcome: str) -> None:
    """Label the active tool span with the policy decision and outcome; ERROR for failures. Never raises."""
    if _runtime is None:
        return
    try:
        from bahmni_ai.observability import _otel

        _otel.record_tool_outcome(_runtime, server, tool, decision, outcome)
    except Exception as exc:
        log.warning("telemetry_record_failed", error=exc.__class__.__name__)


def record_chat_error(code: str) -> None:
    """Mark the request span and count a chat turn that ended in an error event. Never raises."""
    if _runtime is None:
        return
    try:
        from bahmni_ai.observability import _otel

        _otel.record_chat_error(_runtime, code)
    except Exception as exc:
        log.warning("telemetry_record_failed", error=exc.__class__.__name__)
