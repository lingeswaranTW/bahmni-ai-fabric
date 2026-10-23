# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Temporary "what the assistant is doing" events: order, pairing, privacy, retry visibility, switch."""

import httpx
import respx
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import DeltaThinkingPart, FunctionModel

from bahmni_ai.providers.retry import RetryingModel
from tests.test_chat_api import SESSION_URL, H, scripted, sse, user_json
from tests.test_registry_policy import ALL

CANARY_PROMPT = "CANARY-PROMPT-8841"
CANARY_ARG = "CANARY-ARG-5527"
CANARY_THOUGHT = "CANARY-THOUGHT-3390"


def login(validator):
    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))


def activities(ev):
    return [(d["kind"], d["state"]) for n, d in ev if n == "activity"]


@respx.mock
def test_plain_answer_shows_the_model_working_then_clears_with_done(client_factory, validator):
    login(validator)
    with client_factory(scripted()) as c:
        ev = sse(c.post("/bahmni-ai/chat", json={"message": "hi"}, headers=H))
    assert activities(ev) == [("model", "started"), ("model", "finished")]
    names = [n for n, _ in ev]
    assert names.index("activity") < names.index("text") and names[-1] == "done"


@respx.mock
def test_tool_round_shows_tool_lines_and_the_model_working_again(client_factory, validator):
    login(validator)
    with client_factory(scripted("docs__search", {"query": "q"})) as c:
        ev = sse(c.post("/bahmni-ai/chat", json={"message": "find"}, headers=H))
    assert activities(ev) == [
        ("model", "started"),
        ("model", "finished"),
        ("tool", "started"),
        ("tool", "finished"),
        ("model", "started"),
        ("model", "finished"),
    ]
    tool_lines = [d for n, d in ev if n == "activity" and d["kind"] == "tool"]
    assert tool_lines[0]["id"] == tool_lines[1]["id"]  # started and finished are paired by id
    assert tool_lines[0]["server"] == "docs" and tool_lines[0]["tool"] == "search"
    assert ("tool_started", {"server": "docs", "tool": "search"}) in ev  # existing events are unchanged
    ids = [d["id"] for n, d in ev if n == "activity"]
    assert len(ids) == 6 and len(set(ids)) == 3  # model-1, the tool call, model-2: each started and finished once


@respx.mock
def test_thinking_is_reported_as_a_transition_without_its_text(client_factory, validator):
    login(validator)

    async def stream(messages, info):
        yield {0: DeltaThinkingPart(content=CANARY_THOUGHT)}
        yield {0: DeltaThinkingPart(content=" more " + CANARY_THOUGHT)}
        yield "final answer"

    with client_factory(FunctionModel(stream_function=stream)) as c:
        r = c.post("/bahmni-ai/chat", json={"message": "hi"}, headers=H)
    ev = sse(r)
    assert activities(ev) == [
        ("model", "started"),
        ("model", "finished"),
        ("thinking", "started"),
        ("thinking", "finished"),
    ]
    assert CANARY_THOUGHT not in r.text  # reasoning text never reaches the browser


@respx.mock
def test_activity_events_carry_no_prompt_arguments_or_reasoning(client_factory, validator):
    login(validator)
    with client_factory(scripted("docs__search", {"query": CANARY_ARG})) as c:
        r = c.post("/bahmni-ai/chat", json={"message": CANARY_PROMPT}, headers=H)
    blob = " ".join(str(d) for n, d in sse(r) if n == "activity")
    assert blob and CANARY_PROMPT not in blob and CANARY_ARG not in blob


@respx.mock
def test_a_provider_retry_is_visible_while_the_model_call_is_blocked(client_factory, validator):
    login(validator)
    failures = [ModelHTTPError(429, "m")]

    async def stream(messages, info):
        if failures:
            raise failures.pop(0)
        yield "ok after retry"

    async def no_sleep(_):  # keep the test fast; the notice is emitted around the sleep
        return None

    model = RetryingModel(FunctionModel(stream_function=stream, model_name="m"), max_retries=2, sleep=no_sleep)
    with client_factory(model) as c:
        ev = sse(c.post("/bahmni-ai/chat", json={"message": "hi"}, headers=H))
    retry = [d for n, d in ev if n == "activity" and d["kind"] == "retry"]
    assert [d["state"] for d in retry] == ["started", "finished"] and retry[0]["attempt"] == 1
    assert ev[-1][0] == "done"
    assert any(n == "text" and d["delta"] == "ok after retry" for n, d in ev)


@respx.mock
def test_a_failing_provider_ends_with_an_error_after_the_activity_lines(client_factory, validator):
    login(validator)

    async def stream(messages, info):
        raise ModelHTTPError(500, "m", body="PROVIDER-TEXT")
        yield ""  # pragma: no cover

    with client_factory(FunctionModel(stream_function=stream)) as c:
        r = c.post("/bahmni-ai/chat", json={"message": "hi"}, headers=H)
    ev = sse(r)
    assert ev[-1][0] == "error" and ev[-1][1]["code"] == "provider_error" and "done" not in [n for n, _ in ev]
    assert ("model", "started") in activities(ev) and "PROVIDER-TEXT" not in r.text


@respx.mock
def test_confirmation_request_ends_the_stream_after_the_activity_lines(client_factory, validator):
    login(validator)
    with client_factory(scripted("jira__create_issue", {"title": "T", "project": "BAH"})) as c:
        ev = sse(c.post("/bahmni-ai/chat", json={"message": "make issue"}, headers=H))
    names = [n for n, _ in ev]
    assert "confirmation_required" in names and names[-1] == "done"
    assert names.index("activity") < names.index("confirmation_required")


@respx.mock
def test_switch_turns_activity_events_off_but_keeps_the_rest(client_factory, validator):
    login(validator)
    with client_factory(scripted("docs__search", {"query": "q"}), ui_activity_events=False) as c:
        ev = sse(c.post("/bahmni-ai/chat", json={"message": "find"}, headers=H))
    names = [n for n, _ in ev]
    assert "activity" not in names and "tool_started" in names and names[-1] == "done"


async def test_closing_the_stream_early_cancels_the_run(docs_url, jira_url, validator, audit):
    """A client that goes away must not leave the agent run (and its MCP connections) behind."""
    import asyncio

    from bahmni_ai.agent.factory import build_agent
    from bahmni_ai.agent.service import ChatService
    from bahmni_ai.auth.privileges import PrivilegeChecker
    from bahmni_ai.db.store import MemoryConfirmationStore, MemoryConversationStore
    from bahmni_ai.settings import Settings
    from tests.test_registry_policy import registry

    never = asyncio.Event()

    async def stream(messages, info):
        await never.wait()  # the model call hangs
        yield "unreachable"

    user = validator.add("S1", "alice", ALL)
    svc = ChatService(
        build_agent(FunctionModel(stream_function=stream)),
        registry(docs_url, jira_url),
        MemoryConversationStore(),
        MemoryConfirmationStore(),
        PrivilegeChecker(validator),
        audit,
        Settings(openmrs_url="http://omrs:8080"),
    )
    gen = svc.chat(user, "hi", None)
    assert (await gen.__anext__()).name == "conversation"
    assert (await gen.__anext__()).name == "activity"  # visible while the model call is blocked
    await gen.aclose()
    await asyncio.sleep(0.05)
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "chat-run" and not t.done()]
