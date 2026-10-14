# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Events streamed to the frontend. Independent of the model provider and of any MCP server."""

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Event:
    name = "event"

    def data(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConversationStarted(Event):
    name = "conversation"
    conversationId: str  # noqa: N815


@dataclass
class TextDelta(Event):
    name = "text"
    delta: str


@dataclass
class ServersUnavailable(Event):
    name = "servers_unavailable"
    servers: list[str]


@dataclass
class ToolStarted(Event):
    name = "tool_started"
    server: str
    tool: str


@dataclass
class ToolFinished(Event):
    name = "tool_finished"
    server: str
    tool: str
    ok: bool


@dataclass
class PendingAction:
    id: str
    server: str
    tool: str
    arguments: dict[str, Any]


@dataclass
class ConfirmationRequired(Event):
    name = "confirmation_required"
    confirmationId: str  # noqa: N815
    expiresInSeconds: int  # noqa: N815
    actions: list[PendingAction] = field(default_factory=list)


@dataclass
class ErrorEvent(Event):
    name = "error"
    code: str  # session_expired | provider_error | assistant_error | step_limit | timeout | internal_error | ...
    message: str


@dataclass
class Done(Event):
    name = "done"
