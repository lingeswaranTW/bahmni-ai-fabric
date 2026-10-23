# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Provider layer: registry, AI_MODEL parsing, fallback, retry/backoff, error mapping, history budget."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import AgentInfo, FunctionModel

from bahmni_ai.agent import history
from bahmni_ai.agent.factory import ModelConfigError, build_agent
from bahmni_ai.providers import ProviderCapabilities, ProviderError, ProviderSettings, registry, resolve_model
from bahmni_ai.providers.retry import RetryingModel, retry_after_seconds


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.setattr(registry, "_adapters", {})
    monkeypatch.setattr(registry, "_loaded", False)
    monkeypatch.setattr(registry, "_discover", lambda: [])


# --- a provider that exists only in this test: no core file changes are needed to add it -----------------------


@dataclass(frozen=True)
class DummyAdapter:
    name: str = "dummy"
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    def build(self, model_name: str, settings: ProviderSettings) -> Model:
        return FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"dummy:{model_name}")]), model_name=model_name)


async def test_new_provider_needs_only_an_adapter():
    registry.register(DummyAdapter())
    agent = build_agent("dummy:m1")
    assert (await agent.run("hi", deps=None)).output == "dummy:m1"  # type: ignore[arg-type]


def test_plugin_entry_point_is_discovered(monkeypatch):
    monkeypatch.setattr(registry, "_discover", lambda: [DummyAdapter(name="plug")])
    assert "plug" in registry.registered_names()
    assert resolve_model("plug:x") is not None


def test_builtin_providers_are_registered():
    assert {"openai", "anthropic", "ollama", "gemini", "test"} <= set(registry.registered_names())


def test_unknown_provider_fails_closed():
    with pytest.raises(ProviderError, match="unknown provider"):
        resolve_model("nonsense:m")
    with pytest.raises(ModelConfigError):
        build_agent("nonsense:m")


def test_empty_spec_and_missing_model_name():
    with pytest.raises(ProviderError):
        resolve_model(" , ")
    with pytest.raises(ProviderError, match="needs a model name"):
        resolve_model("openai")


def test_duplicate_registration_rejected_unless_replace():
    registry.register(DummyAdapter())
    with pytest.raises(ProviderError):
        registry.register(DummyAdapter())
    registry.register(DummyAdapter(), replace=True)
    with pytest.raises(ProviderError):
        registry.register(DummyAdapter(name="Bad:Name"))


def test_comma_separated_models_become_a_fallback_chain():
    registry.register(DummyAdapter())
    assert isinstance(resolve_model("dummy:a, dummy:b"), FallbackModel)
    assert not isinstance(resolve_model("dummy:a"), FallbackModel)


def test_test_provider_needs_no_model_name():
    assert build_agent("test") is not None


def test_missing_key_message_does_not_echo_secrets(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ModelConfigError) as e:
        build_agent("openai:gpt-4o")
    assert "sk-" not in str(e.value)


# --- retry / backoff -----------------------------------------------------------------------------------------


def _flaky(failures: list[Exception]) -> tuple[FunctionModel, list[int]]:
    calls: list[int] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        if failures:
            raise failures.pop(0)
        return ModelResponse(parts=[TextPart("ok")])

    return FunctionModel(fn, model_name="m"), calls


def _retrying(model: Model, retries: int = 2) -> tuple[RetryingModel, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    return RetryingModel(
        model, max_retries=retries, base_delay=1.0, max_delay=10.0, sleep=fake_sleep, jitter=lambda: 1.0
    ), sleeps


async def test_429_is_retried_with_retry_after_then_succeeds():
    inner, calls = _flaky([ModelHTTPError(429, "m", headers={"retry-after": "3"}), ModelHTTPError(503, "m")])
    model, sleeps = _retrying(inner)
    result = await Agent(model).run("hi")
    assert result.output == "ok" and len(calls) == 3
    assert sleeps == [3.0, 2.0]  # Retry-After honoured, then exponential backoff (base 1 * 2**1)


async def test_non_retryable_4xx_is_not_retried():
    inner, calls = _flaky([ModelHTTPError(401, "m")])
    model, sleeps = _retrying(inner)
    with pytest.raises(ModelHTTPError):
        await Agent(model).run("hi")
    assert len(calls) == 1 and sleeps == []


async def test_retries_are_bounded_and_connection_errors_count():
    inner, calls = _flaky([ModelAPIError("m", "connection reset")] * 5)
    model, _ = _retrying(inner, retries=2)
    with pytest.raises(ModelAPIError):
        await Agent(model).run("hi")
    assert len(calls) == 3


async def test_streaming_start_is_retried():
    calls: list[int] = []
    failures: list[Exception] = [ModelHTTPError(429, "m")]

    async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        calls.append(1)
        if failures:
            raise failures.pop(0)
        yield "ok"

    model, _ = _retrying(FunctionModel(stream_function=stream_fn, model_name="m"))
    async with Agent(model).run_stream("hi") as stream:
        assert await stream.get_output() == "ok"
    assert len(calls) == 2


def test_retry_after_parsing():
    assert retry_after_seconds(ModelHTTPError(429, "m", headers={"retry-after": "7"})) == 7.0
    assert retry_after_seconds(ModelHTTPError(429, "m", headers={"retry-after": "junk"})) is None
    assert retry_after_seconds(ModelHTTPError(429, "m")) is None


async def test_fallback_to_second_provider_after_first_is_exhausted():
    first, _ = _flaky([ModelHTTPError(503, "a")] * 10)
    second = FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("from-second")]), model_name="b")
    retrying, _ = _retrying(first, retries=1)
    assert (await Agent(FallbackModel(retrying, second)).run("hi")).output == "from-second"


# --- error mapping in the chat service -----------------------------------------------------------------------


