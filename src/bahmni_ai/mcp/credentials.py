# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Credential resolution. Configuration holds references only; values come from here."""

import os
import re
from typing import Protocol

from pydantic import SecretStr

_REF = re.compile(r"^[A-Z][A-Z0-9_]*$")
ENV_PREFIX = "MCP_"


class CredentialError(RuntimeError):
    """Raised when a credential reference is invalid, not allowed, or has no value.

    Messages name the reference only, never a value.
    """


class CredentialResolver(Protocol):
    async def resolve(self, ref: str) -> SecretStr: ...


def _allowed(ref: str, allowlist: frozenset[str]) -> bool:
    return bool(_REF.match(ref)) and (ref.startswith(ENV_PREFIX) or ref in allowlist)


class EnvCredentialResolver:
    """Reads credentials from environment variables.

    Only names starting with ``MCP_`` or listed in the explicit allowlist are readable, so a config
    file can never reference ``DATABASE_URL`` or a model-provider key. Fails closed.
    A different source (e.g. AWS Secrets Manager) can implement the same protocol.
    """

    def __init__(self, allowlist: frozenset[str] = frozenset(), environ: dict[str, str] | None = None):
        self._allowlist = allowlist
        self._environ = os.environ if environ is None else environ

    async def resolve(self, ref: str) -> SecretStr:
        if not _allowed(ref, self._allowlist):
            raise CredentialError(f"Credential reference '{ref}' is not allowed (must start with {ENV_PREFIX})")
        value = self._environ.get(ref, "")
        if not value:
            raise CredentialError(f"Credential '{ref}' is not set")
        return SecretStr(value)

    def resolve_plain(self, name: str) -> str:
        """Non-secret settings (such as MCP server URLs) used for ${VAR} expansion in config."""
        if not _allowed(name, self._allowlist):
            raise CredentialError(f"Variable '{name}' is not allowed in configuration (must start with {ENV_PREFIX})")
        value = self._environ.get(name, "")
        if not value:
            raise CredentialError(f"Variable '{name}' referenced by configuration is not set")
        return value
