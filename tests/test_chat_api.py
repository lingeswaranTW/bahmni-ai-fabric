# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

import json

import httpx
import respx
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from bahmni_ai.agent import history
from tests.test_registry_policy import ALL

SESSION_URL = "http://omrs:8080/openmrs/ws/rest/v1/session"


def sse(resp) -> list[tuple[str, dict]]:
    events, name = [], None
    for line in resp.text.splitlines():
        if line.startswith("event:"):
            name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            events.append((name, json.loads(line.split(":", 1)[1])))
    return events


def scripted(tool=None, args=None):
    def returns(messages):
        return [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]

    def fn(messages, info):
        rets = returns(messages)
        if tool and not rets:
            return ModelResponse(parts=[ToolCallPart(tool, args or {})])
        return ModelResponse(parts=[TextPart(f"result: {rets[-1].content}" if rets else "hello there")])

    async def stream(messages, info):
        rets = returns(messages)
        if tool and not rets:
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps(args or {}))}
        else:
            yield f"result: {rets[-1].content}" if rets else "hello there"

    return FunctionModel(fn, stream_function=stream)


def user_json(name="alice", privs=ALL):
    return {
        "authenticated": True,
        "user": {"uuid": f"u-{name}", "username": name, "privileges": [{"name": p} for p in privs], "roles": []},
    }


H = {"X-OpenMRS-Session-Id": "S1"}


@respx.mock
def test_chat_streams_text_and_persists_history(client_factory, validator):
    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))
    with client_factory(scripted()) as c:
        r = c.post("/bahmni-ai/chat", json={"message": "hi"}, headers=H)
        ev = sse(r)
        names = [n for n, _ in ev]
        assert names[0] == "conversation" and names[-1] == "done" and "text" in names
        cid = ev[0][1]["conversationId"]
        r2 = c.post("/bahmni-ai/chat", json={"message": "again", "conversationId": cid}, headers=H)
        assert sse(r2)[-1][0] == "done"


@respx.mock
def test_tool_call_progress_events(client_factory, validator):
    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))
    with client_factory(scripted("docs__search", {"query": "q"})) as c:
        ev = sse(c.post("/bahmni-ai/chat", json={"message": "find"}, headers=H))
    assert ("tool_started", {"server": "docs", "tool": "search"}) in ev
    assert ("tool_finished", {"server": "docs", "tool": "search", "ok": True}) in ev
    assert any(n == "text" and "docs:search:q" in d["delta"] for n, d in ev)


@respx.mock
def test_write_needs_confirmation_then_confirm_endpoint_executes(client_factory, validator):
    from tests import stubs

    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))
    with client_factory(scripted("jira__create_issue", {"title": "T", "project": "BAH"})) as c:
        ev = sse(c.post("/bahmni-ai/chat", json={"message": "make issue"}, headers=H))
        conf = [d for n, d in ev if n == "confirmation_required"][0]
        assert conf["actions"][0]["tool"] == "create_issue" and conf["actions"][0]["arguments"]["title"] == "T"
        assert stubs.CALLS.get("jira:create_issue") is None and ev[-2][0] == "confirmation_required"
        ev2 = sse(c.post("/bahmni-ai/chat/confirm", json={"confirmationId": conf["confirmationId"]}, headers=H))
        assert stubs.CALLS["jira:create_issue"] == 1 and ev2[-1][0] == "done"
        # single use: replay is rejected and nothing runs twice
        ev3 = sse(c.post("/bahmni-ai/chat/confirm", json={"confirmationId": conf["confirmationId"]}, headers=H))
        assert ev3[0][1]["code"] == "confirmation_expired" and stubs.CALLS["jira:create_issue"] == 1


@respx.mock
def test_rejecting_confirmation_runs_nothing(client_factory, validator):
    from tests import stubs

    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))
    with client_factory(scripted("jira__create_issue", {"title": "T", "project": "BAH"})) as c:
        conf = [
            d
            for n, d in sse(c.post("/bahmni-ai/chat", json={"message": "x"}, headers=H))
            if n == "confirmation_required"
        ][0]
        ev = sse(
            c.post(
                "/bahmni-ai/chat/confirm", json={"confirmationId": conf["confirmationId"], "approved": False}, headers=H
            )
        )
    assert "jira:create_issue" not in stubs.CALLS and ev[-1][0] == "done"


