# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Prompt hub: OpenLIT source, resolver (cache, fallback, modes), configuration, and the agent wiring."""

import asyncio
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from bahmni_ai.agent.factory import build_agent
from bahmni_ai.agent.prompts import SYSTEM_PROMPT
from bahmni_ai.main import create_app
from bahmni_ai.prompts import (
    FetchedPrompt,
    PromptRef,
    PromptResolver,
    PromptSourceError,
    build_resolver,
    registry,
)
from bahmni_ai.prompts.openlit import OpenLITAdapter, OpenLITPromptSource
from bahmni_ai.prompts.resolver import APPEND_HEADER
from bahmni_ai.settings import Settings
from tests.test_chat_api import SESSION_URL, scripted, user_json
from tests.test_registry_policy import ALL

URL = "http://openlit:3000"
ENDPOINT = f"{URL}/api/prompt/get-compiled"
API_KEY = "olk-secret-key-123"


def ok(text="Be concise.", version="1.0.0"):
    return httpx.Response(
        200, json={"err": None, "res": {"prompt": "raw " + text, "compiledPrompt": text, "version": version}}
    )


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.setattr(registry, "_adapters", {})
    monkeypatch.setattr(registry, "_loaded", False)
    monkeypatch.setattr(registry, "_discover", lambda: [])


# --- OpenLIT source ------------------------------------------------------------------------------------------


@pytest.fixture
async def source():
    s = OpenLITPromptSource(URL, API_KEY, timeout_seconds=1)
    yield s
    await s.aclose()


@respx.mock
async def test_openlit_fetch_sends_the_documented_request_and_uses_the_compiled_prompt(source):
    route = respx.post(ENDPOINT).mock(return_value=ok("Hello {{x}}", "2.1.0"))
    got = await source.fetch(PromptRef("bahmni-system", "2.1.0", {"x": "y"}))
    assert got == FetchedPrompt("Hello {{x}}", "2.1.0")
    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {API_KEY}"
    assert json.loads(req.content) == {
        "name": "bahmni-system",
        "shouldCompile": True,
        "source": "bahmni-ai",
        "version": "2.1.0",
        "variables": {"x": "y"},
    }


@respx.mock
async def test_openlit_fetch_without_version_asks_for_latest(source):
    route = respx.post(ENDPOINT).mock(return_value=ok())
    await source.fetch(PromptRef("p"))
    assert "version" not in json.loads(route.calls.last.request.content)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"err": "No prompt found", "res": None}),  # OpenLIT reports errors with HTTP 200
        httpx.Response(200, json={"err": None, "res": None}),
        httpx.Response(200, json={"err": None, "res": {"compiledPrompt": "   ", "prompt": ""}}),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, text="<html>proxy error</html>"),
        httpx.Response(401, json={"err": "bad key"}),
        httpx.Response(503),
    ],
)
@respx.mock
async def test_openlit_failures_raise_prompt_source_error_without_leaking_the_key(source, response):
    respx.post(ENDPOINT).mock(return_value=response)
    with pytest.raises(PromptSourceError) as e:
        await source.fetch(PromptRef("p"))
    assert API_KEY not in str(e.value)


@respx.mock
async def test_openlit_unreachable_and_timeout_are_prompt_source_errors(source):
    for exc in (httpx.ConnectError("refused"), httpx.ReadTimeout("slow")):
        respx.post(ENDPOINT).mock(side_effect=exc)
        with pytest.raises(PromptSourceError):
            await source.fetch(PromptRef("p"))


def test_openlit_adapter_validates_its_configuration(monkeypatch):
    from bahmni_ai.prompts import PromptSourceSettings

    adapter = OpenLITAdapter()
    monkeypatch.delenv("OPENLIT_API_KEY", raising=False)
    with pytest.raises(PromptSourceError, match="OPENLIT_API_KEY"):
        adapter.create(PromptSourceSettings(URL))
    monkeypatch.setenv("OPENLIT_API_KEY", API_KEY)
    with pytest.raises(PromptSourceError, match="http"):
        adapter.create(PromptSourceSettings("ftp://x"))
    with pytest.raises(PromptSourceError, match="https"):
        adapter.create(PromptSourceSettings("http://openlit.example.com"))  # the key would travel in clear text
    adapter.create(PromptSourceSettings("https://openlit.example.com"))
    adapter.create(PromptSourceSettings("http://host.docker.internal:3300"))


# --- resolver ------------------------------------------------------------------------------------------------


