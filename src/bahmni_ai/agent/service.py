# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""One chat turn: authenticate (done by the caller) -> connect tools -> run the agent -> stream events."""

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolReturnPart,
)
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.usage import UsageLimits

from bahmni_ai import observability
from bahmni_ai.agent import history
from bahmni_ai.agent.deps import AgentDeps
from bahmni_ai.agent.events import (
    ConfirmationRequired,
    ConversationStarted,
    Done,
    ErrorEvent,
    Event,
    PendingAction,
    ServersUnavailable,
    TextDelta,
    ToolFinished,
    ToolStarted,
)
from bahmni_ai.audit.log import AuditSink
from bahmni_ai.auth.context import UserContext
from bahmni_ai.auth.privileges import PrivilegeChecker
from bahmni_ai.auth.session import OpenmrsUnavailableError, SessionExpiredError
from bahmni_ai.logging import get_logger
from bahmni_ai.mcp.config import TOOL_SEPARATOR
from bahmni_ai.mcp.registry import McpRegistry
from bahmni_ai.settings import Settings
from bahmni_ai.store import ConfirmationStore, ConversationStore, PendingConfirmation, new_id

log = get_logger("bahmni_ai.chat")

MAX_MESSAGE_CHARS = 8000


class ChatError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def split_name(namespaced: str) -> tuple[str, str]:
    server, sep, tool = namespaced.partition(TOOL_SEPARATOR)
    return (server, tool) if sep else ("", namespaced)