@respx.mock
def test_other_user_cannot_use_my_confirmation(client_factory, validator):
    from tests import stubs

    validator.add("S1", "alice", ALL)
    validator.add("S2", "mallory", ALL)
    respx.get(SESSION_URL).mock(
        side_effect=lambda req: httpx.Response(
            200, json=user_json("alice" if "S1" in req.headers["cookie"] else "mallory")
        )
    )
    with client_factory(scripted("jira__create_issue", {"title": "T", "project": "BAH"})) as c:
        conf = [
            d
            for n, d in sse(c.post("/bahmni-ai/chat", json={"message": "x"}, headers=H))
            if n == "confirmation_required"
        ][0]
        ev = sse(
            c.post(
                "/bahmni-ai/chat/confirm",
                json={"confirmationId": conf["confirmationId"]},
                headers={"X-OpenMRS-Session-Id": "S2"},
            )
        )
    assert ev[0][1]["code"] == "confirmation_expired" and "jira:create_issue" not in stubs.CALLS


@respx.mock
def test_history_is_private_to_each_user_and_omits_tool_results(client_factory, validator):
    validator.add("S1", "alice", ALL)
    validator.add("S2", "bob", ALL)
    respx.get(SESSION_URL).mock(
        side_effect=lambda req: httpx.Response(200, json=user_json("alice" if "S1" in req.headers["cookie"] else "bob"))
    )
    with client_factory(scripted("docs__search", {"query": "secret-patient"})) as c:
        cid = sse(c.post("/bahmni-ai/chat", json={"message": "q"}, headers=H))[0][1]["conversationId"]
        store = c.app.state.chat_service._conversations
        import asyncio

        mine = asyncio.run(store.load("u-alice", cid))
        assert isinstance(mine, list)  # plain JSON, not bytes
        returns = [p for m in history.from_storage(mine) for p in m.parts if isinstance(p, ToolReturnPart)]
        assert returns and all(r.content == "[tool result not stored]" for r in returns)
        assert asyncio.run(store.load("u-bob", cid)) is None


def test_no_cors_headers_and_unauthenticated_rejected(client_factory):
    with client_factory(scripted()) as c:
        r = c.post("/bahmni-ai/chat", json={"message": "hi"}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 401 and "access-control-allow-origin" not in r.headers


@respx.mock
def test_session_expiry_mid_run_is_reported(client_factory, validator):
    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))
    with client_factory(scripted("docs__search", {"query": "q"})) as c:
        validator.users.clear()  # OpenMRS forgets the session before the tool runs
        ev = sse(c.post("/bahmni-ai/chat", json={"message": "x"}, headers=H))
    assert ev[-1] == ("error", {"code": "session_expired", "message": "Session expired. Please log in again."})


@respx.mock
def test_provider_failure_becomes_clean_error(client_factory, validator):
    from pydantic_ai.exceptions import ModelHTTPError

    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))

    def boom(messages, info):
        raise ModelHTTPError(status_code=503, model_name="m", body="upstream key=sk-abc")

    with client_factory(FunctionModel(boom, stream_function=boom)) as c:
        ev = sse(c.post("/bahmni-ai/chat", json={"message": "x"}, headers=H))
    assert ev[-1][1]["code"] == "provider_error" and "sk-abc" not in json.dumps(ev)


@respx.mock
def test_invalid_request_bodies(client_factory, validator):
    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))
    with client_factory(scripted()) as c:
        assert c.post("/bahmni-ai/chat", json={"message": "x", "user": "root"}, headers=H).status_code == 422
        assert c.post("/bahmni-ai/chat", json={"message": "x", "conversationId": "../x"}, headers=H).status_code == 422
        assert sse(c.post("/bahmni-ai/chat", json={"message": "  "}, headers=H))[-1][1]["code"] == "invalid_request"


