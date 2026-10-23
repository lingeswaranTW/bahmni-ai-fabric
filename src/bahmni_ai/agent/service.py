# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""One chat turn: authenticate (done by the caller) -> connect tools -> run the agent -> stream events."""

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ToolReturnPart,
)
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.usage import UsageLimits

from bahmni_ai import observability
from bahmni_ai.agent import history
from bahmni_ai.agent.deps import AgentDeps
from bahmni_ai.agent.events import (
    Activity,
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
from bahmni_ai.db.store import ConfirmationStore, ConversationStore, PendingConfirmation, new_id
from bahmni_ai.logging import get_logger
from bahmni_ai.mcp.config import TOOL_SEPARATOR
from bahmni_ai.mcp.registry import McpRegistry
from bahmni_ai.providers.retry import retry_listener
from bahmni_ai.settings import Settings

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
        prompt_version: Callable[[], str | None] | None = None,
    ):
        self._agent, self._registry = agent, registry
        self._conversations, self._confirmations = conversations, confirmations
        self._privileges, self._audit, self._settings = privileges, audit, settings
        self._prompt_version = prompt_version or (lambda: None)

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
        full_history = past
        past = history.trim_to_budget(past, self._settings.ai_history_max_tokens)  # what the model sees
        if len(past) < len(full_history):
            log.info("history_trimmed", kept=len(past), dropped=len(full_history) - len(past))
        stored = next((m.conversation_id for m in reversed(past) if m.conversation_id), None)
        session = observability.session_id(cid, stored)  # None when telemetry is off
        observability.tag_session(session)
        # The agent run lives in its own task and hands events over through a queue. That lets a retry notice reach the
        # user while the model call is blocked, and keeps every async context in the task that entered it.
        queue: asyncio.Queue[Event | _Failure | _End] = asyncio.Queue()
        producer = asyncio.create_task(
            self._produce(user, cid, full_history, past, prompt, deferred, deps, session, queue), name="chat-run"
        )
        try:
            while True:
                item = await queue.get()
                if isinstance(item, _End):
                    break
                if isinstance(item, _Failure):
                    raise item.error
                yield item
        except SessionExpiredError:
            yield ErrorEvent("session_expired", "Session expired. Please log in again.")
            return
        except OpenmrsUnavailableError:
            yield ErrorEvent("openmrs_unavailable", "Cannot verify permissions right now. Please try again.")
            return
        except UsageLimitExceeded:
            yield ErrorEvent("step_limit", "The request exceeded its step or token limit and was stopped.")
            return
        except (ModelAPIError, FallbackExceptionGroup) as exc:
            yield _provider_error(exc)
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
        finally:
            producer.cancel()  # also when the client disconnects mid-stream
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer
        yield Done()

    async def _produce(
        self, user: UserContext, cid: str, full_history: list[ModelMessage], past: list[ModelMessage],
        prompt: str | None, deferred: DeferredToolResults | None, deps: AgentDeps, session: str | None,
        queue: "asyncio.Queue[Event | _Failure | _End]",
    ) -> None:  # fmt: skip
        activity = _ActivityTracker(queue.put_nowait if self._settings.ui_activity_events else None)
        retry_listener.set(activity.on_retry)
        try:
            async with self._registry.open(user) as opened:
                if opened.unavailable:
                    queue.put_nowait(ServersUnavailable(opened.unavailable))
                async with self._agent.run_stream_events(
                    prompt, message_history=past, deferred_tool_results=deferred, deps=deps,
                    conversation_id=session,  # same value on every turn of a conversation; None = telemetry off
                    toolsets=opened.toolsets,
                    usage_limits=UsageLimits(
                        request_limit=self._settings.ai_max_tool_steps,
                        total_tokens_limit=self._settings.ai_max_total_tokens or None,
                    ),
                ) as events:  # fmt: skip
                    activity.model_started()
                    async for ev in events:
                        activity.on_event(ev)
                        if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart) and ev.part.content:
                            queue.put_nowait(TextDelta(ev.part.content))
                        elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                            queue.put_nowait(TextDelta(ev.delta.content_delta))
                        elif isinstance(ev, FunctionToolCallEvent):
                            server, tool = split_name(ev.part.tool_name)
                            queue.put_nowait(ToolStarted(server, tool))
                        elif isinstance(ev, FunctionToolResultEvent) and isinstance(ev.part, ToolReturnPart):
                            server, tool = split_name(ev.part.tool_name)
                            queue.put_nowait(ToolFinished(server, tool, ok=not _is_error(ev.part.content)))
                        elif isinstance(ev, AgentRunResultEvent):
                            usage = ev.result.usage
                            log.info(
                                "chat_usage", requests=usage.requests, input_tokens=usage.input_tokens,
                                output_tokens=usage.output_tokens, tool_calls=usage.tool_calls,
                                prompt_version=self._prompt_version(),
                            )  # fmt: skip
                            # Storage keeps the full conversation even when the model only saw a trimmed window.
                            messages = [*full_history, *ev.result.new_messages()]
                            async for out in self._finish(user, cid, ev.result.output, messages):
                                queue.put_nowait(out)
        except Exception as exc:  # re-raised by _run, which maps it to a client-safe error
            queue.put_nowait(_Failure(exc))
            return
        queue.put_nowait(_End())

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