def test_provider_error_mapping_never_echoes_provider_text():
    from pydantic_ai.exceptions import FallbackExceptionGroup

    from bahmni_ai.agent.service import _provider_error

    assert _provider_error(ModelHTTPError(429, "m", body="secret-body")).code == "rate_limited"
    assert _provider_error(ModelHTTPError(500, "m", body="secret-body")).code == "provider_error"
    assert _provider_error(ModelAPIError("m", "secret-detail")).code == "provider_error"
    group = FallbackExceptionGroup("all failed", [ModelHTTPError(429, "a"), ModelHTTPError(429, "b")])
    assert _provider_error(group).code == "rate_limited"
    mixed = FallbackExceptionGroup("all failed", [ModelHTTPError(429, "a"), ModelHTTPError(500, "b")])
    assert _provider_error(mixed).code == "provider_error"
    assert "secret" not in _provider_error(ModelHTTPError(500, "m", body="secret-body")).message


# --- history budget ------------------------------------------------------------------------------------------


def _turn(i: int, size: int = 400) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(f"q{i} " + "x" * size)]),
        ModelResponse(parts=[TextPart(f"a{i} " + "y" * size)]),
    ]


def test_trim_drops_oldest_turns_and_keeps_newest():
    msgs = [m for i in range(6) for m in _turn(i)]
    kept = history.trim_to_budget(msgs, max_tokens=400)
    assert 0 < len(kept) < len(msgs) and kept[-1] is msgs[-1]
    assert isinstance(kept[0], ModelRequest) and kept[0].parts[0].content.startswith("q")  # type: ignore[union-attr]


def test_trim_disabled_and_oversized_latest_turn():
    msgs = [m for i in range(3) for m in _turn(i)]
    assert history.trim_to_budget(msgs, 0) == msgs
    assert history.trim_to_budget(msgs, 1) == msgs[-2:]  # newest turn is kept even when alone over budget


def test_trim_never_splits_a_tool_call_from_its_result():
    call = ModelResponse(parts=[ToolCallPart("docs__search", {"q": 1}, tool_call_id="c1")])
    ret = ModelRequest(parts=[ToolReturnPart("docs__search", "r" * 400, tool_call_id="c1")])
    done = ModelResponse(parts=[TextPart("done")])
    msgs = [*_turn(0), ModelRequest(parts=[UserPromptPart("find")]), call, ret, done]
    for budget in (1, 50, 150, 300, 10_000):
        kept = history.trim_to_budget(msgs, budget)
        if call in kept:
            assert ret in kept
        assert isinstance(kept[0], ModelRequest) and isinstance(kept[0].parts[0], UserPromptPart)


def test_optional_provider_without_its_sdk_gives_an_install_hint(monkeypatch):
    monkeypatch.setattr("bahmni_ai.providers.builtin.find_spec", lambda name: None)
    with pytest.raises(ProviderError, match=r"bahmni-ai\[gemini\]"):
        resolve_model("gemini:gemini-2.5-flash")


# --- every built-in adapter honours the same contract ----------------------------------------------------------

BUILTIN_ENV = {
    "openai": {"OPENAI_API_KEY": "sk-test"},
    "anthropic": {"ANTHROPIC_API_KEY": "sk-ant-test"},
    "ollama": {"OLLAMA_BASE_URL": "http://localhost:11434/v1"},
    "openai-compat": {"OPENAI_COMPAT_BASE_URL": "http://localhost:8000/v1"},
    "gemini": {"GOOGLE_API_KEY": "g-test"},
    "test": {},
}


@pytest.mark.parametrize("name", sorted(BUILTIN_ENV))
def test_builtin_adapter_follows_the_contract(monkeypatch, name):
    from importlib.util import find_spec

    from bahmni_ai.providers import ProviderAdapter

    if name == "gemini" and find_spec("google.genai") is None:
        pytest.skip("gemini extra not installed")
    for k, v in BUILTIN_ENV[name].items():
        monkeypatch.setenv(k, v)
    adapter = registry.get_adapter(name)
    assert isinstance(adapter, ProviderAdapter) and adapter.name == name
    assert isinstance(adapter.capabilities, ProviderCapabilities)
    model = resolve_model(name if name == "test" else f"{name}:some-model")
    assert isinstance(model, RetryingModel)  # same retry layer for every provider
    assert isinstance(build_agent(f"{name}:some-model" if name != "test" else "test"), Agent)


@pytest.mark.parametrize("name", ["openai", "anthropic", "ollama"])
def test_builtin_adapter_requires_a_model_name_and_credentials(monkeypatch, name):
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ProviderError, match="needs a model name"):
        resolve_model(name)
    with pytest.raises(ModelConfigError) as e:
        build_agent(f"{name}:some-model")
    assert "sk-" not in str(e.value)


# --- openai-compat -------------------------------------------------------------------------------------------


def test_openai_compat_builds_a_chat_completions_model(monkeypatch):
    from pydantic_ai.models.openai import OpenAIChatModel

    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://vllm:8000/v1")
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    model = resolve_model("openai-compat:my-model")
    assert isinstance(model, RetryingModel) and isinstance(model.wrapped, OpenAIChatModel)
    assert model.wrapped.model_name == "my-model" and "vllm:8000" in str(model.wrapped.base_url)


def test_openai_compat_needs_a_url_and_a_model_name(monkeypatch):
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    with pytest.raises(ProviderError, match="OPENAI_COMPAT_BASE_URL"):
        resolve_model("openai-compat:m")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "ftp://x")
    with pytest.raises(ProviderError):
        resolve_model("openai-compat:m")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://x/v1")
    with pytest.raises(ProviderError, match="needs a model name"):
        resolve_model("openai-compat")
