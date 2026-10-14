# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Structured audit trail for tool decisions and outcomes.

Records who did what, never the secrets or the clinical content: argument *names* and a hash are
logged, not values.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from bahmni_ai.logging import get_logger

Decision = Literal["allowed", "denied", "invalid_arguments", "confirmation_required", "session_expired"]
Outcome = Literal["success", "error", "timeout", "not_executed"]


@dataclass
class ToolAudit:
    request_id: str
    username: str
    user_uuid: str
    server: str
    tool: str
    access: str
    decision: Decision
    reason: str = ""
    outcome: Outcome = "not_executed"
    latency_ms: int = 0
    argument_names: tuple[str, ...] = ()
    arguments_hash: str = ""


class AuditSink(Protocol):
    def record(self, entry: ToolAudit) -> None: ...


def hash_arguments(args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]


class LogAuditSink:
    def __init__(self) -> None:
        self._log = get_logger("bahmni_ai.audit")

    def record(self, entry: ToolAudit) -> None:
        self._log.info(
            "tool_audit",
            request_id=entry.request_id, username=entry.username, user_uuid=entry.user_uuid,
            server=entry.server, tool=entry.tool, access=entry.access, decision=entry.decision,
            reason=entry.reason, outcome=entry.outcome, latency_ms=entry.latency_ms,
            argument_names=list(entry.argument_names), arguments_hash=entry.arguments_hash,
        )  # fmt: skip
