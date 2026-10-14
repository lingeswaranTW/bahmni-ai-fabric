# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Serialisation and storage-time redaction of conversation history."""

import dataclasses
from collections.abc import Sequence
from typing import Literal

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelRequest, ToolReturnPart

from bahmni_ai.logging import redact_value

StoreResults = Literal["none", "truncated", "full"]
_OMITTED = "[tool result not stored]"
_MAX = 2000


def _redact_part(part: object, mode: StoreResults) -> object:
    if not isinstance(part, ToolReturnPart) or mode == "full":
        return part
    if mode == "none":
        return dataclasses.replace(part, content=_OMITTED, metadata=None)
    text = str(redact_value(str(part.content)))
    return dataclasses.replace(part, content=text[:_MAX], metadata=None)


def for_storage(messages: Sequence[ModelMessage], mode: StoreResults) -> bytes:
    """Keep the conversation usable as model context while dropping clinical tool output by default.

    Tool-call/return pairing is preserved so the history stays valid for every provider.
    """
    out: list[ModelMessage] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            msg = dataclasses.replace(msg, parts=[_redact_part(p, mode) for p in msg.parts])  # type: ignore[misc]
        out.append(msg)
    return ModelMessagesTypeAdapter.dump_json(out)


def from_storage(data: bytes | None) -> list[ModelMessage]:
    return ModelMessagesTypeAdapter.validate_json(data) if data else []


def to_json(messages: Sequence[ModelMessage]) -> bytes:
    """Full-fidelity (for the short-lived pending confirmation only)."""
    return ModelMessagesTypeAdapter.dump_json(list(messages))
