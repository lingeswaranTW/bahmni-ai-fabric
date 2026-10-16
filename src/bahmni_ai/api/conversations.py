# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""A user's own conversations. Every call is scoped to the signed-in user; someone else's id is simply "not found"."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from bahmni_ai.agent import history
from bahmni_ai.api.deps import current_user, error
from bahmni_ai.auth.context import UserContext
from bahmni_ai.logging import get_logger
from bahmni_ai.store import ConversationStore, ConversationSummary

router = APIRouter()
log = get_logger("bahmni_ai.conversations")

ConversationId = Annotated[str, Path(max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


def get_store(request: Request) -> ConversationStore:
    store: ConversationStore = request.app.state.chat_service.conversations
    return store


def _summary(c: ConversationSummary) -> dict[str, Any]:
    return {"id": c.id, "title": c.title, "createdAt": c.created_at, "updatedAt": c.updated_at}


@router.get("/conversations")
async def list_conversations(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    user: UserContext = Depends(current_user),
    store: ConversationStore = Depends(get_store),
) -> dict[str, Any]:
    rows = await store.list(user.user_uuid, limit, offset)
    return {"conversations": [_summary(c) for c in rows], "limit": limit, "offset": offset}


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: ConversationId,
    user: UserContext = Depends(current_user),
    store: ConversationStore = Depends(get_store),
) -> dict[str, Any]:
    record = await store.get(user.user_uuid, conversation_id)
    if record is None:
        raise error(404, "not_found", "Conversation not found.")
    return {**_summary(record.summary), "messages": history.to_transcript(history.from_storage(record.messages_json))}


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: ConversationId,
    hard: bool = False,
    user: UserContext = Depends(current_user),
    store: ConversationStore = Depends(get_store),
) -> Response:
    """Soft delete by default (hidden, row kept); ``?hard=true`` removes it for good."""
    if not await store.delete(user.user_uuid, conversation_id, hard):
        raise error(404, "not_found", "Conversation not found.")
    log.info("conversation_deleted", user=user.username, conversation=conversation_id, hard=hard)
    return Response(status_code=204)
