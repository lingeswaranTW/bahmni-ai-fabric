# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Observability: scrubbing, processor safety, bounded shutdown, propagation gate, discovery filtering, fail-safe setup.

See docs/OBSERVABILITY.md and docs/OBSERVABILITY_PHASE0_RESULT.md for why each of these exists.
"""

import asyncio
import pathlib
import re
import time

import pytest
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.instrumented import InstrumentationSettings

from bahmni_ai import observability
from bahmni_ai.mcp.config import McpConfig
from bahmni_ai.observability._otel import Runtime, SafeSpanProcessor
from bahmni_ai.observability.propagation import build_gated_propagator, propagation_scope
from bahmni_ai.observability.scrub import ScrubbingExporter
from bahmni_ai.settings import Settings
from tests.telemetry_support import (
    CANARY_BEARER,
    CANARY_PATIENT,
    CANARY_SECRET,
    CANARY_SESSION,
    SwitchExporter,
    serve_recording,
    span_dump,
)


# ---------------------------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def _shared():
    switch = SwitchExporter()
    settings = Settings(telemetry_enabled=True, telemetry_discovery_spans="errors-only")
    switch.metrics = InMemoryMetricReader()
    observability.setup(settings, span_exporter=switch, metric_reader=switch.metrics)
    return switch, observability._runtime


@pytest.fixture
def telemetry(_shared, monkeypatch):
    """The shared, production-wired provider with a fresh in-memory exporter."""
    switch, runtime = _shared
    switch.target = InMemorySpanExporter()
    switch.mode = "ok"
    monkeypatch.setattr(observability, "_runtime", runtime)

    class Handle:
        def flush(self):
            assert runtime.tracer_provider.force_flush(5000)
            return span_dump(switch.target.get_finished_spans())

    return Handle()


def private_provider(*, keep_content=False, discovery="errors-only", slow_ms=1000):
    mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(
        ScrubbingExporter(mem, keep_content=keep_content, discovery=discovery, slow_ms=slow_ms)))  # fmt: skip
    return provider, mem


def blob(spans) -> str:
    return str(spans)


# ---------------------------------------------------------------------------------------------------------------
# 1. exporter scrubbing
# ---------------------------------------------------------------------------------------------------------------
def test_scrubber_keeps_only_allowlisted_attributes():
    provider, mem = private_provider()
    with provider.get_tracer("t").start_as_current_span("chat") as span:
        span.set_attribute("gen_ai.usage.input_tokens", 5)
        span.set_attribute("gen_ai.request.model", "m")
        span.set_attribute("http.url", f"http://x/?sid={CANARY_SESSION}")
        span.set_attribute("net.peer.name", "internal-host")
        span.set_attribute("arbitrary", CANARY_PATIENT)
    (out,) = mem.get_finished_spans()
    assert dict(out.attributes) == {"gen_ai.usage.input_tokens": 5, "gen_ai.request.model": "m"}


def test_scrubber_drops_content_keys_unless_capture_is_on_and_still_redacts():
    for keep, expect_content in ((False, False), (True, True)):
        provider, mem = private_provider(keep_content=keep)
        with provider.get_tracer("t").start_as_current_span("chat") as span:
            span.set_attribute("gen_ai.input.messages", f"{CANARY_PATIENT} with {CANARY_BEARER}")
            span.set_attribute("gen_ai.usage.input_tokens", 1)
        (out,) = mem.get_finished_spans()
        text = blob(dict(out.attributes))
        assert (CANARY_PATIENT in text) is expect_content
        assert CANARY_SECRET not in text  # bearer redacted even when content is kept


def test_scrubber_removes_status_description_and_exception_text_but_keeps_type():
    provider, mem = private_provider()
    with pytest.raises(RuntimeError):
        with provider.get_tracer("t").start_as_current_span("tools/call x") as span:
            span.set_attribute("error.type", "tool_error")
            span.set_status(Status(StatusCode.ERROR, f"Error calling tool: boom with {CANARY_BEARER}"))
            try:
                raise RuntimeError(f"boom with {CANARY_BEARER}")
            except RuntimeError as exc:
                span.record_exception(exc)
                raise
    (out,) = mem.get_finished_spans()
    assert out.status.status_code == StatusCode.ERROR
    assert out.status.description is None
    assert CANARY_SECRET not in blob(out.events) + blob(out.attributes)
    assert out.events and all(e.name == "exception" for e in out.events)
    assert all(dict(e.attributes) == {"exception.type": "RuntimeError"} for e in out.events)


def test_scrubber_fails_closed_when_a_span_cannot_be_scrubbed(monkeypatch):
    provider, mem = private_provider()
    monkeypatch.setattr(ScrubbingExporter, "_clean", lambda self, span: (_ for _ in ()).throw(ValueError("bad")))
    with provider.get_tracer("t").start_as_current_span("x") as span:
        span.set_attribute("gen_ai.request.model", CANARY_PATIENT)
    assert mem.get_finished_spans() == ()  # dropped, not exported raw


async def test_agent_run_exports_no_content_with_instrumentation_content_off():
    provider, mem = private_provider()
    agent = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"answer about {CANARY_PATIENT}")])))
    agent.instrument = InstrumentationSettings(tracer_provider=provider, include_content=False, version=5)
    await agent.run(f"tell me about {CANARY_PATIENT}")
    spans = span_dump(mem.get_finished_spans())
    assert spans and CANARY_PATIENT not in blob(spans)
    assert any("gen_ai.usage.input_tokens" in s["attributes"] for s in spans)  # useful data survives


async def test_scrubber_blocks_content_even_if_instrumentation_content_is_on():
    provider, mem = private_provider(keep_content=False)
    agent = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(f"answer about {CANARY_PATIENT}")])))
    agent.instrument = InstrumentationSettings(tracer_provider=provider, include_content=True, version=5)
    await agent.run(f"tell me about {CANARY_PATIENT}")
    assert CANARY_PATIENT not in blob(span_dump(mem.get_finished_spans()))


# ---------------------------------------------------------------------------------------------------------------
# 2. span-processor safety
# ---------------------------------------------------------------------------------------------------------------
class Boom(SpanProcessor):
    def __init__(self, where: str):
        self.where = where

    def on_start(self, span, parent_context=None):
        if self.where in ("start", "both"):
            raise RuntimeError("on_start exploded")

    def on_end(self, span):
        if self.where in ("end", "both"):
            raise RuntimeError("on_end exploded")

    def shutdown(self):
        raise RuntimeError("shutdown exploded")

    def force_flush(self, timeout_millis=30000):
        raise RuntimeError("flush exploded")


@pytest.mark.parametrize("where", ["start", "end", "both"])
def test_unwrapped_processor_failure_reaches_the_caller_which_is_why_we_wrap(where):
    """Documents the SDK behaviour found in Phase 0."""
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(Boom(where))
    with pytest.raises(RuntimeError):
        with provider.get_tracer("t").start_as_current_span("x"):
            pass


@pytest.mark.parametrize("where", ["start", "end", "both"])
async def test_safe_processor_keeps_requests_working(where):
    provider = TracerProvider(shutdown_on_exit=False)
    safe = SafeSpanProcessor(Boom(where))
    provider.add_span_processor(safe)
    agent = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("ok")])))
    agent.instrument = InstrumentationSettings(tracer_provider=provider, version=5)
    result = await agent.run("hi")
    assert result.output == "ok"
    assert safe.failures > 0
    assert safe.force_flush() is False
    safe.shutdown()  # does not raise


def test_production_wiring_registers_only_safe_processors(_shared):
    _, runtime = _shared
    assert runtime.processors and all(isinstance(p, SafeSpanProcessor) for p in runtime.processors)
    registered = runtime.tracer_provider._active_span_processor._span_processors  # SDK internals, test only
    assert registered and all(isinstance(p, SafeSpanProcessor) for p in registered)


# ---------------------------------------------------------------------------------------------------------------
# 3. bounded shutdown, fail-safe setup
# ---------------------------------------------------------------------------------------------------------------
def test_shutdown_is_bounded_when_the_backend_hangs():
    class Hanging:
        def force_flush(self, *_):
            time.sleep(30)

        def shutdown(self, *_):
            time.sleep(30)

    settings = Settings(telemetry_shutdown_timeout_seconds=0.4)
    started = time.monotonic()
    Runtime(Hanging(), Hanging(), settings, []).shutdown()  # type: ignore[arg-type]
    assert time.monotonic() - started < 2.0


def test_shutdown_survives_provider_errors():
    class Broken:
        def force_flush(self, *_):
            raise RuntimeError("x")

        def shutdown(self, *_):
            raise RuntimeError("x")

    Runtime(Broken(), Broken(), Settings(), []).shutdown()  # type: ignore[arg-type]


def test_disabled_telemetry_is_a_noop(monkeypatch):
    monkeypatch.setattr(observability, "_runtime", None)
    shutdown = observability.setup(Settings(telemetry_enabled=False))
    assert shutdown() is None
    assert observability.instrumentation_for_agent() is False
    assert observability.current_trace_ids() == {}
    observability.instrument_app(object())  # no-op, must not touch the app


def test_misconfigured_telemetry_disables_itself_instead_of_stopping_the_service(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setattr(observability, "_runtime", None)
    shutdown = observability.setup(Settings(telemetry_enabled=True, telemetry_backend="otlp"))
    assert shutdown() is None
    assert observability._runtime is None
    assert observability.instrumentation_for_agent() is False


def test_agent_instrumentation_is_content_free_and_versioned(telemetry):
    settings = observability.instrumentation_for_agent()
    assert isinstance(settings, InstrumentationSettings)
    assert settings.include_content is False and settings.include_binary_content is False
    assert settings.version == 5


# ---------------------------------------------------------------------------------------------------------------
# 4. propagation gate
# ---------------------------------------------------------------------------------------------------------------
def test_gated_propagator_injects_only_inside_an_allowed_scope():
    provider = TracerProvider()
    propagator = build_gated_propagator()
    with provider.get_tracer("t").start_as_current_span("x"):
        outside, allowed, denied = {}, {}, {}
        propagator.inject(outside)
        with propagation_scope(True):
            propagator.inject(allowed)
            with propagation_scope(False):  # nested scopes restore correctly
                propagator.inject(denied)
            again = {}
            propagator.inject(again)
    assert outside == {} and denied == {}
    assert "traceparent" in allowed and "traceparent" in again
    carrier: dict = {}
    propagator.inject(carrier)
    assert carrier == {}  # scope is gone


def test_propagate_trace_defaults_to_false_and_is_validated():
    base = {"url": "http://x/mcp", "auth": {"type": "none"}}
    cfg = McpConfig.model_validate({"mcpServers": {"a": base, "b": {**base, "propagateTrace": True}}})
    assert cfg.mcpServers["a"].propagateTrace is False and cfg.mcpServers["b"].propagateTrace is True
    with pytest.raises(ValueError):
        McpConfig.model_validate({"mcpServers": {"a": {**base, "propagateTrace": "yes please"}}})


def _service(docs_url, jira_url, tool, docs_trusted, *, allow=("search",), privs=None, model=None):
    from bahmni_ai.agent.factory import build_agent
    from bahmni_ai.agent.service import ChatService
    from bahmni_ai.auth.privileges import PrivilegeChecker
    from bahmni_ai.db.store import MemoryConfirmationStore, MemoryConversationStore
    from bahmni_ai.mcp.config import parse_config
    from bahmni_ai.mcp.credentials import EnvCredentialResolver
    from bahmni_ai.mcp.registry import McpRegistry
    from tests.conftest import FakeValidator, MemoryAudit
    from tests.test_chat_api import scripted
    from tests.test_registry_policy import ALL

    def server(url, priv, trusted):
        return {
            "url": url,
            "auth": {"type": "none"},
            "tools": {"allow": list(allow)},
            "requiredPrivilege": priv,
            "propagateTrace": trusted,
            "trustToolAnnotations": True,
        }

    cfg = {
        "mcpServers": {
            "docs": server(docs_url, "Get Documents", docs_trusted),
            "jira": server(jira_url, "Get Issues", False),
        }
    }
    resolver = EnvCredentialResolver(environ={})
    registry = McpRegistry(parse_config(cfg, resolver), resolver, connect_timeout_seconds=2, call_timeout_seconds=5)
    validator = FakeValidator()
    user = validator.add("S", "alice", ALL if privs is None else privs, uuid="u-alice")
    audit = MemoryAudit()
    svc = ChatService(
        build_agent(model or scripted(tool, {"query": "x"})),
        registry,
        MemoryConversationStore(),
        MemoryConfirmationStore(),
        PrivilegeChecker(validator),
        audit,
        Settings(openmrs_url="http://o"),
    )
    return svc, user, audit


async def _turn(svc, user):
    return [e async for e in svc.chat(user, "hello", None)]


def _calls(store):
    return [r for r in store if r["method"] == "tools/call"]


async def test_trace_context_is_sent_only_to_servers_marked_trusted(telemetry):
    docs_rec, jira_rec = [], []
    docs_url, s1 = serve_recording("docs", docs_rec)
    jira_url, s2 = serve_recording("jira", jira_rec)
    try:
        svc, user, _ = _service(docs_url, jira_url, "docs__search", docs_trusted=True)
        await _turn(svc, user)
        svc, user, _ = _service(docs_url, jira_url, "jira__search", docs_trusted=True)
        await _turn(svc, user)
    finally:
        s1.should_exit = s2.should_exit = True
    assert [r["traceparent"] for r in _calls(docs_rec)] == [True]
    assert [r["traceparent"] for r in _calls(jira_rec)] == [False]
    assert not any(r["traceparent"] for r in jira_rec)  # nothing at all reaches the untrusted server


async def test_trace_context_defaults_to_off_for_every_server(telemetry):
    docs_rec, jira_rec = [], []
    docs_url, s1 = serve_recording("docs", docs_rec)
    jira_url, s2 = serve_recording("jira", jira_rec)
    try:
        svc, user, _ = _service(docs_url, jira_url, "docs__search", docs_trusted=False)
        await _turn(svc, user)
    finally:
        s1.should_exit = s2.should_exit = True
    assert docs_rec and not any(r["traceparent"] for r in docs_rec + jira_rec)


async def test_propagation_flag_does_not_bleed_between_concurrent_runs(telemetry):
    docs_rec, jira_rec = [], []
    docs_url, s1 = serve_recording("docs", docs_rec)
    jira_url, s2 = serve_recording("jira", jira_rec)
    try:
        runs = []
        for i in range(20):
            svc, user, _ = _service(docs_url, jira_url, "docs__search" if i % 2 else "jira__search", docs_trusted=True)
            runs.append(_turn(svc, user))
        await asyncio.gather(*runs)
    finally:
        s1.should_exit = s2.should_exit = True
    assert len(_calls(docs_rec)) == 10 and all(r["traceparent"] for r in _calls(docs_rec))
    assert len(_calls(jira_rec)) == 10 and not any(r["traceparent"] for r in jira_rec)


# ---------------------------------------------------------------------------------------------------------------
# 5. discovery spans: reduced, errors kept
# ---------------------------------------------------------------------------------------------------------------
def _timed_span(provider, name, *, status=StatusCode.OK, ms=1, method=None):
    tracer = provider.get_tracer("t")
    start = time.time_ns()
    span = tracer.start_span(name, start_time=start)
    if method:
        span.set_attribute("mcp.method.name", method)
    span.set_status(Status(status, "boom" if status == StatusCode.ERROR else None))
    span.end(end_time=start + ms * 1_000_000)


def test_ok_discovery_spans_are_dropped_but_errors_and_slow_ones_stay():
    provider, mem = private_provider(slow_ms=500)
    _timed_span(provider, "tools/list", method="tools/list")  # dropped
    _timed_span(provider, "MCP send server/discover")  # dropped (matched by name)
    _timed_span(provider, "MCP send initialize", method="initialize")  # dropped
    _timed_span(provider, "tools/list", status=StatusCode.ERROR, method="tools/list")  # kept: error
    _timed_span(provider, "tools/list", ms=900, method="tools/list")  # kept: slow
    _timed_span(provider, "tools/call search", method="tools/call")  # kept: not discovery
    _timed_span(provider, "execute_tool docs__search")  # kept
    kept = [(s.name, s.status.status_code) for s in mem.get_finished_spans()]
    assert [n for n, _ in kept] == ["tools/list", "tools/list", "tools/call search", "execute_tool docs__search"]
    assert kept[0][1] == StatusCode.ERROR


def test_discovery_spans_can_be_kept_with_all():
    provider, mem = private_provider(discovery="all")
    _timed_span(provider, "tools/list", method="tools/list")
    assert len(mem.get_finished_spans()) == 1


async def test_a_chat_turn_exports_tool_spans_but_no_routine_discovery_spans(telemetry):
    rec: list[dict] = []
    docs_url, s1 = serve_recording("docs", rec)
    jira_url, s2 = serve_recording("jira", [])
    try:
        svc, user, audit = _service(docs_url, jira_url, "docs__search", docs_trusted=False)
        events = await _turn(svc, user)
    finally:
        s1.should_exit = s2.should_exit = True
    spans = telemetry.flush()
    names = [s["name"] for s in spans]
    assert events[-1].name == "done" and len(audit.entries) == 1
    assert "execute_tool docs__search" in names and any(n.startswith("tools/call") for n in names)
    assert not [n for n in names if "server/discover" in n or "tools/list" in n]
    assert CANARY_SESSION not in blob(spans)


async def test_unreachable_server_is_still_visible_in_telemetry(telemetry):
    from bahmni_ai.mcp.config import parse_config
    from bahmni_ai.mcp.credentials import EnvCredentialResolver
    from bahmni_ai.mcp.registry import McpRegistry
    from tests.stubs import free_port

    resolver = EnvCredentialResolver(environ={})
    cfg = {
        "mcpServers": {
            "gone": {
                "url": f"http://127.0.0.1:{free_port()}/mcp",
                "auth": {"type": "none"},
                "tools": {"allow": ["search"]},
                "requiredPrivilege": "x",
                "trustToolAnnotations": True,
            }
        }
    }
    registry = McpRegistry(parse_config(cfg, resolver), resolver, connect_timeout_seconds=1, call_timeout_seconds=2)
    with trace.get_tracer("test").start_as_current_span("probe"):
        async with registry.open(None) as opened:
            assert opened.unavailable == ["gone"]
    spans = telemetry.flush()
    errored = [s for s in spans if s["status"] == str(StatusCode.ERROR)]
    assert [s["attributes"]["bahmni.server"] for s in errored] == ["gone"], (
        "a failing server must leave an error span even though routine discovery spans are dropped"
    )
    assert errored[0]["name"] == "bahmni_ai.mcp.unavailable" and errored[0]["description"] is None
    # the library's own discovery span for the failed attempt is status UNSET, so it is filtered like any routine one
    assert not [s for s in spans if "server/discover" in s["name"]]


# ---------------------------------------------------------------------------------------------------------------
# 6. isolation: a failing exporter never affects the service; audit is independent
# ---------------------------------------------------------------------------------------------------------------
async def test_failing_exporter_does_not_affect_chat_or_audit(telemetry, _shared):
    switch, _ = _shared
    switch.mode = "raise"
    docs_url, s1 = serve_recording("docs", [])
    jira_url, s2 = serve_recording("jira", [])
    try:
        svc, user, audit = _service(docs_url, jira_url, "docs__search", docs_trusted=False)
        events = await _turn(svc, user)
        _shared[1].tracer_provider.force_flush(3000)  # the exporter raises on the batch thread
    finally:
        s1.should_exit = s2.should_exit = True
    assert [e.name for e in events][-1] == "done" and not any(e.name == "error" for e in events)
    assert len(audit.entries) == 1 and audit.entries[0].outcome == "success"


# ---------------------------------------------------------------------------------------------------------------
# 7. HTTP path and log correlation
# ---------------------------------------------------------------------------------------------------------------
def test_fastapi_spans_carry_no_session_or_url_secrets_and_health_is_excluded(telemetry, _shared, monkeypatch):
    import httpx
    import respx
    from fastapi.testclient import TestClient

    from bahmni_ai.main import create_app
    from tests.test_chat_api import SESSION_URL, scripted, user_json

    settings = Settings(openmrs_url="http://omrs:8080")
    runtime = _shared[1]
    from bahmni_ai.agent.factory import build_agent
    from bahmni_ai.agent.service import ChatService
    from bahmni_ai.auth.privileges import PrivilegeChecker
    from bahmni_ai.db.store import MemoryConfirmationStore, MemoryConversationStore
    from tests.conftest import FakeValidator, MemoryAudit
    from tests.test_registry_policy import registry

    docs_url, s1 = serve_recording("docs", [])
    jira_url, s2 = serve_recording("jira", [])
    try:
        validator = FakeValidator()
        svc = ChatService(
            build_agent(scripted()),
            registry(docs_url, jira_url),
            MemoryConversationStore(),
            MemoryConfirmationStore(),
            PrivilegeChecker(validator),
            MemoryAudit(),
            settings,
        )
        app = create_app(settings, chat_service=svc)
        monkeypatch.setattr(observability, "_runtime", runtime)  # create_app() ran setup() with telemetry off
        observability.instrument_app(app)
        with respx.mock(assert_all_called=False) as mock, TestClient(app) as client:
            mock.route(host="127.0.0.1").pass_through()
            mock.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json("alice")))
            client.get("/bahmni-ai/health")
            resp = client.post(
                "/bahmni-ai/chat",
                json={"message": f"hi {CANARY_PATIENT}"},
                headers={"X-OpenMRS-Session-Id": CANARY_SESSION, "Cookie": f"JSESSIONID={CANARY_SESSION}"},
            )
            assert resp.status_code == 200
    finally:
        s1.should_exit = s2.should_exit = True
    spans = telemetry.flush()
    http = [s for s in spans if s["attributes"].get("http.route")]
    assert [s["attributes"]["http.route"] for s in http if "health" in s["attributes"]["http.route"]] == []
    assert any(s["attributes"].get("http.route") == "/bahmni-ai/chat" for s in http)
    assert CANARY_SESSION not in blob(spans) and CANARY_PATIENT not in blob(spans)


def test_trace_ids_for_log_correlation(telemetry):
    from bahmni_ai.logging import _trace_processor

    assert _trace_processor(None, "info", {"event": "x"}) == {"event": "x"}
    with trace.get_tracer("t").start_as_current_span("s"):
        out = _trace_processor(None, "info", {"event": "x"})
    assert re.fullmatch(r"[0-9a-f]{32}", out["trace_id"]) and re.fullmatch(r"[0-9a-f]{16}", out["span_id"])


# ---------------------------------------------------------------------------------------------------------------
# 8. layering: business modules only know the facade
# ---------------------------------------------------------------------------------------------------------------
def test_only_the_observability_package_imports_opentelemetry_or_openlit():
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "bahmni_ai"
    offenders = []
    for path in root.rglob("*.py"):
        if "observability" in path.relative_to(root).parts:
            continue
        text = path.read_text()
        if re.search(r"^\s*(from|import)\s+(opentelemetry|openlit)\b", text, re.MULTILINE):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


# ---------------------------------------------------------------------------------------------------------------
# 9. session grouping (gen_ai.conversation.id)
# ---------------------------------------------------------------------------------------------------------------
def test_session_id_modes(telemetry, _shared, monkeypatch):
    runtime = _shared[1]

    def with_settings(stored=None, **kw):
        monkeypatch.setattr(runtime, "settings", Settings(**kw))
        return observability.session_id("c66a150404984052a5eb843cb642aaba", stored)

    anon = with_settings()  # default: anonymous, unrelated to our conversation id
    assert anon and "c66a1504" not in anon and anon != with_settings()  # new conversation -> new random id
    assert with_settings(stored="abc") == "abc"  # later turns reuse the id kept in the conversation history
    assert with_settings(telemetry_session_id="raw") == "c66a150404984052a5eb843cb642aaba"
    hmac_without_key = with_settings(telemetry_session_id="hmac")  # no key: anonymous fallback, never the raw id
    assert hmac_without_key and "c66a1504" not in hmac_without_key
    a = with_settings(telemetry_session_id="hmac", telemetry_session_key="k1")
    assert a == with_settings(telemetry_session_id="hmac", telemetry_session_key="k1")  # stable
    assert a != with_settings(telemetry_session_id="hmac", telemetry_session_key="k2")
    assert a and "c66a1504" not in a and len(a) == 32


def test_session_id_is_none_when_telemetry_is_off(monkeypatch):
    monkeypatch.setattr(observability, "_runtime", None)
    assert observability.session_id("abc") is None


def _conversation_ids(spans):
    return [s["attributes"].get("gen_ai.conversation.id") for s in spans if s["name"].startswith("invoke_agent")]


async def test_turns_of_one_conversation_share_a_session_when_enabled(telemetry, _shared, monkeypatch):
    monkeypatch.setattr(_shared[1], "settings", Settings(telemetry_session_id="raw"))
    docs_url, s1 = serve_recording("docs", [])
    jira_url, s2 = serve_recording("jira", [])
    try:
        svc, user, _ = _service(docs_url, jira_url, "docs__search", docs_trusted=False)
        for text, cid in (("one", "conv-A"), ("two", "conv-A"), ("three", "conv-B")):
            [e async for e in svc.chat(user, text, cid)]
    finally:
        s1.should_exit = s2.should_exit = True
    ids = _conversation_ids(telemetry.flush())
    assert sorted(ids) == ["conv-A", "conv-A", "conv-B"]


async def test_turns_of_a_conversation_share_a_session_id_even_with_session_id_off(telemetry, _shared):
    """Pydantic AI carries its own (random) conversation id through the stored history, so a conversation is grouped
    in the backend without sending our conversation id. TELEMETRY_SESSION_ID only chooses what that value is."""
    docs_url, s1 = serve_recording("docs", [])
    jira_url, s2 = serve_recording("jira", [])
    try:
        svc, user, _ = _service(docs_url, jira_url, "docs__search", docs_trusted=False)
        for text in ("one", "two"):
            [e async for e in svc.chat(user, text, "conv-A")]
        [e async for e in svc.chat(user, "other", "conv-B")]
    finally:
        s1.should_exit = s2.should_exit = True
    ids = _conversation_ids(telemetry.flush())
    assert len(ids) == 3 and len(set(ids)) == 2 and "conv-A" not in ids and "conv-B" not in ids


# ---------------------------------------------------------------------------------------------------------------
# 10. end-to-end audit (docs/OBSERVABILITY_AUDIT.md): trace tree, sessions, tool failures, LLM errors
# ---------------------------------------------------------------------------------------------------------------
def _post_chats(monkeypatch, _shared, svc, requests):
    """POST each (message, conversationId) to the real FastAPI app (instrumented) and return the SSE event names."""
    import httpx
    import respx
    from fastapi.testclient import TestClient

    from bahmni_ai.main import create_app
    from tests.test_chat_api import SESSION_URL, user_json

    settings = Settings(openmrs_url="http://omrs:8080")
    app = create_app(settings, chat_service=svc)
    monkeypatch.setattr(observability, "_runtime", _shared[1])  # create_app() ran setup() with telemetry off
    observability.instrument_app(app)
    out = []
    with respx.mock(assert_all_called=False) as mock, TestClient(app) as client:
        mock.route(host="127.0.0.1").pass_through()
        mock.get(SESSION_URL).mock(return_value=httpx.Response(200, json=user_json("alice")))
        for message, cid in requests:
            body = {"message": message, **({"conversationId": cid} if cid else {})}
            resp = client.post("/bahmni-ai/chat", json=body, headers={"X-OpenMRS-Session-Id": "S"})
            assert resp.status_code == 200
            out.append(re.findall(r"^event: (\w+)", resp.text, re.M))
    return out


def _finished(telemetry, _shared):
    telemetry.flush()
    return list(_shared[0].target.get_finished_spans())


def _by_trace(spans):
    traces: dict[int, list] = {}
    for s in spans:
        traces.setdefault(s.context.trace_id, []).append(s)
    return traces


def _ancestors(span, by_id):
    while span.parent is not None and span.parent.span_id in by_id:
        span = by_id[span.parent.span_id]
        yield span


@pytest.fixture
def stub_servers():
    docs_url, s1 = serve_recording("docs", [])
    jira_url, s2 = serve_recording("jira", [])
    yield docs_url, jira_url
    s1.should_exit = s2.should_exit = True


def test_each_turn_is_one_connected_trace_with_nested_llm_and_tool_spans(telemetry, _shared, monkeypatch, stub_servers):
    svc, _, _ = _service(*stub_servers, "docs__search", docs_trusted=False)
    _post_chats(monkeypatch, _shared, svc, [("a1", "conv-A"), ("a2", "conv-A"), ("b1", "conv-B")])
    spans = _finished(telemetry, _shared)
    assert len({s.context.span_id for s in spans}) == len(spans)  # no span exported twice
    all_traces = _by_trace(spans)
    # The stub MCP server runs in this process; with propagateTrace off its server span must NOT join our trace.
    stub = [t for t in all_traces.values() if t[0].kind == trace.SpanKind.SERVER and t[0].name.startswith("tools/call")]
    assert len(stub) == 2 and all(len(t) == 1 for t in stub)
    traces = {k: v for k, v in all_traces.items() if v not in stub}
    assert len(traces) == 3  # one trace per request: turns are not merged into one long trace
    for members in traces.values():
        by_id = {s.context.span_id: s for s in members}
        roots = [s for s in members if s.parent is None]
        assert len(roots) == 1 and roots[0].attributes["http.route"] == "/bahmni-ai/chat"
        assert all(s.parent is None or s.parent.span_id in by_id for s in members)  # no orphans
        names = [s.name for s in members]
        assert sum(n.startswith("invoke_agent") for n in names) == 1  # no duplicate agent span
        agent = next(s for s in members if s.name.startswith("invoke_agent"))
        assert agent.parent.span_id == roots[0].context.span_id
        used_tool = "execute_tool docs__search" in names  # the scripted model skips the tool once history has a result
        assert names.count("execute_tool docs__search") == (1 if used_tool else 0)
        assert sum(n.startswith("chat ") for n in names) == (2 if used_tool else 1)
        if used_tool:
            tool = next(s for s in members if s.name.startswith("execute_tool"))
            assert tool.parent.span_id == agent.context.span_id
            mcp = [s for s in members if s.name.startswith("tools/call")]
            assert mcp and all(tool in list(_ancestors(s, by_id)) for s in mcp)  # MCP call nests under the tool span
        for llm in (s for s in members if s.name.startswith("chat ")):
            assert llm.parent.span_id == agent.context.span_id
            assert llm.attributes["gen_ai.request.model"] and llm.attributes["gen_ai.usage.input_tokens"] > 0
            assert llm.attributes["gen_ai.usage.output_tokens"] > 0 and llm.attributes["gen_ai.provider.name"]


def test_every_turn_root_span_carries_the_conversation_session(telemetry, _shared, monkeypatch, stub_servers):
    svc, _, _ = _service(*stub_servers, "docs__search", docs_trusted=False)
    _post_chats(monkeypatch, _shared, svc, [("a1", "conv-A"), ("b1", "conv-B"), ("a2", "conv-A")])
    traces = _by_trace(_finished(telemetry, _shared)).values()
    roots = [r for t in traces for r in t if r.parent is None and "http.route" in r.attributes]
    sessions = [r.attributes.get("session.id") for r in sorted(roots, key=lambda s: s.start_time)]
    assert all(sessions), "root span has no session.id"
    assert sessions[0] == sessions[2] != sessions[1]  # same conversation -> same session; other conversation -> other
    assert "conv-A" not in str(sessions) and "conv-B" not in str(sessions)  # anonymous by default
    for t in (t for t in traces if any("http.route" in s.attributes for s in t)):  # (skip the stub server's traces)
        root = next(s for s in t if s.parent is None)
        ids = {s.attributes.get("gen_ai.conversation.id") for s in t if s.name.startswith(("invoke_agent", "chat "))}
        assert ids == {root.attributes["session.id"]} == {root.attributes["gen_ai.conversation.id"]}


def test_session_id_follows_the_conversation_when_raw_is_enabled(telemetry, _shared, monkeypatch, stub_servers):
    monkeypatch.setattr(_shared[1], "settings", Settings(telemetry_session_id="raw"))
    svc, _, _ = _service(*stub_servers, "docs__search", docs_trusted=False)
    _post_chats(monkeypatch, _shared, svc, [("a1", "conv-A"), ("a2", "conv-A")])
    roots = [s for s in _finished(telemetry, _shared) if s.parent is None and "http.route" in s.attributes]
    assert [r.attributes["session.id"] for r in roots] == ["conv-A", "conv-A"]


def test_a_failing_tool_marks_its_span_as_error_without_leaking_the_error_text(
    telemetry, _shared, monkeypatch, stub_servers
):
    svc, _, audit = _service(*stub_servers, "docs__boom", docs_trusted=False, allow=("search", "boom"))
    _post_chats(monkeypatch, _shared, svc, [("go", "conv-A")])
    spans = _finished(telemetry, _shared)
    (tool,) = [s for s in spans if s.name == "execute_tool docs__boom"]
    assert tool.status.status_code == StatusCode.ERROR
    assert tool.attributes["bahmni.tool.outcome"] == "error" and tool.attributes["bahmni.tool.decision"] == "allowed"
    assert audit.entries[-1].outcome == "error"
    assert CANARY_SECRET not in blob(span_dump(spans))


def test_a_denied_tool_is_labelled_but_is_not_a_failure(telemetry, _shared, monkeypatch, stub_servers):
    privs = ("Use AI Chatbot",)  # lacks "Get Documents"
    svc, _, _ = _service(*stub_servers, "docs__search", docs_trusted=False, privs=privs)
    _post_chats(monkeypatch, _shared, svc, [("go", "conv-A")])
    (tool,) = [s for s in _finished(telemetry, _shared) if s.name == "execute_tool docs__search"]
    assert tool.attributes["bahmni.tool.decision"] == "denied"
    assert tool.status.status_code != StatusCode.ERROR


def test_tool_calls_are_counted_by_outcome(telemetry, _shared, monkeypatch, stub_servers):
    svc, _, _ = _service(*stub_servers, "docs__boom", docs_trusted=False, allow=("search", "boom"))
    _post_chats(monkeypatch, _shared, svc, [("go", "conv-A")])
    _finished(telemetry, _shared)
    data = _shared[0].metrics.get_metrics_data()
    points = [
        (dict(p.attributes), p.value)
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "bahmni_ai.tool.calls"
        for p in m.data.data_points
    ]
    assert any(a.get("bahmni.outcome") == "error" and a.get("bahmni.server") == "docs" for a, _ in points)
    assert CANARY_SECRET not in str(points)


def test_provider_error_marks_the_llm_span_and_hides_the_provider_text(telemetry, _shared, monkeypatch, stub_servers):
    from pydantic_ai.exceptions import ModelHTTPError

    def failing(messages, info):
        raise ModelHTTPError(503, "gpt-oss:20b", {"error": f"overloaded {CANARY_SECRET}"})

    async def failing_stream(messages, info):
        failing(messages, info)
        yield "unreachable"

    model = FunctionModel(failing, stream_function=failing_stream)
    svc, _, _ = _service(*stub_servers, None, docs_trusted=False, model=model)
    events = _post_chats(monkeypatch, _shared, svc, [("go", "conv-A")])
    assert "error" in events[0] and "done" not in events[0]
    spans = _finished(telemetry, _shared)
    root = next(s for s in spans if s.parent is None)
    agent = next(s for s in spans if s.name.startswith("invoke_agent"))
    # Pydantic AI leaves the `chat` span UNSET on a provider failure, so the failure must show on the others.
    assert agent.status.status_code == StatusCode.ERROR and [e.name for e in agent.events] == ["exception"]
    assert root.status.status_code == StatusCode.ERROR and root.attributes["bahmni.error.code"] == "provider_error"
    assert CANARY_SECRET not in blob(span_dump(spans))


async def test_prompt_from_the_hub_is_not_exported_in_spans():
    provider, mem = private_provider()
    hub_prompt = f"Operator rule mentioning {CANARY_PATIENT}"
    agent = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("ok")])), instructions=lambda: hub_prompt)
    agent.instrument = InstrumentationSettings(tracer_provider=provider, include_content=False, version=5)
    await agent.run("hi")
    spans = span_dump(mem.get_finished_spans())
    assert spans and CANARY_PATIENT not in blob(spans)
