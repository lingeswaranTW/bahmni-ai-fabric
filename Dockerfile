FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
# The telemetry extra is a no-op unless TELEMETRY_ENABLED=true (docs/OBSERVABILITY.md).
RUN uv sync --frozen --no-dev --no-editable --extra telemetry

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 bahmniai
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PORT=8090 MCP_CONFIG_PATH=/etc/bahmni_config/ai/mcp-servers.json
USER bahmniai
EXPOSE 8090
CMD ["python", "-m", "bahmni_ai.server"]
