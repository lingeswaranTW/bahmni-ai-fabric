# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Real streamable-HTTP MCP stub servers for tests."""

import asyncio
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers

CALLS: dict[str, int] = {}


def make_stub(label: str) -> FastMCP:
    mcp = FastMCP(label)

    @mcp.tool(annotations={"readOnlyHint": True})
    def search(query: str) -> str:
        return f"{label}:search:{query}"

    @mcp.tool(annotations={"readOnlyHint": True})
    async def whoami() -> str:
        await asyncio.sleep(0.15)
        h = get_http_headers(include={"x-openmrs-session-id", "authorization"})
        return f"{h.get('x-openmrs-session-id', 'none')}|{h.get('authorization', 'none')}"

    @mcp.tool
    def create_issue(title: str, project: str = "BAH") -> str:
        CALLS[f"{label}:create_issue"] = CALLS.get(f"{label}:create_issue", 0) + 1
        return f"{label}:created:{project}:{title}"

    @mcp.tool
    def flaky_write(title: str) -> str:
        CALLS[f"{label}:flaky_write"] = CALLS.get(f"{label}:flaky_write", 0) + 1
        raise RuntimeError("boom with Bearer sekret-token-123")

    @mcp.tool
    async def slow_write(title: str) -> str:
        CALLS[f"{label}:slow_write"] = CALLS.get(f"{label}:slow_write", 0) + 1
        await asyncio.sleep(5)
        return "late"

    @mcp.tool
    def delete_everything() -> str:
        CALLS[f"{label}:delete_everything"] = CALLS.get(f"{label}:delete_everything", 0) + 1
        return "deleted"

    return mcp


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@contextmanager
def serve(label: str) -> Iterator[str]:
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(make_stub(label).http_app(stateless_http=True), port=port, log_level="error")
    )
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        t.join(timeout=3)
