"""E3: does the MCP stack send W3C trace context to MCP servers, can it be switched off, and does the per-server gate
from docs/OBSERVABILITY.md 5.1 work?  usage: e3_propagation.py <baseline|gate|fastmcp-off|env-none>"""
import asyncio
import contextvars
import json
import os
import sys

scenario = sys.argv[1]
if scenario == "env-none":
    os.environ["OTEL_PROPAGATORS"] = "none"

import lib  # noqa: E402

tel = lib.setup_otel(endpoint=None, content=False, service="spike-propagation")

from fastmcp import FastMCP  # noqa: E402
from opentelemetry import propagate  # noqa: E402
from opentelemetry.propagators.textmap import DefaultSetter, TextMapPropagator  # noqa: E402
from pydantic_ai.toolsets import WrapperToolset  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402

import threading, time, uvicorn  # noqa: E402,E401

ALLOW: contextvars.ContextVar[bool] = contextvars.ContextVar("propagate_allowed", default=False)


class GatedPropagator(TextMapPropagator):
    """Wraps the process-wide propagator: inject only when the current call's server is trusted."""

    def __init__(self, inner):
        self.inner = inner

    def extract(self, carrier, context=None, getter=None):
        return self.inner.extract(carrier, context) if getter is None else self.inner.extract(carrier, context, getter)

    def inject(self, carrier, context=None, setter=DefaultSetter()):
        if ALLOW.get():
            self.inner.inject(carrier, context, setter)

    @property
    def fields(self):
        return self.inner.fields


class Recorder:
    """Pure-ASGI middleware: records JSON-RPC method, _meta trace keys and the HTTP traceparent header."""

    def __init__(self, app, store):
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        chunks = []

        async def rcv():
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body"):
                    self._record(scope, b"".join(chunks))
            return msg

        return await self.app(scope, rcv, send)

    def _record(self, scope, body):
        try:
            payload = json.loads(body)
        except Exception:
            return
        for m in payload if isinstance(payload, list) else [payload]:
            meta = (m.get("params") or {}).get("_meta") or {}
            hdrs = dict(scope["headers"])
            self.store.append({"method": m.get("method"),
                               "meta_traceparent": "traceparent" in meta, "meta_tracestate": "tracestate" in meta,
                               "http_traceparent": b"traceparent" in hdrs})


def serve(label, store):
    mcp = FastMCP(label)

    @mcp.tool(annotations={"readOnlyHint": True})
    def search(query: str) -> str:
        return f"{label}:{query}"

    app = mcp.http_app(stateless_http=True, middleware=[Middleware(Recorder, store=store)])
    port = lib_free_port()
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}/mcp", server


def lib_free_port():
    from tests.stubs import free_port
    return free_port()


class GateToolset(WrapperToolset):
    """Spike stand-in for the check inside ServerToolset: set the flag for this server around discovery and calls."""

    def __init__(self, wrapped, trusted):
        super().__init__(wrapped)
        self.trusted = trusted

    async def get_tools(self, ctx):
        tok = ALLOW.set(self.trusted)
        try:
            return await super().get_tools(ctx)
        finally:
            ALLOW.reset(tok)

    async def call_tool(self, name, tool_args, ctx, tool):
        tok = ALLOW.set(self.trusted)
        try:
            return await super().call_tool(name, tool_args, ctx, tool)
        finally:
            ALLOW.reset(tok)


async def run_turn(docs_url, jira_url, tool, trusted_servers, gate):
    from bahmni_ai.agent.factory import build_agent
    from bahmni_ai.agent.service import ChatService
    from bahmni_ai.auth.privileges import PrivilegeChecker
    from bahmni_ai.settings import Settings
    from bahmni_ai.store import MemoryConfirmationStore, MemoryConversationStore
    from tests.conftest import FakeValidator, MemoryAudit
    from tests.test_chat_api import scripted
    from tests.test_registry_policy import ALL, registry

    validator = FakeValidator()
    user = validator.add("S", "alice", ALL, uuid="u-a")
    reg = registry(docs_url, jira_url)
    if gate:  # wrap each opened ServerToolset the way policy.py / registry.py would
        original_open = reg.open
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def gated_open(u):
            async with original_open(u) as opened:
                opened.toolsets = [GateToolset(ts, ts.server in trusted_servers) for ts in opened.toolsets]
                yield opened
        reg.open = gated_open
    svc = ChatService(build_agent(scripted(tool, {"query": "x"})), reg, MemoryConversationStore(),
                      MemoryConfirmationStore(), PrivilegeChecker(validator), MemoryAudit(), Settings(openmrs_url="http://o"))
    return await lib.collect(svc.chat(user, "hello", None))


def summarize(store):
    by = {}
    for r in store:
        k = r["method"]
        d = by.setdefault(k, {"requests": 0, "with_meta_traceparent": 0, "with_http_traceparent": 0})
        d["requests"] += 1
        d["with_meta_traceparent"] += r["meta_traceparent"]
        d["with_http_traceparent"] += r["http_traceparent"]
    return by


async def main():
    docs_rec, jira_rec = [], []
    docs_url, s1 = serve("docs", docs_rec)
    jira_url, s2 = serve("jira", jira_rec)
    result = {"scenario": scenario, "propagators_env": os.environ.get("OTEL_PROPAGATORS")}
    if scenario == "gate":
        propagate.set_global_textmap(GatedPropagator(propagate.get_global_textmap()))
    if scenario == "fastmcp-off":
        import fastmcp
        fastmcp.settings.telemetry_mode = "off"
    # (a)/(b): one turn per server
    await run_turn(docs_url, jira_url, "docs__search", {"docs"}, scenario == "gate")
    await run_turn(docs_url, jira_url, "jira__search", {"docs"}, scenario == "gate")
    result["docs(trusted)"] = summarize(docs_rec)
    result["jira(untrusted)"] = summarize(jira_rec)
    if scenario == "gate":  # (d) concurrency: no flag bleed between simultaneous runs
        docs_rec.clear(), jira_rec.clear()
        await asyncio.gather(*[run_turn(docs_url, jira_url, "docs__search" if i % 2 else "jira__search", {"docs"}, True)
                               for i in range(20)])
        result["concurrent_20_runs"] = {"docs(trusted)": summarize(docs_rec), "jira(untrusted)": summarize(jira_rec)}
    s1.should_exit = s2.should_exit = True
    return result


res = asyncio.run(main())
lib.save(f"e3_{scenario}", res)
print(json.dumps(res, indent=1))