@respx.mock
def test_conversations_api_lists_reads_and_deletes_only_my_own(client_factory, validator):
    validator.add("S1", "alice", ALL)
    validator.add("S2", "bob", ALL)
    respx.get(SESSION_URL).mock(
        side_effect=lambda req: httpx.Response(200, json=user_json("alice" if "S1" in req.headers["cookie"] else "bob"))
    )
    bob = {"X-OpenMRS-Session-Id": "S2"}
    with client_factory(scripted("docs__search", {"query": "canary-arg-123"})) as c:
        cid = sse(c.post("/bahmni-ai/chat", json={"message": "find the policy"}, headers=H))[0][1]["conversationId"]

        listed = c.get("/bahmni-ai/conversations", headers=H).json()["conversations"]
        assert [x["id"] for x in listed] == [cid] and listed[0]["title"] == "find the policy"
        assert c.get("/bahmni-ai/conversations", headers=bob).json()["conversations"] == []

        got = c.get(f"/bahmni-ai/conversations/{cid}", headers=H).json()
        assert [m["role"] for m in got["messages"]] == ["user", "assistant"]
        assert got["messages"][0]["text"] == "find the policy"
        assert got["messages"][1]["tools"] == [{"server": "docs", "tool": "search"}]
        assert set(got["messages"][1]) == {"role", "text", "timestamp", "tools"}  # no tool arguments or results

        assert c.get(f"/bahmni-ai/conversations/{cid}", headers=bob).status_code == 404
        assert c.delete(f"/bahmni-ai/conversations/{cid}", headers=bob).status_code == 404

        assert c.delete(f"/bahmni-ai/conversations/{cid}", headers=H).status_code == 204  # soft
        assert c.get(f"/bahmni-ai/conversations/{cid}", headers=H).status_code == 404
        assert c.get("/bahmni-ai/conversations", headers=H).json()["conversations"] == []
        assert c.delete(f"/bahmni-ai/conversations/{cid}", headers=H).status_code == 404
        assert c.delete(f"/bahmni-ai/conversations/{cid}?hard=true", headers=H).status_code == 204  # purge


@respx.mock
def test_continuing_a_deleted_conversation_starts_fresh(client_factory, validator):
    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))
    with client_factory(scripted()) as c:
        cid = sse(c.post("/bahmni-ai/chat", json={"message": "first secret"}, headers=H))[0][1]["conversationId"]
        c.delete(f"/bahmni-ai/conversations/{cid}", headers=H)
        sse(c.post("/bahmni-ai/chat", json={"message": "second", "conversationId": cid}, headers=H))
        got = c.get(f"/bahmni-ai/conversations/{cid}", headers=H).json()
    assert [m["text"] for m in got["messages"] if m["role"] == "user"] == ["second"]


def test_conversations_api_requires_login_and_validates_input(client_factory):
    with client_factory(scripted()) as c:
        assert c.get("/bahmni-ai/conversations").status_code == 401
        assert c.delete("/bahmni-ai/conversations/abc").status_code == 401


@respx.mock
def test_model_sees_trimmed_history_but_storage_and_transcript_keep_everything(client_factory, validator):
    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))
    seen: list[int] = []

    async def stream(messages, info):
        seen.append(len(messages))
        yield "ok"

    model = FunctionModel(stream_function=stream)
    with client_factory(model, ai_history_max_tokens=1) as c:
        cid = sse(c.post("/bahmni-ai/chat", json={"message": "one"}, headers=H))[0][1]["conversationId"]
        for text in ("two", "three"):
            sse(c.post("/bahmni-ai/chat", json={"message": text, "conversationId": cid}, headers=H))
        # the model always gets the previous turn plus the new prompt; turn one is dropped from turn three on
        assert seen == [1, 3, 3]
        transcript = c.get(f"/bahmni-ai/conversations/{cid}", headers=H).json()["messages"]
        assert [m["text"] for m in transcript if m["role"] == "user"] == ["one", "two", "three"]


@respx.mock
def test_rate_limited_provider_is_reported_without_provider_text(client_factory, validator):
    from pydantic_ai.exceptions import ModelHTTPError

    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))

    async def stream(messages, info):
        raise ModelHTTPError(429, "m", body="PROVIDER-SECRET-TEXT")
        yield ""  # pragma: no cover

    with client_factory(FunctionModel(stream_function=stream)) as c:
        r = c.post("/bahmni-ai/chat", json={"message": "hi"}, headers=H)
        ev = sse(r)
        assert ev[-1][0] == "error" or ev[-2][0] == "error"
        assert any(n == "error" and d["code"] == "rate_limited" for n, d in ev)
        assert "PROVIDER-SECRET-TEXT" not in r.text
