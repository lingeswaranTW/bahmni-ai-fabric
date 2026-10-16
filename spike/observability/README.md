# Observability Phase 0 spike

Throwaway code for `docs/OBSERVABILITY_PHASE0_RESULT.md`. The application under `src/` is imported, never modified.
Synthetic data only. Run from the repository root; the OpenTelemetry packages are added per run with `uv run --with`
(nothing is added to `pyproject.toml` or `uv.lock`).

```bash
# 1. Pinned OpenLIT + ClickHouse (loopback ports 3300, 4317, 4318; ClickHouse not published)
cd spike/observability/openlit
printf 'OPENLIT_DB_PASSWORD=%s\n' "$(openssl rand -hex 16)" > .env.local     # untracked
docker --context std compose --env-file .env.local up -d

# 2. Experiments
cd ../../..
W="uv run --with opentelemetry-sdk --with opentelemetry-exporter-otlp-proto-http python"
$W spike/observability/e1_ingest_redaction.py <content on|off> <scrub on|off>   # ingestion + redaction
$W spike/observability/e3_propagation.py <baseline|gate|fastmcp-off|env-none>   # trace-context propagation
$W spike/observability/e4_failure_isolation.py <none|ok|down|hang|raising|raising-start|raising-end|raising-exporter> [turns]
SPIKE_EXPORT_TIMEOUT=2 $W spike/observability/e4_failure_isolation.py down 40   # bounded exporter timeout

# 3. Clean up
docker --context std compose --env-file spike/observability/openlit/.env.local -f spike/observability/openlit/docker-compose.yml down -v
```
Raw outputs are in `results/*.json`.
