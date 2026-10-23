# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bahmni_ai.main import create_app
from bahmni_ai.mcp.auth import validate_credentials
from bahmni_ai.mcp.config import ConfigError, parse_config
from bahmni_ai.mcp.credentials import EnvCredentialResolver
from bahmni_ai.mcp.registry import McpRegistry
from bahmni_ai.settings import Settings


def write(tmp_path, cfg) -> str:
    p = tmp_path / "mcp-servers.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def settings(path, **kw):
    return Settings(ai_model="test", mcp_config_path=path, openmrs_url="http://omrs:8080", **kw)


def test_empty_config_starts_and_serves_health(tmp_path):
    with TestClient(create_app(settings(write(tmp_path, {"mcpServers": {}})))) as c:
        assert c.get("/bahmni-ai/health").status_code == 200


def test_missing_config_file_stops_startup(tmp_path):
    with pytest.raises(ConfigError, match="not found"), TestClient(create_app(settings(str(tmp_path / "x.json")))):
        pass


def test_missing_credential_stops_startup_without_leaking(tmp_path, monkeypatch):
    monkeypatch.delenv("MCP_X_TOKEN", raising=False)
    cfg = {
        "mcpServers": {
            "x": {"url": "https://x.example/mcp", "auth": {"type": "bearer", "credentialRef": "MCP_X_TOKEN"}}
        }
    }
    with pytest.raises(ConfigError, match="MCP_X_TOKEN"), TestClient(create_app(settings(write(tmp_path, cfg)))):
        pass


def test_unknown_model_provider_stops_startup(tmp_path):
    from bahmni_ai.agent.factory import ModelConfigError

    s = Settings(ai_model="nonsense:m", mcp_config_path=write(tmp_path, {"mcpServers": {}}))
    with pytest.raises(ModelConfigError), TestClient(create_app(s)):
        pass


def test_stdio_disabled_by_default(tmp_path):
    cfg = {"mcpServers": {"local": {"transport": "stdio", "command": "x"}}}
    with pytest.raises(ConfigError, match="stdio"), TestClient(create_app(settings(write(tmp_path, cfg)))):
        pass


async def test_stdio_server_runs_shared_with_injected_credential():
    stub = str(Path(__file__).parent / "stdio_stub.py")
    cfg = {
        "mcpServers": {
            "local": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [stub],
                "envRefs": {"LOCAL_TOKEN": "MCP_LOCAL_TOKEN"},
                "tools": {"allow": ["ping"]},
                "trustToolAnnotations": True,
            }
        }
    }
    r = EnvCredentialResolver(environ={"MCP_LOCAL_TOKEN": "tok"})
    config = parse_config(cfg, r, allow_stdio=True)
    await validate_credentials(config, r)
    reg = McpRegistry(config, r, connect_timeout_seconds=20)
    await reg.start()
    try:
        async with reg.open(None) as opened:
            assert opened.unavailable == [] and len(opened.toolsets) == 1
    finally:
        await reg.close()


async def test_stdio_subprocess_does_not_inherit_service_secrets(monkeypatch, validator, make_deps):
    from tests.test_registry_policy import run

    monkeypatch.setenv("PARENT_SECRET_KEY", "must-not-leak")
    stub = str(Path(__file__).parent / "stdio_stub.py")
    cfg = {
        "mcpServers": {
            "local": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [stub],
                "tools": {"allow": ["leaked"]},
                "requiredPrivilege": "Use AI Chatbot",
                "trustToolAnnotations": True,
            }
        }
    }
    r = EnvCredentialResolver(environ={})
    reg = McpRegistry(parse_config(cfg, r, allow_stdio=True), r, connect_timeout_seconds=20)
    await reg.start()
    try:
        u = validator.add("S1", "alice", ("Use AI Chatbot",))
        res = await run(reg, u, make_deps(u), "local__leaked")
        assert res.output == "keys:"
    finally:
        await reg.close()
