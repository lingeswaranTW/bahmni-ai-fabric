# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI

from bahmni_ai.agent.factory import build_agent
from bahmni_ai.agent.service import ChatService
from bahmni_ai.api import chat, health
from bahmni_ai.audit.log import LogAuditSink
from bahmni_ai.auth.privileges import PrivilegeChecker
from bahmni_ai.auth.session import SessionValidator
from bahmni_ai.logging import configure_logging, get_logger
from bahmni_ai.mcp.auth import validate_credentials
from bahmni_ai.mcp.config import load_config
from bahmni_ai.mcp.credentials import EnvCredentialResolver
from bahmni_ai.mcp.registry import McpRegistry
from bahmni_ai.settings import Settings, get_settings
from bahmni_ai.store import MemoryConfirmationStore, MemoryConversationStore


def create_app(settings: Settings | None = None, *, chat_service: ChatService | None = None) -> FastAPI:
    """Build the app. Configuration problems stop startup (fail closed); nothing half-configured serves requests."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    log = get_logger()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("bahmni-ai starting", service_path=settings.service_path)
        async with httpx.AsyncClient(timeout=settings.openmrs_timeout_seconds) as client:
            validator = SessionValidator(client, settings.openmrs_url, settings.session_cache_ttl_seconds)
            app.state.session_validator = validator
            registry: McpRegistry | None = None
            closers: list[Callable[[], Awaitable[None]]] = []
            if chat_service is not None:
                app.state.chat_service = chat_service
            else:
                allow = frozenset(x.strip() for x in settings.credential_allowlist.split(",") if x.strip())
                resolver = EnvCredentialResolver(allow)
                config = load_config(settings.mcp_config_path, resolver, settings.mcp_allow_stdio)
                await validate_credentials(config, resolver)
                registry = McpRegistry(
                    config, resolver, call_timeout_seconds=settings.mcp_call_timeout_seconds
                )
                await registry.start()
                stores = await _stores(settings, log)
                closers.append(stores.aclose)
                app.state.chat_service = ChatService(
                    build_agent(settings.ai_model, settings.ai_request_timeout_seconds),
                    registry, stores.conversations, stores.confirmations,
                    PrivilegeChecker(validator), LogAuditSink(), settings,
                )  # fmt: skip
                log.info("mcp servers configured", servers=registry.server_names)
            try:
                yield
            finally:
                for close in closers:
                    await close()
                if registry is not None:
                    await registry.close()
        log.info("bahmni-ai stopped")

    app = FastAPI(title="bahmni-ai", lifespan=lifespan)
    root = APIRouter(prefix=settings.service_path)
    root.include_router(health.router)
    root.include_router(chat.router)
    app.include_router(root)
    return app


@dataclass
class _MemoryStores:
    conversations: MemoryConversationStore
    confirmations: MemoryConfirmationStore

    async def aclose(self) -> None:
        return None


async def _stores(settings: Settings, log: Any) -> Any:
    if settings.database_url:
        from bahmni_ai.db.postgres import init_postgres_stores

        return await init_postgres_stores(settings.database_url)
    log.warning("DATABASE_URL not set: using in-memory history (lost on restart; single instance only)")
    return _MemoryStores(MemoryConversationStore(), MemoryConfirmationStore())
