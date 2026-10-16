"""E1 + E2: OTLP ingestion into the pinned OpenLIT/ClickHouse, and sensitive-data redaction.
usage: e1_ingest_redaction.py <content:on|off> <scrub:on|off>"""
import asyncio
import sys
import time

import lib
from lib import CANARY_PATIENT, collect, dump_spans, find_canaries

content = sys.argv[1] == "on"
scrub = sys.argv[2] == "on"
import time as _t
service = f"spike-content-{sys.argv[1]}-scrub-{sys.argv[2]}-{int(_t.time())}"
tel = lib.setup_otel(content=content, scrub=scrub, service=service)

from tests import stubs  # noqa: E402
from tests.test_chat_api import scripted  # noqa: E402


async def main():
    with stubs.serve("docs") as docs, stubs.serve("jira") as jira:
        # Turn 1: read tool, canary in the user's message and in the tool arguments.
        svc, user, audit = lib.build_service(docs, jira, scripted("docs__search", {"query": CANARY_PATIENT}))
        t1 = await collect(svc.chat(user, f"Find notes about {CANARY_PATIENT}", None))
        # Turn 2: write tool that fails with a secret inside the exception text -> confirmation -> confirm.
        svc2, user2, audit2 = lib.build_service(jira and docs, jira, scripted("jira__flaky_write", {"title": CANARY_PATIENT}))
        t2 = await collect(svc2.chat(user2, "create the thing", None))
        conf = next(e for e in t2 if e.name == "confirmation_required")
        t3 = await collect(svc2.confirm(user2, conf.data()["confirmationId"], True, {}))
        return t1, t2, t3, audit.entries + audit2.entries

t1, t2, t3, audits = asyncio.run(main())
tel.flush()
time.sleep(3)
tel.flush()
time.sleep(2)

spans = dump_spans(tel.spans.get_finished_spans())
from opentelemetry.sdk.metrics.export import MetricsData  # noqa: E402
md: MetricsData | None = tel.reader.get_metrics_data()
metrics = []
for rm in (md.resource_metrics if md else []):
    for sm in rm.scope_metrics:
        for m in sm.metrics:
            pts = [dict(p.attributes) | ({"count": getattr(p, "count", None), "sum": getattr(p, "sum", None)}
                   if hasattr(p, "sum") else {"value": getattr(p, "value", None)}) for p in m.data.data_points]
            metrics.append({"name": m.name, "unit": m.unit, "points": pts})

ch_traces = lib.clickhouse(
    f"SELECT SpanName, SpanKind, ScopeName, StatusCode, StatusMessage, SpanAttributes, `Events.Name`, "
    f"`Events.Attributes` FROM otel_traces WHERE ServiceName='{service}' ORDER BY Timestamp FORMAT JSONEachRow")
ch_metric_tables = {}
for t in ("otel_metrics_sum", "otel_metrics_histogram", "otel_metrics_gauge"):
    ch_metric_tables[t] = lib.clickhouse(
        f"SELECT MetricName, Attributes, ResourceAttributes FROM {t} WHERE ServiceName='{service}' FORMAT JSONEachRow")

audit_view = [a.__dict__ for a in audits]
result = {
    "mode": {"content": content, "scrub": scrub, "service": service},
    "event_names_turn1": [e.name for e in t1], "event_names_confirm": [e.name for e in t3],
    "inmemory_span_count": len(spans), "span_names": [s["name"] for s in spans],
    "clickhouse_span_count": len(ch_traces), "clickhouse_span_names": [r["SpanName"] for r in ch_traces],
    "metrics_inmemory": metrics,
    "clickhouse_metric_names": {t: sorted({r["MetricName"] for r in rows}) for t, rows in ch_metric_tables.items()},
    # The stub MCP servers run in this process, so their SERVER spans land here too. In production they would not;
    # report client-side (our process only) separately from the stub's server-side spans.
    "canaries_in_memory_spans_all": find_canaries(spans),
    "canaries_in_memory_spans_ours": find_canaries([s for s in spans if "SERVER" not in s["kind"]]),
    "canaries_in_memory_spans_stub_server_side": find_canaries([s for s in spans if "SERVER" in s["kind"]]),
    "canaries_in_clickhouse_spans_all": find_canaries(ch_traces),
    "canaries_in_clickhouse_spans_ours": find_canaries([r for r in ch_traces if "SERVER" not in r["SpanKind"].upper()]),
    "canaries_in_clickhouse_spans_stub_server_side": find_canaries([r for r in ch_traces if "SERVER" in r["SpanKind"].upper()]),
    "canaries_in_metrics": find_canaries([metrics, ch_metric_tables]),
    "canaries_in_audit_records": find_canaries(audit_view),
    "spans": spans, "clickhouse_traces": ch_traces,
}
lib.save(f"e1_content-{sys.argv[1]}_scrub-{sys.argv[2]}", result)
print({k: v for k, v in result.items() if k not in ("spans", "clickhouse_traces", "metrics_inmemory")})
