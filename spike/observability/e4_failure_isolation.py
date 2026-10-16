"""E4: failure isolation. usage: e4_failure_isolation.py <none|ok|down|hang|raising> [turns]
Measures chat-turn latency, errors, audit completeness, memory and shutdown time when the telemetry backend is
absent, healthy, refusing connections, hanging, or when a span processor raises."""
import asyncio
import json
import resource
import socket
import statistics
import sys
import threading
import time

mode = sys.argv[1]
turns = int(sys.argv[2]) if len(sys.argv) > 2 else 40

import lib  # noqa: E402

hang_socks = []
endpoint = None
if mode == "ok":
    endpoint = lib.OTLP
elif mode == "down":
    endpoint = "http://127.0.0.1:4399"
elif mode == "hang":
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(50)
    endpoint = f"http://127.0.0.1:{srv.getsockname()[1]}"

    def accept():
        while True:
            c, _ = srv.accept(); hang_socks.append(c)  # accept, read nothing, answer nothing
    threading.Thread(target=accept, daemon=True).start()

extra = ()
if mode in ("raising", "raising-start", "raising-end"):
    from opentelemetry.sdk.trace import SpanProcessor

    class Boom(SpanProcessor):
        def on_start(self, span, parent_context=None):
            if mode != "raising-end": raise RuntimeError("telemetry on_start exploded")
        def on_end(self, span):
            if mode != "raising-start": raise RuntimeError("telemetry on_end exploded")
    extra = (Boom(),)

if mode == "raising-exporter":
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

    class BadExporter(SpanExporter):
        def export(self, spans): raise RuntimeError("exporter exploded")
        def shutdown(self): pass
    extra = (BatchSpanProcessor(BadExporter(), schedule_delay_millis=200),)

tel = None
if mode != "none":
    tel = lib.setup_otel(endpoint=endpoint, content=False, service=f"spike-failure-{mode}", extra_processors=extra,
                          export_timeout_s=float(__import__("os").environ.get("SPIKE_EXPORT_TIMEOUT", "10")))

from tests import stubs  # noqa: E402
from tests.test_chat_api import scripted  # noqa: E402


async def main():
    with stubs.serve("docs") as docs, stubs.serve("jira") as jira:
        svc, user, audit = lib.build_service(docs, jira, scripted("docs__search", {"query": "synthetic"}))
        lat, errors, texts, seen_err = [], 0, 0, []
        for _ in range(turns):
            t0 = time.perf_counter()
            evs = await lib.collect(svc.chat(user, "hello", None))
            lat.append((time.perf_counter() - t0) * 1000)
            errors += sum(e.name == "error" for e in evs)
            if errors and not seen_err:
                seen_err.append([e.data() for e in evs if e.name == "error"])
            texts += any(e.name == "text" for e in evs) and evs[-1].name == "done"
        return lat, errors, texts, audit.entries, seen_err


rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
lat, errors, ok_turns, audits, seen_err = asyncio.run(main())
t0 = time.perf_counter()
shutdown_s = None
if tel:
    tel.tp.shutdown()
    shutdown_s = time.perf_counter() - t0
rss1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
q = statistics.quantiles(lat, n=20)
res = {"mode": mode, "turns": turns, "ok_turns": ok_turns, "error_events": errors, "first_error": seen_err[:1],
       "audit_records": len(audits), "audit_expected": turns,  # one allowed docs__search per turn
       "latency_ms": {"median": round(statistics.median(lat), 1), "p95": round(q[18], 1), "max": round(max(lat), 1)},
       "shutdown_seconds": None if shutdown_s is None else round(shutdown_s, 2),
       "rss_mb_before_after": [round(rss0 / 1e6, 1), round(rss1 / 1e6, 1)]}
lib.save(f"e4_{mode}" + ("_timeout2" if __import__("os").environ.get("SPIKE_EXPORT_TIMEOUT") else ""), res)
print(json.dumps(res))