class FakeSource:
    def __init__(self, *results):
        self.results = list(results)  # FetchedPrompt or Exception, consumed in order; the last one repeats
        self.calls = 0
        self.closed = False

    async def fetch(self, ref):
        self.calls += 1
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r

    async def aclose(self):
        self.closed = True


def resolver(*results, mode="append", max_chars=8000, sleep=None):
    kw = {"sleep": sleep} if sleep else {}
    return PromptResolver(
        "BUILTIN",
        FakeSource(*results),
        PromptRef("p"),
        source_name="fake",
        mode=mode,
        refresh_seconds=60,
        max_chars=max_chars,
        **kw,
    )


async def test_before_any_fetch_the_builtin_prompt_is_used():
    r = resolver(PromptSourceError("down"))
    assert r.current() == "BUILTIN" and r.version is None
    assert await r.refresh() is False
    assert r.current() == "BUILTIN"


async def test_append_mode_keeps_the_builtin_rules_and_adds_the_hub_text_after_them():
    r = resolver(FetchedPrompt("Use metric units.", "3"))
    assert await r.refresh()
    assert r.current() == f"BUILTIN\n\n{APPEND_HEADER}\nUse metric units." and r.version == "3"


async def test_replace_mode_uses_only_the_hub_text():
    r = resolver(FetchedPrompt("Whole prompt.", "3"), mode="replace")
    await r.refresh()
    assert r.current() == "Whole prompt." and "BUILTIN" not in r.current()


async def test_replace_mode_still_falls_back_to_builtin_when_the_hub_never_answered():
    r = resolver(PromptSourceError("down"), mode="replace")
    await r.refresh()
    assert r.current() == "BUILTIN"


async def test_failed_refresh_keeps_the_last_good_prompt_and_a_later_one_picks_up_the_new_version():
    r = resolver(FetchedPrompt("v1 text", "1"), PromptSourceError("down"), FetchedPrompt("v2 text", "2"))
    await r.refresh()
    assert "v1 text" in r.current()
    assert await r.refresh() is False
    assert "v1 text" in r.current() and r.version == "1"  # last good survives an outage
    assert await r.refresh() is True
    assert "v2 text" in r.current() and r.version == "2"


async def test_unexpected_errors_never_escape_refresh():
    r = resolver(RuntimeError("boom"))
    assert await r.refresh() is False


async def test_oversized_prompt_is_rejected_and_the_previous_one_is_kept():
    r = resolver(FetchedPrompt("ok", "1"), FetchedPrompt("x" * 500, "2"), max_chars=100)
    await r.refresh()
    assert await r.refresh() is False
    assert r.version == "1" and "x" * 500 not in r.current()


async def test_prompt_text_is_never_logged(capsys):
    secret = "TOP-SECRET-PROMPT-BODY"
    r = resolver(FetchedPrompt(secret, "1"), PromptSourceError(f"oops {secret[:0]}"))
    await r.refresh()
    await r.refresh()
    assert secret not in capsys.readouterr().out


async def test_background_refresh_runs_until_stopped():
    gate = asyncio.Event()
    ticks = 0

    async def fake_sleep(_seconds):
        nonlocal ticks
        ticks += 1
        if ticks > 2:
            await gate.wait()  # park, like a real sleep, so stop() has something to cancel

    src = FakeSource(FetchedPrompt("a", "1"), FetchedPrompt("b", "2"), FetchedPrompt("c", "3"))
    r = PromptResolver("BUILTIN", src, PromptRef("p"), source_name="fake", refresh_seconds=60, sleep=fake_sleep)
    await r.start()
    for _ in range(50):
        await asyncio.sleep(0)
    assert r.version == "3" and src.calls == 3  # initial fetch + two scheduled refreshes
    await r.stop()
    assert src.closed and r._task is None
    calls = src.calls
    await asyncio.sleep(0.01)
    assert src.calls == calls  # nothing keeps running after stop


# --- configuration and wiring ---------------------------------------------------------------------------------


def hub_settings(**kw):
    base = {
        "prompt_hub_enabled": True,
        "prompt_hub_url": URL,
        "prompt_hub_prompt_name": "bahmni-system",
        "ai_model": "test",
        "openmrs_url": "http://omrs:8080",
    }
    return Settings(**{**base, **kw})


def test_disabled_by_default_builds_nothing():
    assert build_resolver(Settings()) is None


