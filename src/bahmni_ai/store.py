# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Conversation history and pending confirmations.

History is never an authorization source: it is only model context, and it is always read through the
owning user. Both stores have an in-memory implementation (tests/dev) and a Postgres one (``db/``).
"""

import secrets
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

Messages = list[dict[str, Any]]  # pydantic-ai message history as plain JSON (stored as a JSON column)


@dataclass
class PendingConfirmation:
    id: str
    user_uuid: str
    conversation_id: str
    messages_json: Messages  # full-fidelity run state, kept only until confirmed, rejected, or expired
    actions_json: str
    expires_at: float


@dataclass(frozen=True)
class ConversationSummary:
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ConversationRecord:
    summary: ConversationSummary
    messages_json: Messages


class ConversationStore(Protocol):
    """Every method is scoped to the owner. Soft-deleted conversations are invisible to all of them."""

    async def load(self, user_uuid: str, conversation_id: str) -> Messages | None: ...
    async def save(
        self, user_uuid: str, conversation_id: str, messages_json: Messages, title: str | None = None
    ) -> None:
        """Create or update. ``title`` is only used when the conversation is new (or was soft-deleted: reusing a
        deleted id starts a fresh conversation, the old content is gone)."""
        ...

    async def list(self, user_uuid: str, limit: int, offset: int) -> list[ConversationSummary]:
        """Newest activity first."""
        ...

    async def get(self, user_uuid: str, conversation_id: str) -> ConversationRecord | None: ...
    async def delete(self, user_uuid: str, conversation_id: str, hard: bool) -> bool:
        """Soft (hidden, row kept) or hard (row removed, also if soft-deleted). False if unknown or not the user's."""
        ...


class ConfirmationStore(Protocol):
    async def put(self, pending: PendingConfirmation) -> None: ...
    async def take(self, user_uuid: str, confirmation_id: str) -> PendingConfirmation | None:
        """Atomically remove and return the confirmation if it belongs to the user (single use)."""
        ...


def new_id() -> str:
    return secrets.token_urlsafe(24)


@dataclass
class _Entry:
    messages: Messages
    title: str
    created_at: datetime
    updated_at: datetime
    deleted: bool = False


class MemoryConversationStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], _Entry] = {}

    def _live(self, user_uuid: str, conversation_id: str) -> _Entry | None:
        e = self._data.get((user_uuid, conversation_id))
        return None if e is None or e.deleted else e

    @staticmethod
    def _summary(cid: str, e: _Entry) -> ConversationSummary:
        return ConversationSummary(cid, e.title, e.created_at, e.updated_at)

    async def load(self, user_uuid: str, conversation_id: str) -> Messages | None:
        e = self._live(user_uuid, conversation_id)
        return e.messages if e else None

    async def save(
        self, user_uuid: str, conversation_id: str, messages_json: Messages, title: str | None = None
    ) -> None:
        now = datetime.now(UTC)
        e = self._live(user_uuid, conversation_id)
        if e is None:
            self._data[(user_uuid, conversation_id)] = _Entry(messages_json, title or "", now, now)
        else:
            self._data[(user_uuid, conversation_id)] = replace(e, messages=messages_json, updated_at=now)

    async def list(self, user_uuid: str, limit: int, offset: int) -> list[ConversationSummary]:
        rows = [self._summary(c, e) for (u, c), e in self._data.items() if u == user_uuid and not e.deleted]
        rows.sort(key=lambda r: r.updated_at, reverse=True)
        return rows[offset : offset + limit]

    async def get(self, user_uuid: str, conversation_id: str) -> ConversationRecord | None:
        e = self._live(user_uuid, conversation_id)
        return ConversationRecord(self._summary(conversation_id, e), e.messages) if e else None

    async def delete(self, user_uuid: str, conversation_id: str, hard: bool) -> bool:
        e = self._data.get((user_uuid, conversation_id))
        if e is None or (e.deleted and not hard):
            return False
        if hard:  # also purges an already soft-deleted conversation
            del self._data[(user_uuid, conversation_id)]
        else:
            e.deleted = True
        return True


class MemoryConfirmationStore:
    def __init__(self) -> None:
        self._data: dict[str, PendingConfirmation] = {}

    async def put(self, pending: PendingConfirmation) -> None:
        now = time.time()
        self._data = {k: v for k, v in self._data.items() if v.expires_at > now}
        self._data[pending.id] = pending

    async def take(self, user_uuid: str, confirmation_id: str) -> PendingConfirmation | None:
        p = self._data.get(confirmation_id)
        if p is None or p.user_uuid != user_uuid:
            return None  # same answer for "not yours" and "unknown": no probing
        del self._data[confirmation_id]
        return p if p.expires_at > time.time() else None
