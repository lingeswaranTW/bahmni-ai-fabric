# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

import json

import pytest

from bahmni_ai.auth.context import UserContext
from bahmni_ai.mcp.auth import AuthError, build_strategy, validate_credentials
from bahmni_ai.mcp.config import ConfigError, load_config, parse_config
from bahmni_ai.mcp.credentials import CredentialError, EnvCredentialResolver

ENV = {"MCP_DOCS_URL": "https://docs.internal/mcp", "MCP_DOCS_TOKEN": "s3cr3t-value", "DATABASE_URL": "postgres://x"}


def resolver(**extra):
    return EnvCredentialResolver(environ={**ENV, **extra})


def good():
    return {
        "mcpServers": {
            "docs": {
                "url": "${MCP_DOCS_URL}",
                "auth": {"type": "bearer", "credentialRef": "MCP_DOCS_TOKEN"},
                "tools": {"allow": ["search_*"]},
            },
            "openmrs": {"url": "https://omrs-mcp/mcp", "auth": {"type": "openmrs-session"}},
        }
    }


def test_valid_config_expands_url_only():
    cfg = parse_config(good(), resolver())
    assert cfg.mcpServers["docs"].url == "https://docs.internal/mcp"


async def test_missing_credential_fails_closed_without_leaking():
    cfg = parse_config(good(), resolver())
    with pytest.raises(ConfigError, match="MCP_DOCS_TOKEN") as e:
        await validate_credentials(cfg, EnvCredentialResolver(environ={"MCP_DOCS_URL": "https://x"}))
    assert "s3cr3t" not in str(e.value)


async def test_resolver_rejects_non_allowlisted_refs():
    r = resolver()
    for ref in ("DATABASE_URL", "ANTHROPIC_API_KEY", "lowercase", "MCP_X; rm"):
        with pytest.raises(CredentialError):
            await r.resolve(ref)
    assert (await EnvCredentialResolver(frozenset({"JIRA_TOKEN"}), {"JIRA_TOKEN": "t"}).resolve("JIRA_TOKEN")).get_secret_value() == "t"


async def test_secret_not_in_repr():
    assert "s3cr3t" not in repr(await resolver().resolve("MCP_DOCS_TOKEN"))


def test_url_expansion_limited_to_mcp_vars():
    raw = good(); raw["mcpServers"]["docs"]["url"] = "https://h/${DATABASE_URL}"
    with pytest.raises(ConfigError, match="not allowed"):
        parse_config(raw, resolver())


def test_unknown_auth_type_and_extra_fields_rejected():
    raw = good(); raw["mcpServers"]["docs"]["auth"] = {"type": "magic"}
    with pytest.raises(ConfigError):
        parse_config(raw, resolver())
    raw = good(); raw["mcpServers"]["docs"]["surprise"] = 1
    with pytest.raises(ConfigError):
        parse_config(raw, resolver())


def test_literal_secret_value_is_rejected_by_schema_not_echoed():
    raw = good(); raw["mcpServers"]["docs"]["auth"] = {"type": "bearer", "credentialRef": "MCP_X", "token": "LEAK123"}
    with pytest.raises(ConfigError) as e:
        parse_config(raw, resolver())
    assert "LEAK123" not in str(e.value)


def test_stdio_rejected_by_default_and_allowed_when_enabled():
    raw = {"mcpServers": {"local": {"transport": "stdio", "command": "npx", "args": ["x"]}}}
    with pytest.raises(ConfigError, match="stdio"):
        parse_config(raw, resolver())
    assert parse_config(raw, resolver(), allow_stdio=True).stdio_servers == ["local"]


def test_server_name_rules():
    for bad in ("a__b", "Docs", "1x", "a_b"):
        raw = good(); raw["mcpServers"][bad] = raw["mcpServers"].pop("openmrs")
        with pytest.raises(ConfigError):
            parse_config(raw, resolver())


def test_write_override_requires_confirm():
    raw = good(); raw["mcpServers"]["docs"]["tools"]["overrides"] = {"create": {"access": "write"}}
    with pytest.raises(ConfigError):
        parse_config(raw, resolver())


def test_missing_file_and_bad_json(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.json", resolver())
    p = tmp_path / "c.json"; p.write_text("{bad")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(p, resolver())
    p.write_text(json.dumps(good()))
    assert "docs" in load_config(p, resolver()).mcpServers


async def test_auth_strategies():
    cfg = parse_config(good(), resolver())
    bearer = build_strategy(cfg.mcpServers["docs"].auth, resolver())
    assert bearer.scope == "shared"
    assert (await bearer.headers(None))["Authorization"] == "Bearer s3cr3t-value"
    omrs = build_strategy(cfg.mcpServers["openmrs"].auth, resolver())
    assert omrs.scope == "per-user"
    with pytest.raises(AuthError):
        await omrs.headers(None)
    u = UserContext("alice", "u-1", frozenset(), session_id="SESS-A")
    assert (await omrs.headers(u)) == {"X-OpenMRS-Session-Id": "SESS-A"}
    assert "SESS-A" not in repr(u)
