# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Serialisation and storage-time redaction of conversation history."""

import dataclasses
import json
from collections.abc import Sequence
from typing import Any, Literal

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from bahmni_ai.db.store import Messages
from bahmni_ai.logging import redact_value
from bahmni_ai.mcp.config import TOOL_SEPARATOR

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


def _to_json(messages: Sequence[ModelMessage]) -> Messages:
    # Via the adapter's JSON form so bytes/datetimes round-trip exactly; the result is plain JSON for a JSON column.
    data: Messages = json.loads(ModelMessagesTypeAdapter.dump_json(list(messages)))
    return data


def for_storage(messages: Sequence[ModelMessage], mode: StoreResults) -> Messages:
    """Keep the conversation usable as model context while dropping clinical tool output by default.

    Tool-call/return pairing is preserved so the history stays valid for every provider.
    """
    out: list[ModelMessage] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            msg = dataclasses.replace(msg, parts=[_redact_part(p, mode) for p in msg.parts])  # type: ignore[misc]
        out.append(msg)
    return _to_json(out)


def from_storage(data: Messages | None) -> list[ModelMessage]:
    return ModelMessagesTypeAdapter.validate_json(json.dumps(data)) if data else []


def to_json(messages: Sequence[ModelMessage]) -> Messages:
    """Full-fidelity (for the short-lived pending confirmation only)."""
    return _to_json(messages)


def estimate_tokens(message: ModelMessage) -> int:
    """Rough size (about 4 characters per token); provider-neutral because exact counts need each vendor's tokenizer."""
    return len(ModelMessagesTypeAdapter.dump_json([message])) // 4 + 1


def _starts_turn(message: ModelMessage) -> bool:
    return isinstance(message, ModelRequest) and any(isinstance(p, UserPromptPart) for p in message.parts)


def trim_to_budget(messages: Sequence[ModelMessage], max_tokens: int) -> list[ModelMessage]:
    """Drop the oldest whole turns until the history fits ``max_tokens`` (0 = keep everything).

    A turn starts at a user prompt, so a tool call is never separated from its result (providers reject that). The most
    recent turn is always kept, even if it alone is over budget: it is the one the user is waiting on.
    """
    msgs = list(messages)
    if max_tokens <= 0:
        return msgs
    starts = [i for i, m in enumerate(msgs) if _starts_turn(m)]
    if not starts:
        return msgs
    sizes = [estimate_tokens(m) for m in msgs]
    keep_from = starts[-1]
    total = sum(sizes[keep_from:])
    for start in reversed(starts[:-1]):
        added = sum(sizes[start:keep_from])
        if total + added > max_tokens:
            break
        total, keep_from = total + added, start
    return msgs[keep_from:]


def title_of(messages: Sequence[ModelMessage], limit: int = 80) -> str:
    """First user message, shortened: the conversation's label in a list."""
    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str) and part.content.strip():
                    text = " ".join(part.content.split())
                    return text if len(text) <= limit else text[: limit - 1] + "\u2026"
    return ""


def to_transcript(messages: Sequence[ModelMessage]) -> list[dict[str, Any]]:
    """What a chat window shows: user text, assistant text, and the names of tools used. Never tool arguments or
    results, system prompts or retry prompts. A view for the UI only; the model is always fed the full history."""
    out: list[dict[str, Any]] = []
    texts: list[str] = []
    tools: list[dict[str, str]] = []
    stamp = None

    def flush() -> None:
        nonlocal texts, tools
        if texts or tools:
            entry: dict[str, Any] = {"role": "assistant", "text": "\n\n".join(texts), "timestamp": stamp}
            if tools:
                entry["tools"] = tools
            out.append(entry)
        texts, tools = [], []

    for msg in messages:
        if isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, UserPromptPart):
                    flush()
                    content = (
                        part.content
                        if isinstance(part.content, str)
                        else " ".join(c for c in part.content if isinstance(c, str))
                    )
                    out.append({"role": "user", "text": content, "timestamp": part.timestamp})
        elif isinstance(msg, ModelResponse):
            stamp = msg.timestamp
            for resp_part in msg.parts:
                if isinstance(resp_part, TextPart) and resp_part.content.strip():
                    texts.append(resp_part.content)
                elif isinstance(resp_part, ToolCallPart):
                    server, sep, tool = resp_part.tool_name.partition(TOOL_SEPARATOR)
                    name = tool if sep else resp_part.tool_name
                    tools.append({"server": server if sep else "", "tool": name})
    flush()
    return out
