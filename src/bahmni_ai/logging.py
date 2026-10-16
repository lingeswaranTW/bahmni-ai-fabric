# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Structured logging with secret redaction."""

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEY = re.compile(r"(authorization|cookie|token|secret|password|api[_-]?key|session[_-]?id)", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_JSESSION = re.compile(r"(?i)(jsessionid=)[^;\s]+")
REDACTED = "***"


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _JSESSION.sub(r"\1" + REDACTED, _BEARER.sub(r"\1 " + REDACTED, value))
    if isinstance(value, dict):
        return {k: (REDACTED if _SENSITIVE_KEY.search(str(k)) else redact_value(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    return value


def _redact_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    return {
        k: (REDACTED if _SENSITIVE_KEY.search(k) and k != "event" else redact_value(v))
        for k, v in event_dict.items()
    }


def _trace_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Adds trace_id/span_id when telemetry is on, so a log or audit line can be found in the trace backend."""
    from bahmni_ai.observability import current_trace_ids

    return {**event_dict, **current_trace_ids()}


def configure_logging(level: str = "info") -> None:
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            _trace_processor,
            _redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str = "bahmni_ai") -> Any:
    return structlog.get_logger(name)