class ChatService:
    def __init__(
        self,
        agent: Agent[AgentDeps, str | DeferredToolRequests],
        registry: McpRegistry,
        conversations: ConversationStore,
        confirmations: ConfirmationStore,
        privileges: PrivilegeChecker,
        audit: AuditSink,
        settings: Settings,
    ):
        self._agent, self._registry = agent, registry
        self._conversations, self._confirmations = conversations, confirmations
        self._privileges, self._audit, self._settings = privileges, audit, settings

    @property
    def conversations(self) -> ConversationStore:
        return self._conversations

    def _deps(self, user: UserContext) -> AgentDeps:
        return AgentDeps(
            user=user, privileges=self._privileges, audit=self._audit,
            gate_privilege=self._settings.gate_privilege, call_timeout_seconds=self._settings.mcp_call_timeout_seconds,
        )  # fmt: skip

    async def chat(self, user: UserContext, message: str, conversation_id: str | None) -> AsyncIterator[Event]:
        if not message.strip() or len(message) > MAX_MESSAGE_CHARS:
            yield ErrorEvent("invalid_request", "Message is empty or too long.")
            return
        cid = conversation_id or uuid.uuid4().hex
        yield ConversationStarted(cid)
        past = history.from_storage(await self._conversations.load(user.user_uuid, cid))
        async for ev in self._observed(self._run(user, cid, past, message, None)):
            yield ev

    async def confirm(
        self, user: UserContext, confirmation_id: str, approved: bool, decisions: dict[str, bool]
    ) -> AsyncIterator[Event]:
        pending = await self._confirmations.take(user.user_uuid, confirmation_id)
        if pending is None:
            yield ErrorEvent("confirmation_expired", "This confirmation is invalid or has expired.")
            return
        actions = json.loads(pending.actions_json)
        results = DeferredToolResults(approvals={
            a["id"]: (True if decisions.get(a["id"], approved) else ToolDenied("The user declined this action."))
            for a in actions
        })  # fmt: skip
        yield ConversationStarted(pending.conversation_id)
        past = history.from_storage(pending.messages_json)
        async for ev in self._observed(self._run(user, pending.conversation_id, past, None, results)):
            yield ev

    @staticmethod
    async def _observed(events: AsyncIterator[Event]) -> AsyncIterator[Event]:
        async for ev in events:
            if isinstance(ev, ErrorEvent):
                observability.record_chat_error(ev.code)
            yield ev

    async def _run(
        self, user: UserContext, cid: str, past: list[ModelMessage], prompt: str | None,
        deferred: DeferredToolResults | None,
    ) -> AsyncIterator[Event]:  # fmt: skip
        deps = self._deps(user)
        stored = next((m.conversation_id for m in reversed(past) if m.conversation_id), None)
        session = observability.session_id(cid, stored)  # None when telemetry is off
        observability.tag_session(session)
        try:
            async with self._registry.open(user) as opened:
                if opened.unavailable:
                    yield ServersUnavailable(opened.unavailable)
                async with self._agent.run_stream_events(
                    prompt, message_history=past, deferred_tool_results=deferred, deps=deps,
                    conversation_id=session,  # same value on every turn of a conversation; None = telemetry off
                    toolsets=opened.toolsets,
                    usage_limits=UsageLimits(request_limit=self._settings.ai_max_tool_steps),
                ) as events:  # fmt: skip
                    async for ev in events:
                        if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart) and ev.part.content:
                            yield TextDelta(ev.part.content)
                        elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                            yield TextDelta(ev.delta.content_delta)
                        elif isinstance(ev, FunctionToolCallEvent):
                            server, tool = split_name(ev.part.tool_name)
                            yield ToolStarted(server, tool)
                        elif isinstance(ev, FunctionToolResultEvent) and isinstance(ev.part, ToolReturnPart):
                            server, tool = split_name(ev.part.tool_name)
                            yield ToolFinished(server, tool, ok=not _is_error(ev.part.content))
                        elif isinstance(ev, AgentRunResultEvent):
                            async for out in self._finish(user, cid, ev.result.output, ev.result.all_messages()):
                                yield out
        except SessionExpiredError:
            yield ErrorEvent("session_expired", "Session expired. Please log in again.")
            return
        except OpenmrsUnavailableError:
            yield ErrorEvent("openmrs_unavailable", "Cannot verify permissions right now. Please try again.")
            return
        except UsageLimitExceeded:
            yield ErrorEvent("step_limit", "The request needed too many steps and was stopped.")
            return
        except ModelHTTPError as exc:
            log.warning("provider_error", status=exc.status_code, model=exc.model_name)
            yield ErrorEvent("provider_error", "The AI model service returned an error. Please try again.")
            return
        except UnexpectedModelBehavior as exc:
            log.warning("assistant_error", error=exc.__class__.__name__)
            yield ErrorEvent("assistant_error", "The assistant could not complete that request.")
            return
        except TimeoutError:
            yield ErrorEvent("timeout", "The request took too long.")
            return
        except Exception as exc:  # never leak internals to the client
            log.error("chat_failed", error=exc.__class__.__name__, exc_info=True)
            yield ErrorEvent("internal_error", "Something went wrong.")
            return
        yield Done()

    async def _finish(
        self, user: UserContext, cid: str, output: Any, messages: list[ModelMessage]
    ) -> AsyncIterator[Event]:
        if isinstance(output, DeferredToolRequests):
            actions = []
            for call in output.approvals:
                server, tool = split_name(call.tool_name)
                actions.append(PendingAction(call.tool_call_id, server, tool, call.args_as_dict()))
            cid_ = new_id()
            ttl = self._settings.confirmation_ttl_seconds
            await self._confirmations.put(PendingConfirmation(
                id=cid_, user_uuid=user.user_uuid, conversation_id=cid,
                messages_json=history.to_json(messages),
                actions_json=json.dumps([a.__dict__ for a in actions], default=str),
                expires_at=time.time() + ttl,
            ))  # fmt: skip
            yield ConfirmationRequired(cid_, ttl, actions)
            return
        await self._conversations.save(
            user.user_uuid, cid, history.for_storage(messages, self._settings.history_tool_results),
            history.title_of(messages),
        )  # fmt: skip


def _is_error(content: Any) -> bool:
    return isinstance(content, dict) and "error" in content
