# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Service settings. Everything comes from environment variables (Bahmni compose convention)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    port: int = 8090
    log_level: str = "info"
    service_path: str = Field("/bahmni-ai", description="URL prefix, like /template-service")
    openmrs_url: str = "http://openmrs:8080"
    openmrs_timeout_seconds: float = 5.0
    session_cache_ttl_seconds: float = 30.0

    ai_model: str = "anthropic:claude-sonnet-5-5"
    ai_request_timeout_seconds: float = 60.0
    ai_max_tool_steps: int = 10

    mcp_config_path: str = "/etc/bahmni_config/ai/mcp-servers.json"
    mcp_allow_stdio: bool = False
    mcp_call_timeout_seconds: float = 30.0
    # Extra credential names (beyond the MCP_ prefix) the resolver may read.
    credential_allowlist: str = ""

    # Privilege every chatbot user must hold (created in OpenMRS).
    gate_privilege: str = "Use AI Chatbot"

    # How much tool output is kept in stored history: none | truncated | full
    history_tool_results: Literal["none", "truncated", "full"] = "none"
    database_url: str = ""
    confirmation_ttl_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
