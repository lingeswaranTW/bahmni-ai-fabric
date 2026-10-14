# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Connects to the configured MCP servers for one agent run.

Every run gets its own connections, built from that run's user and fresh credentials, so a session or
token can never be reused across users or outlive its owner. (Pooling shared-credential connections is
a later optimisation; it is not needed for correctness.) stdio servers, when explicitly enabled, are
long-lived local processes shared by all runs.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastmcp.client.transports import StdioTransport
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset

from bahmni_ai.agent.deps import AgentDeps
from bahmni_ai.auth.context import UserContext
from bahmni_ai.logging import get_logger
from bahmni_ai.mcp.auth import AuthStrategy, build_strategy
from bahmni_ai.mcp.config import McpConfig, ServerConfig
from bahmni_ai.mcp.credentials import CredentialResolver
from bahmni_ai.mcp.policy import ServerToolset

log = get_logger("bahmni_ai.mcp")


@dataclass
class OpenedServers:
    toolsets: list[AbstractToolset[AgentDeps]] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)  # servers skipped for this run


class McpRegistry:
    def __init__(
        self,
        config: McpConfig,
        resolver: CredentialResolver,
        connect_timeout_seconds: float = 5.0,
        call_timeout_seconds: float = 30.0,
    ):
        self._config = config
        self._resolver = resolver
        self._connect_timeout = connect_timeout_seconds
        self._call_timeout = call_timeout_seconds
        self._strategies: dict[str, AuthStrategy] = {
            n: build_strategy(s.auth, resolver) for n, s in config.mcpServers.items()
        }
        self._stack = AsyncExitStack()
        self._shared: dict[str, MCPToolset[Any]] = {}

    @property
    def server_names(self) -> list[str]:
        return list(self._config.mcpServers)

    async def start(self) -> None:
        """Start long-lived stdio servers. Enter and exit run in the same task (the app lifespan)."""
        for name, cfg in self._config.mcpServers.items():
            if cfg.transport != "stdio":
                continue
            try:
                env = {k: (await self._resolver.resolve(ref)).get_secret_value() for k, ref in cfg.envRefs.items()}
                transport = StdioTransport(command=cfg.command or "", args=cfg.args, env=env)
                ts: MCPToolset[Any] = MCPToolset(transport, init_timeout=self._connect_timeout, max_retries=0)
                self._shared[name] = await self._stack.enter_async_context(ts)
            except Exception as exc:
                log.error("mcp_stdio_start_failed", server=name, error=exc.__class__.__name__)

    async def close(self) -> None:
        await self._stack.aclose()
        self._shared.clear()

    def _http(self, cfg: ServerConfig, headers: dict[str, str]) -> MCPToolset[Any]:
        return MCPToolset(
            cfg.url or "",
            headers=headers,
            init_timeout=self._connect_timeout,
            read_timeout=self._call_timeout,
            max_retries=0,  # tool failures go back to the model as results; never auto-retried
            tool_error_behavior="failed",
        )

    @asynccontextmanager
    async def open(self, user: UserContext | None) -> AsyncIterator[OpenedServers]:
        opened = OpenedServers()
        async with AsyncExitStack() as stack:
            for name, cfg in self._config.mcpServers.items():
                try:
                    if cfg.transport == "stdio":
                        inner = self._shared.get(name)
                        if inner is None:
                            raise RuntimeError("stdio server is not running")
                    else:
                        headers = await self._strategies[name].headers(user)
                        inner = await asyncio.wait_for(
                            stack.enter_async_context(self._http(cfg, headers)), self._connect_timeout + 1
                        )
                    opened.toolsets.append(ServerToolset(inner, server=name, config=cfg))
                except Exception as exc:
                    # One failing server must not take the others down. Never log headers or URLs with secrets.
                    log.warning("mcp_server_unavailable", server=name, error=exc.__class__.__name__)
                    opened.unavailable.append(name)
            yield opened
