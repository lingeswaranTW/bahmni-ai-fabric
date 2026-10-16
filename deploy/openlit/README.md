# OpenLIT backend for bahmni-ai telemetry

A **separate** self-hosted deployment (not part of the `bahmni-ai` service): pinned OpenLIT `2.1.0` + ClickHouse `24.4.1`,
hardened against the findings in `docs/OBSERVABILITY_PHASE0_RESULT.md`. bahmni-ai only needs its OTLP URL
(`OTEL_EXPORTER_OTLP_ENDPOINT`), so it can be swapped for any OTLP backend.

| Upstream default | Here |
|---|---|
| `ghcr.io/openlit/openlit:latest` | pinned `2.1.0`; ClickHouse and nginx pinned too |
| seeded login `user@openlit.io` / `openlituser` | `harden.py` replaces it and verifies the default is rejected |
| open self-registration | blocked by `ui-proxy` (nginx), verified `403` |
| `TELEMETRY_ENABLED=true` (vendor product telemetry) | `"false"` |
| ClickHouse ports 8123/9000 published; weak default password | not published; password from `.env.local` |
| 730 h retention, fixed | `TRACE_RETENTION_HOURS` (default 168) and `METRIC_RETENTION_HOURS` (720), re-applied on every start |

## Run
```bash
cd deploy/openlit
cp .env.example .env.local        # untracked; set OPENLIT_DB_PASSWORD and OPENLIT_ADMIN_EMAIL
docker compose --env-file .env.local up -d
cd ../.. && uv run --with bcrypt python deploy/openlit/harden.py     # once, right after the first start
```
`harden.py` stores a generated admin password in `.env.local` (mode 600) if none is set, rewrites the seeded account,
and checks: default login rejected, admin login works, registration blocked. It is idempotent. Add
`--remove-other-users` to delete accounts that registered before hardening. Use `DOCKER_CONTEXT` to pick a Docker context.

UI: `http://127.0.0.1:3300` (loopback by default). OTLP for bahmni-ai: `http://<host>:4318`.
For remote access put TLS and SSO or a VPN in front; OTLP receivers are unauthenticated, so keep them on an internal network.

## Operate
- **Retention:** change `TRACE_RETENTION_HOURS` / `METRIC_RETENTION_HOURS` in `.env.local` and `up -d`; ClickHouse re-applies TTLs on start.
- **Delete data (erasure, incident):** `python3 purge.py --all`, or `--service bahmni-ai --before 2026-10-01T00:00:00`, or `--trace-id <hex>`.
  It also clears the derived trace index and metric tables that a plain delete on `otel_traces` would leave behind.
- **Upgrade:** change the pinned tags in `docker-compose.yml`, re-run the checks in `spike/observability/`, then `up -d`.
- **Stop and delete everything:** `docker compose --env-file .env.local down -v`.

Ownership, access, retention and deletion responsibilities must be assigned before rollout:
`docs/OBSERVABILITY.md` section 10.2.
