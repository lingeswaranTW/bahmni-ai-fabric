# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from bahmni_ai.agent.events import Event
from bahmni_ai.agent.service import ChatService
from bahmni_ai.api.deps import current_user
from bahmni_ai.auth.context import UserContext

router = APIRouter()


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(max_length=8000)
    conversationId: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")  # noqa: N815


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmationId: str = Field(max_length=128)  # noqa: N815
    approved: bool = True
    decisions: dict[str, bool] = Field(default_factory=dict)  # per-action override of `approved`


def get_service(request: Request) -> ChatService:
    service: ChatService = request.app.state.chat_service
    return service


def _stream(events: AsyncIterator[Event]) -> EventSourceResponse:
    async def gen() -> AsyncIterator[dict[str, str]]:
        async for ev in events:
            yield {"event": ev.name, "data": json.dumps(ev.data())}

    # X-Accel-Buffering/Cache-Control keep proxies (Apache in bahmni-proxy, bahmni-core) from buffering.
    return EventSourceResponse(gen(), headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/chat")
async def chat(
    body: ChatRequest, user: UserContext = Depends(current_user), service: ChatService = Depends(get_service)
) -> EventSourceResponse:
    return _stream(service.chat(user, body.message, body.conversationId))


@router.post("/chat/confirm")
async def confirm(
    body: ConfirmRequest, user: UserContext = Depends(current_user), service: ChatService = Depends(get_service)
) -> EventSourceResponse:
    return _stream(service.confirm(user, body.confirmationId, body.approved, body.decisions))
