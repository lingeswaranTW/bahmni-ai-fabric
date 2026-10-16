"""Phase 0 spike helpers. Spike code only: the application under src/ is imported, never modified.
Synthetic data only: every 'patient' and 'secret' below is a made-up canary string."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

# Synthetic canaries. If one shows up in a span, metric or log line, that is a leak.
CANARY_PATIENT = "ZX-CANARY-PATIENT-7731"      # stands for patient-identifying free text
CANARY_SESSION = "SESSCANARY9f3a77"             # stands for a JSESSIONID
CANARY_TOKEN = "docs-secret"                    # the docs MCP bearer token used by tests/test_registry_policy.py
CANARY_EXC = "sekret-token-123"                 # appears inside the stub's exception text
ALL_CANARIES = (CANARY_PATIENT, CANARY_SESSION, CANARY_TOKEN, CANARY_EXC, "jira-secret")

OTLP = os.environ.get("SPIKE_OTLP", "http://127.0.0.1:4318")


def env_file_value(key: str) -> str:
    for line in (HERE / "openlit" / ".env.local").read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    raise KeyError(key)


def clickhouse(query: str) -> list[dict]:
    out = subprocess.run(
        ["docker", "--context", "std", "exec", "openlit-spike-clickhouse-1", "clickhouse-client", "--user", "openlit",
         "--password", env_file_value("OPENLIT_DB_PASSWORD"), "--database", "openlit", "--query", query,
         "--format", "JSONEachRow"],
        capture_output=True, text=True, check=True).stdout
    return [json.loads(x) for x in out.splitlines() if x.strip()]


def save(name: str, data: object) -> None:
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(data, indent=2, default=str))


# ---------- OpenTelemetry wiring (what the future observability.setup() would do) ----------

class Telemetry:
    def __init__(self, tp, mp, spans, metrics_reader):
        self.tp, self.mp, self.spans, self.reader = tp, mp, spans, metrics_reader

    def flush(self) -> None:
        self.tp.force_flush(5000)
        self.mp.force_flush(5000)


def setup_otel(endpoint: str | None = OTLP, *, content: bool = False, scrub: bool = False,
               service: str = "bahmni-ai-spike", extra_processors=(), export_timeout_s: float = 10) -> Telemetry:
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader, PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    res = Resource.create({"service.name": service})
    tp = TracerProvider(resource=res)
    mem = InMemorySpanExporter()
    wrap = (lambda e: ScrubbingExporter(e)) if scrub else (lambda e: e)
    tp.add_span_processor(SimpleSpanProcessor(wrap(mem)))
    for p in extra_processors:
        tp.add_span_processor(p)
    readers = []
    reader = InMemoryMetricReader()
    readers.append(reader)
    if endpoint:
        tp.add_span_processor(BatchSpanProcessor(
            wrap(OTLPSpanExporter(endpoint=endpoint + "/v1/traces", timeout=export_timeout_s)),
            schedule_delay_millis=500))
        readers.append(PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint + "/v1/metrics", timeout=export_timeout_s),
            export_interval_millis=1000))
    mp = MeterProvider(resource=res, metric_readers=readers)
    trace.set_tracer_provider(tp)
    metrics.set_meter_provider(mp)
    Agent.instrument_all(InstrumentationSettings(
        tracer_provider=tp, meter_provider=mp, include_content=content, include_binary_content=False))
    return Telemetry(tp, mp, mem, reader)


ALLOWED_KEYS = ("gen_ai.", "bahmni.", "mcp.method.name", "rpc.", "http.request.method", "http.route",
                "http.response.status_code", "error.type", "logfire.")
DROPPED_EVENT_ATTRS = ("exception.message", "exception.stacktrace")


class ScrubbingExporter:
    """Prototype of the allowlist exporter wrapper from docs/OBSERVABILITY.md section 5, item 2."""

    def __init__(self, inner):
        self.inner = inner

    def export(self, spans):
        from opentelemetry.sdk.trace import Event, ReadableSpan

        clean = []
        for s in spans:
            attrs = {k: v for k, v in (s.attributes or {}).items()
                     if any(k.startswith(a) for a in ALLOWED_KEYS)
                     and k not in ("gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
                                   "tool_arguments", "tool_response", "gen_ai.tool.call.arguments",
                                   "gen_ai.tool.call.result", "final_result", "pydantic_ai.all_messages")}
            events = [Event(e.name, {k: v for k, v in (e.attributes or {}).items() if k not in DROPPED_EVENT_ATTRS},
                            e.timestamp) for e in s.events if e.name == "exception" or not e.name.startswith("gen_ai.")]
            clean.append(ReadableSpan(
                name=s.name, context=s.context, parent=s.parent, resource=s.resource, attributes=attrs, events=events,
                links=s.links, kind=s.kind, instrumentation_scope=s.instrumentation_scope,
                status=type(s.status)(s.status.status_code), start_time=s.start_time, end_time=s.end_time))
        return self.inner.export(clean)

    def shutdown(self):
        return self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000):
        return self.inner.force_flush(timeout_millis)


def dump_spans(spans) -> list[dict]:
    out = []
    for s in spans:
        out.append({
            "name": s.name, "kind": str(s.kind), "trace": format(s.context.trace_id, "032x"),
            "span": format(s.context.span_id, "016x"),
            "parent": format(s.parent.span_id, "016x") if s.parent else None,
            "scope": s.instrumentation_scope.name if s.instrumentation_scope else None,
            "status": str(s.status.status_code), "status_description": s.status.description, "attributes": dict(s.attributes or {}),
            "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in s.events],
            "resource": dict(s.resource.attributes),
        })
    return out


def find_canaries(blob: object) -> list[str]:
    text = json.dumps(blob, default=str)
    return [c for c in ALL_CANARIES if c in text]


# ---------- the real application pieces, wired like tests/conftest.py ----------

def build_service(docs_url: str, jira_url: str, model, validator=None, audit=None):
    from bahmni_ai.agent.factory import build_agent
    from bahmni_ai.agent.service import ChatService
    from bahmni_ai.auth.privileges import PrivilegeChecker
    from bahmni_ai.settings import Settings
    from bahmni_ai.store import MemoryConfirmationStore, MemoryConversationStore
    from tests.conftest import FakeValidator, MemoryAudit
    from tests.test_registry_policy import ALL, registry

    validator = validator or FakeValidator()
    audit = audit or MemoryAudit()
    user = validator.add(CANARY_SESSION, "alice", ALL, uuid="u-alice")
    settings = Settings(openmrs_url="http://omrs:8080")
    svc = ChatService(build_agent(model), registry(docs_url, jira_url), MemoryConversationStore(),
                      MemoryConfirmationStore(), PrivilegeChecker(validator), audit, settings)
    return svc, user, audit


async def collect(gen) -> list:
    return [ev async for ev in gen]
