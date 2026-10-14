# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Validated MCP configuration (standard-config ``ai/mcp-servers.json``).

This JSON shape is an application convention of bahmni-ai, not the generic MCP client format.
It contains URLs, auth types, credential *references*, and tool policy. Never secret values.
"""

import fnmatch
import json
import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bahmni_ai.mcp.credentials import EnvCredentialResolver

SERVER_NAME = re.compile(r"^[a-z][a-z0-9-]*$")  # no underscores, so "server__tool" is unambiguous
TOOL_SEPARATOR = "__"
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    """Invalid MCP configuration. Messages never include secret values."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NoAuth(_Strict):
    type: Literal["none"]


class BearerAuth(_Strict):
    type: Literal["bearer"]
    credentialRef: str  # noqa: N815 - JSON contract


class ApiKeyHeaderAuth(_Strict):
    type: Literal["api-key-header"]
    credentialRef: str  # noqa: N815
    header: str = Field(pattern=r"^[A-Za-z0-9-]+$")


class OpenmrsSessionAuth(_Strict):
    type: Literal["openmrs-session"]


AuthConfig = Annotated[
    NoAuth | BearerAuth | ApiKeyHeaderAuth | OpenmrsSessionAuth, Field(discriminator="type")
]


class ToolOverride(_Strict):
    access: Literal["read", "write"] = "read"
    confirm: bool = False
    requiredPrivilege: str | None = None  # noqa: N815
    # Argument constraints enforced server-side, e.g. {"project": ["BAH", "OPS"]} allows only those values.
    argumentConstraints: dict[str, list[str]] = Field(default_factory=dict)  # noqa: N815

    @model_validator(mode="after")
    def _writes_confirm(self) -> "ToolOverride":
        if self.access == "write" and not self.confirm:
            raise ValueError("write tools must set confirm=true")
        return self


class ToolsPolicy(_Strict):
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    overrides: dict[str, ToolOverride] = Field(default_factory=dict)
    default: Literal["deny"] = "deny"


class ServerConfig(_Strict):
    url: str | None = None
    transport: Literal["streamable-http", "stdio"] = "streamable-http"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    # stdio only: environment variable name for the subprocess -> credentialRef
    envRefs: dict[str, str] = Field(default_factory=dict)  # noqa: N815
    auth: AuthConfig = NoAuth(type="none")
    tools: ToolsPolicy = Field(default_factory=ToolsPolicy)
    requiredPrivilege: str | None = None  # noqa: N815 - server-wide default for matched tools

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("url must be http(s)")
        return v

    @model_validator(mode="after")
    def _transport_fields(self) -> "ServerConfig":
        if self.transport == "streamable-http":
            if not self.url:
                raise ValueError("url is required for streamable-http")
            if self.command or self.args or self.envRefs:
                raise ValueError("command/args/envRefs are only valid for stdio")
        else:
            if not self.command:
                raise ValueError("command is required for stdio")
            if self.url:
                raise ValueError("url is not valid for stdio")
            if self.auth.type == "openmrs-session":
                raise ValueError("per-user auth cannot be used with a shared stdio process")
        # Writes must name the privilege that guards them (fail closed).
        for tool, ov in self.tools.overrides.items():
            if ov.access == "write" and not (ov.requiredPrivilege or self.requiredPrivilege):
                raise ValueError(f"write tool '{tool}' needs requiredPrivilege (on the tool or the server)")
        return self


class McpConfig(_Strict):
    mcpServers: dict[str, ServerConfig]  # noqa: N815

    @field_validator("mcpServers")
    @classmethod
    def _names(cls, v: dict[str, ServerConfig]) -> dict[str, ServerConfig]:
        for name in v:
            if not SERVER_NAME.match(name):
                raise ValueError(f"invalid server name '{name}' (lowercase letters, digits, '-' only)")
        return v

    @property
    def stdio_servers(self) -> list[str]:
        return [n for n, s in self.mcpServers.items() if s.transport == "stdio"]


def _expand_urls(raw: dict, resolver: EnvCredentialResolver) -> dict:  # type: ignore[type-arg]
    """Expand ``${MCP_*}`` in ``url`` fields only. Everything else is taken literally."""
    for name, server in raw.get("mcpServers", {}).items():
        url = server.get("url") if isinstance(server, dict) else None
        if isinstance(url, str):
            try:
                server["url"] = _VAR.sub(lambda m: resolver.resolve_plain(m.group(1)), url)
            except Exception as exc:
                raise ConfigError(f"server '{name}': {exc}") from exc
    return raw


def parse_config(raw: dict, resolver: EnvCredentialResolver, allow_stdio: bool = False) -> McpConfig:  # type: ignore[type-arg]
    if not isinstance(raw, dict) or not isinstance(raw.get("mcpServers"), dict):
        raise ConfigError("config must be an object with a top-level 'mcpServers' object")
    try:
        config = McpConfig.model_validate(_expand_urls(raw, resolver))
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"invalid MCP configuration: {_safe(exc)}") from exc
    if config.stdio_servers and not allow_stdio:
        raise ConfigError(f"stdio transport is disabled (servers: {', '.join(config.stdio_servers)})")
    return config


def _safe(exc: Exception) -> str:
    # pydantic includes "input_value" for failures; drop it so nothing from the file is echoed.
    from pydantic import ValidationError

    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    return exc.__class__.__name__


def load_config(path: str | Path, resolver: EnvCredentialResolver, allow_stdio: bool = False) -> McpConfig:
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"MCP configuration file not found: {p}")
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"MCP configuration is not valid JSON: {exc.msg} (line {exc.lineno})") from exc
    return parse_config(raw, resolver, allow_stdio)


def matches(patterns: list[str], tool: str) -> bool:
    return any(fnmatch.fnmatchcase(tool, p) for p in patterns)
