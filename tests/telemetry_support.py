# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Helpers for telemetry tests.

OpenTelemetry's global providers can be set once per process, and the MCP SDK caches the tracer it resolves, so the
integration tests share one provider (built through the production ``observability.setup``) whose exporter forwards to a
per-test in-memory exporter. Scrubber and processor unit tests use private providers and need none of this.
"""

import json
import threading
import time

import uvicorn
from fastmcp import FastMCP
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.middleware import Middleware

from tests.stubs import free_port

# Synthetic canaries: if one appears in an exported span, metric or log field, that is a leak.
CANARY_PATIENT = "ZX-CANARY-PATIENT-7731"
CANARY_SESSION = "SESSCANARY9f3a77"
CANARY_BEARER = "Bearer sekret-token-123"
CANARY_SECRET = "sekret-token-123"


class SwitchExporter(SpanExporter):
    """Forwards to the current test's in-memory exporter, or misbehaves on request."""

    def __init__(self) -> None:
        self.target = InMemorySpanExporter()
        self.mode = "ok"  # ok | raise

    def export(self, spans):  # type: ignore[no-untyped-def]
        if self.mode == "raise":
            raise RuntimeError("exporter exploded")
        return self.target.export(spans)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def span_dump(spans) -> list[dict]:  # type: ignore[no-untyped-def]
    return [{
        "name": s.name, "kind": str(s.kind), "status": str(s.status.status_code), "description": s.status.description,
        "attributes": dict(s.attributes or {}),
        "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in s.events],
    } for s in spans]  # fmt: skip


class Recorder:
    """Pure-ASGI middleware that records the JSON-RPC method and whether `_meta` carries trace context."""

    def __init__(self, app, store: list[dict]):  # type: ignore[no-untyped-def]
        self.app, self.store = app, store

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        chunks: list[bytes] = []

        async def rcv():  # type: ignore[no-untyped-def]
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body"):
                    self._record(b"".join(chunks))
            return msg

        return await self.app(scope, rcv, send)

    def _record(self, body: bytes) -> None:
        try:
            payload = json.loads(body)
        except ValueError:
            return
        for m in payload if isinstance(payload, list) else [payload]:
            meta = (m.get("params") or {}).get("_meta") or {}
            self.store.append({"method": m.get("method"), "traceparent": "traceparent" in meta})


def serve_recording(label: str, store: list[dict]):  # type: ignore[no-untyped-def]
    """Start a stub MCP server (tool: search) recording what it receives. Returns (url, uvicorn.Server)."""
    mcp = FastMCP(label)

    @mcp.tool(annotations={"readOnlyHint": True})
    def search(query: str) -> str:
        return f"{label}:{query}"

    @mcp.tool(annotations={"readOnlyHint": True})
    def boom(query: str) -> str:
        raise ValueError(f"upstream failed for {query} {CANARY_SECRET}")

    port = free_port()
    app = mcp.http_app(stateless_http=True, middleware=[Middleware(Recorder, store=store)])
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}/mcp", server
