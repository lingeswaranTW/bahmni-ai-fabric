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
class Activity(Event):
    """Temporary progress line for the UI: what the assistant is doing right now.

    Carries codes and ids only, never reasoning text, prompts, tool arguments or tool results (they can hold patient
    data). The UI translates ``kind`` into text and removes every activity when the stream ends (done, error or
    confirmation_required). Not stored; not part of the transcript.

    kind: model (waiting for the model) | thinking (a reasoning part is streaming) | tool (running a tool) |
    retry (the provider failed transiently, trying again; ``attempt`` is the retry number).
    state: started | finished.
    """

    name = "activity"
    id: str
    kind: str
    state: str
    server: str | None = None
    tool: str | None = None
    attempt: int | None = None

    def data(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


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