def test_enabled_but_misconfigured_fails_closed(monkeypatch):
    monkeypatch.setenv("OPENLIT_API_KEY", API_KEY)
    with pytest.raises(PromptSourceError, match="PROMPT_HUB_PROMPT_NAME"):
        build_resolver(hub_settings(prompt_hub_prompt_name=" "))
    with pytest.raises(PromptSourceError, match="unknown prompt source"):
        build_resolver(hub_settings(prompt_hub_source="nope"))
    monkeypatch.delenv("OPENLIT_API_KEY")
    with pytest.raises(PromptSourceError, match="OPENLIT_API_KEY"):
        build_resolver(hub_settings())
    monkeypatch.setenv("OPENLIT_API_KEY", API_KEY)
    assert build_resolver(hub_settings()) is not None


def test_another_service_can_be_plugged_in_as_a_prompt_source(monkeypatch):
    class Other:
        name = "otherhub"

        def create(self, settings):
            return FakeSource(FetchedPrompt("from other hub", "9"))

    monkeypatch.setattr(registry, "_discover", lambda: [Other()])
    r = build_resolver(hub_settings(prompt_hub_source="otherhub"))
    assert r is not None
    asyncio.run(r.refresh())
    assert "from other hub" in r.current()


async def test_agent_gets_the_current_prompt_on_every_run():
    seen: list[str] = []

    def model_fn(messages, info: AgentInfo):
        seen.append(info.instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    r = resolver(FetchedPrompt("rule one", "1"), FetchedPrompt("rule two", "2"))
    agent = build_agent(FunctionModel(model_fn), instructions=r.current)
    await agent.run("hi", deps=None)  # type: ignore[arg-type]
    await r.refresh()
    await agent.run("hi", deps=None)  # type: ignore[arg-type]
    await r.refresh()
    await agent.run("hi", deps=None)  # type: ignore[arg-type]
    assert seen[0] == "BUILTIN" and "rule one" in seen[1] and "rule two" in seen[2]
    assert seen[1].startswith("BUILTIN")


async def test_without_a_resolver_the_builtin_prompt_is_used_exactly_as_before():
    seen: list[str] = []

    def model_fn(messages, info: AgentInfo):
        seen.append(info.instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    await build_agent(FunctionModel(model_fn)).run("hi", deps=None)  # type: ignore[arg-type]
    assert seen == [SYSTEM_PROMPT.strip()]


@respx.mock
def test_service_starts_loads_the_prompt_and_reports_its_version(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENLIT_API_KEY", API_KEY)
    cfg = tmp_path / "m.json"
    cfg.write_text('{"mcpServers": {}}')
    route = respx.post(ENDPOINT).mock(return_value=ok("Answer in English.", "7"))
    s = hub_settings(mcp_config_path=str(cfg))
    with TestClient(create_app(s)) as c:
        assert c.get("/bahmni-ai/health").status_code == 200
        assert route.called
    out = capsys.readouterr().out
    assert "prompt_loaded" in out and "Answer in English." not in out and API_KEY not in out


@respx.mock
def test_service_still_starts_when_the_hub_is_down(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENLIT_API_KEY", API_KEY)
    cfg = tmp_path / "m.json"
    cfg.write_text('{"mcpServers": {}}')
    respx.post(ENDPOINT).mock(side_effect=httpx.ConnectError("refused"))
    with TestClient(create_app(hub_settings(mcp_config_path=str(cfg)))) as c:
        assert c.get("/bahmni-ai/health").status_code == 200
    assert "prompt_fallback" in capsys.readouterr().out


def test_service_refuses_to_start_when_the_hub_is_misconfigured(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENLIT_API_KEY", raising=False)
    cfg = tmp_path / "m.json"
    cfg.write_text('{"mcpServers": {}}')
    with pytest.raises(PromptSourceError), TestClient(create_app(hub_settings(mcp_config_path=str(cfg)))):
        pass


@respx.mock
def test_prompt_version_appears_in_the_usage_log(client_factory, validator, capsys):
    from bahmni_ai.agent.service import ChatService

    validator.add("S1", "alice", ALL)
    respx.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json()))
    with client_factory(scripted()) as c:
        svc: ChatService = c.app.state.chat_service  # type: ignore[attr-defined]
        svc._prompt_version = lambda: "7"
        c.post("/bahmni-ai/chat", json={"message": "hi"}, headers={"X-OpenMRS-Session-Id": "S1"})
    assert '"prompt_version": "7"' in capsys.readouterr().out