def _provider_error(exc: ModelAPIError | FallbackExceptionGroup) -> ErrorEvent:
    """Map a provider failure (or all fallbacks failing) to one client-safe event. Never echoes provider text."""
    errors: list[BaseException] = list(exc.exceptions) if isinstance(exc, FallbackExceptionGroup) else [exc]
    statuses = [e.status_code for e in errors if isinstance(e, ModelHTTPError)]
    log.warning("provider_error", statuses=statuses, errors=[e.__class__.__name__ for e in errors])
    if statuses and all(s == 429 for s in statuses) and len(statuses) == len(errors):
        return ErrorEvent("rate_limited", "The AI model service is busy. Please try again shortly.")
    return ErrorEvent("provider_error", "The AI model service returned an error. Please try again.")


class _End:
    """Queue marker: the run finished normally."""


@dataclass
class _Failure:
    error: Exception


class _ActivityTracker:
    """Turns Pydantic AI stream events and retry notices into content-free ``Activity`` events, on transitions only.

    ``emit`` is None when activity events are switched off. Open activities are not closed at the end of the stream:
    the UI removes them all on done/error/confirmation_required.
    """

    def __init__(self, emit: Callable[[Event], None] | None):
        self._emit = emit
        self._n = 0
        self._model_id: str | None = None
        self._thinking_id: str | None = None

    def _send(self, id_: str, kind: str, state: str, **extra: Any) -> None:
        if self._emit:
            self._emit(Activity(id_, kind, state, **extra))

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n}"

    def model_started(self) -> None:
        if self._model_id is None:
            self._model_id = self._next("model")
            self._send(self._model_id, "model", "started")

    def _model_finished(self) -> None:
        if self._model_id is not None:
            self._send(self._model_id, "model", "finished")
            self._model_id = None

    def _thinking_finished(self) -> None:
        if self._thinking_id is not None:
            self._send(self._thinking_id, "thinking", "finished")
            self._thinking_id = None

    def on_event(self, ev: Any) -> None:
        if isinstance(ev, PartStartEvent):
            self._model_finished()  # the model answered: first part of this request
            if isinstance(ev.part, ThinkingPart):
                if self._thinking_id is None:
                    self._thinking_id = self._next("thinking")
                    self._send(self._thinking_id, "thinking", "started")
            else:
                self._thinking_finished()
        elif isinstance(ev, FunctionToolCallEvent):
            self._model_finished()
            self._thinking_finished()
            server, tool = split_name(ev.part.tool_name)
            self._send(ev.part.tool_call_id, "tool", "started", server=server or None, tool=tool)
        elif isinstance(ev, FunctionToolResultEvent):
            part = ev.part
            server, tool = split_name(part.tool_name) if isinstance(part, ToolReturnPart) else ("", "")
            self._send(ev.tool_call_id, "tool", "finished", server=server or None, tool=tool or None)
            self.model_started()  # the results go back to the model: it is working again

    def on_retry(self, phase: str, attempt: int) -> None:
        self._send(f"retry-{attempt}", "retry", "started" if phase == "waiting" else "finished", attempt=attempt)


def _is_error(content: Any) -> bool:
    return isinstance(content, dict) and "error" in content
