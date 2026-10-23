# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Auth strategies: how a server's credentials become request headers.

``shared``   – service credential, safe to use on a long-lived shared connection.
``per-user`` – built from the authenticated user, so the connection must be created per run.
"""

from typing import Literal, Protocol

from bahmni_ai.auth.context import UserContext
from bahmni_ai.mcp.config import (
    ApiKeyHeaderAuth,
    AuthConfig,
    BearerAuth,
    ConfigError,
    McpConfig,
    NoAuth,
    OpenmrsSessionAuth,
)
from bahmni_ai.mcp.credentials import CredentialError, CredentialResolver

SESSION_HEADER = "X-OpenMRS-Session-Id"


class AuthError(RuntimeError):
    pass


class AuthStrategy(Protocol):
    scope: Literal["shared", "per-user"]

    async def headers(self, user: UserContext | None) -> dict[str, str]: ...


class _None:
    scope: Literal["shared", "per-user"] = "shared"

    async def headers(self, user: UserContext | None) -> dict[str, str]:
        return {}


class _Bearer:
    scope: Literal["shared", "per-user"] = "shared"

    def __init__(self, ref: str, resolver: CredentialResolver):
        self._ref, self._resolver = ref, resolver

    async def headers(self, user: UserContext | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {(await self._resolver.resolve(self._ref)).get_secret_value()}"}


class _ApiKey:
    scope: Literal["shared", "per-user"] = "shared"

    def __init__(self, ref: str, header: str, resolver: CredentialResolver):
        self._ref, self._header, self._resolver = ref, header, resolver

    async def headers(self, user: UserContext | None) -> dict[str, str]:
        return {self._header: (await self._resolver.resolve(self._ref)).get_secret_value()}


class _OpenmrsSession:
    scope: Literal["shared", "per-user"] = "per-user"

    async def headers(self, user: UserContext | None) -> dict[str, str]:
        if user is None or not user.session_id:
            raise AuthError("this server requires an authenticated OpenMRS user")
        return {SESSION_HEADER: user.session_id}


def build_strategy(auth: AuthConfig, resolver: CredentialResolver) -> AuthStrategy:
    if isinstance(auth, NoAuth):
        return _None()
    if isinstance(auth, BearerAuth):
        return _Bearer(auth.credentialRef, resolver)
    if isinstance(auth, ApiKeyHeaderAuth):
        return _ApiKey(auth.credentialRef, auth.header, resolver)
    if isinstance(auth, OpenmrsSessionAuth):
        return _OpenmrsSession()
    raise AuthError(f"unsupported auth type: {auth!r}")  # pragma: no cover


def credential_refs(auth: AuthConfig) -> list[str]:
    return [auth.credentialRef] if isinstance(auth, (BearerAuth, ApiKeyHeaderAuth)) else []


async def validate_credentials(config: McpConfig, resolver: CredentialResolver) -> None:
    """Fail closed at startup: every referenced credential must resolve."""
    for name, server in config.mcpServers.items():
        for ref in [*credential_refs(server.auth), *server.envRefs.values()]:
            try:
                await resolver.resolve(ref)
            except CredentialError as exc:
                raise ConfigError(f"server '{name}': {exc}") from exc
