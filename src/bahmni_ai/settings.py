# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Service settings. Everything comes from environment variables (Bahmni compose convention)."""

from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    host: str = "127.0.0.1"  # the container image sets HOST=0.0.0.0
    port: int = 8090
    log_level: str = "info"
    service_path: str = Field("/bahmni-ai", description="URL prefix, like /template-service")
    openmrs_url: str = "http://openmrs:8080"
    openmrs_timeout_seconds: float = 5.0
    session_cache_ttl_seconds: float = 30.0

    # One or more comma-separated provider:model entries, tried in order (docs/PROVIDERS.md).
    ai_model: str = "anthropic:claude-sonnet-5-5"
    ai_request_timeout_seconds: float = 60.0
    ai_max_tool_steps: int = 10
    # Retry of transient provider failures (429, 5xx, connection errors) before the request has started streaming.
    ai_max_retries: int = Field(2, ge=0, le=10)
    ai_retry_base_delay_seconds: float = Field(1.0, ge=0)
    ai_retry_max_delay_seconds: float = Field(20.0, ge=0)
    # Approximate token budget for the stored history sent to the model (oldest whole turns are dropped first;
    # storage keeps everything). Lower it for small-context local models. 0 disables trimming.
    ai_history_max_tokens: int = Field(32000, ge=0)
    # Hard cap on tokens (input + output) one request may use across all its model/tool rounds. 0 = no cap.
    ai_max_total_tokens: int = Field(0, ge=0)

    # Optional external source for the system prompt (docs/PROMPTS.md). Off by default.
    prompt_hub_enabled: bool = False
    # An adapter name. Credentials come from the adapter's own variable (OpenLIT: OPENLIT_API_KEY), not from here.
    prompt_hub_source: str = "openlit"
    # append: built-in safety prompt is kept and the hub text added after it. replace: the hub text is the whole prompt.
    prompt_hub_mode: Literal["append", "replace"] = "append"
    prompt_hub_url: str = ""
    prompt_hub_prompt_name: str = ""
    prompt_hub_prompt_version: str = ""  # empty = the source's latest
    prompt_hub_refresh_seconds: float = Field(300.0, ge=5)
    prompt_hub_timeout_seconds: float = Field(3.0, gt=0)
    prompt_hub_max_chars: int = Field(8000, ge=100)

    # Stream temporary "what the assistant is doing" events (activity) to the UI. Content-free; see agent/events.py.
    ui_activity_events: bool = True

    # Clinic time zone (IANA name) used for the date/time the model is told (docs/PROMPTS.md#placeholders).
    timezone: str = "UTC"
    # Append a "Current date and time" line when the final prompt has no date placeholder.
    ai_inject_datetime: bool = True

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"TIMEZONE '{value}' is not an IANA time zone name (for example Asia/Kolkata)") from exc
        return value

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

    # Observability (docs/OBSERVABILITY.md). Off by default. The OTLP endpoint and headers use the standard
    # OTEL_EXPORTER_OTLP_* variables, read by the OpenTelemetry SDK itself.
    telemetry_enabled: bool = False
    telemetry_backend: Literal["otlp", "console"] = "otlp"
    telemetry_capture_content: bool = False  # prompts, answers, tool arguments/results in spans: synthetic data only
    telemetry_export_timeout_seconds: float = 2.0
    telemetry_shutdown_timeout_seconds: float = 5.0
    telemetry_discovery_spans: Literal["errors-only", "all"] = "errors-only"
    telemetry_discovery_slow_ms: int = 1000
    # Value of gen_ai.conversation.id, the backend's session key. `off` (default): Pydantic AI's own random id, which
    # already groups a conversation's turns; `raw`: our conversation id (dev, searchable); `hmac`: keyed hash of it
    # (needs TELEMETRY_SESSION_KEY).
    telemetry_session_id: Literal["off", "raw", "hmac"] = "off"
    telemetry_session_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
