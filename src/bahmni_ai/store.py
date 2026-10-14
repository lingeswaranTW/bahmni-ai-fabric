# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Conversation history and pending confirmations.

History is never an authorization source: it is only model context, and it is always read through the
owning user. Both stores have an in-memory implementation (tests/dev) and a Postgres one (``db/``).
"""

import secrets
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass
class PendingConfirmation:
    id: str
    user_uuid: str
    conversation_id: str
    messages_json: bytes  # full-fidelity run state, kept only until confirmed, rejected, or expired
    actions_json: str
    expires_at: float


class ConversationStore(Protocol):
    async def load(self, user_uuid: str, conversation_id: str) -> bytes | None: ...
    async def save(self, user_uuid: str, conversation_id: str, messages_json: bytes) -> None: ...


class ConfirmationStore(Protocol):
    async def put(self, pending: PendingConfirmation) -> None: ...
    async def take(self, user_uuid: str, confirmation_id: str) -> PendingConfirmation | None:
        """Atomically remove and return the confirmation if it belongs to the user (single use)."""
        ...


def new_id() -> str:
    return secrets.token_urlsafe(24)


class MemoryConversationStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], bytes] = {}

    async def load(self, user_uuid: str, conversation_id: str) -> bytes | None:
        return self._data.get((user_uuid, conversation_id))

    async def save(self, user_uuid: str, conversation_id: str, messages_json: bytes) -> None:
        self._data[(user_uuid, conversation_id)] = messages_json


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
