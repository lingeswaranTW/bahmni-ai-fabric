FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
# The telemetry extra is a no-op unless TELEMETRY_ENABLED=true (docs/OBSERVABILITY.md).
RUN uv sync --frozen --no-dev --no-editable --extra telemetry

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 bahmniai
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# HOST=0.0.0.0: inside a container the service must listen on all interfaces (the default outside is 127.0.0.1).
ENV PATH="/app/.venv/bin:$PATH" HOST=0.0.0.0 PORT=8090 MCP_CONFIG_PATH=/etc/bahmni_config/ai/mcp-servers.json
USER bahmniai
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/bahmni-ai/health" || exit 1
CMD ["python", "-m", "bahmni_ai.server"]
