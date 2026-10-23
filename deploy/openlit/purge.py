#!/usr/bin/env python3
"""Delete telemetry from ClickHouse, including derived tables. Use for erasure requests and incident response.

OpenLIT keeps traces in `otel_traces` plus a derived index `otel_traces_trace_id_ts` (trace id and time range), and metrics
and logs in their own tables. Deleting only `otel_traces` leaves the rest behind (Phase 0 finding), so this covers all.

usage:  python3 purge.py --all
        python3 purge.py --service bahmni-ai --before 2026-10-01T00:00:00
        python3 purge.py --trace-id <32 hex>
Runs `clickhouse-client` inside the container using its own environment, so no password is passed on a command line.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TABLES = ["otel_traces", "otel_logs", "otel_metrics_gauge", "otel_metrics_sum", "otel_metrics_histogram",
          "otel_metrics_summary", "otel_metrics_exponential_histogram"]
TIME = {"otel_traces": "Timestamp", "otel_logs": "Timestamp"}  # metrics tables use TimeUnix


def ch(env_file: str, sql: str) -> str:
    cmd = ["docker", "compose", "-f", str(HERE / "docker-compose.yml"), "--env-file", env_file, "exec", "-T", "-e", "SQL", "clickhouse", "sh", "-c",
           'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --database "$CLICKHOUSE_DATABASE" '
           "--multiquery --query \"$SQL\""]
    return subprocess.run(cmd, env={**os.environ, "SQL": sql}, capture_output=True, text=True, check=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=str(HERE / ".env.local"))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true")
    g.add_argument("--service")
    g.add_argument("--trace-id")
    ap.add_argument("--before", help="only rows older than this ISO timestamp")
    args = ap.parse_args()
    if args.before and not re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", args.before):
        sys.exit("--before must look like 2026-10-01T00:00:00")
    if args.service and not re.fullmatch(r"[A-Za-z0-9._-]+", args.service):
        sys.exit("invalid service name")
    if args.trace_id and not re.fullmatch(r"[0-9a-f]{32}", args.trace_id):
        sys.exit("trace id must be 32 lowercase hex characters")

    before = args.before.replace("T", " ") if args.before else None
    statements = []
    for table in TABLES:
        col = TIME.get(table, "TimeUnix")
        conds = []
        if args.service:
            conds.append(f"ServiceName = '{args.service}'")
        if args.trace_id and table in ("otel_traces", "otel_logs"):
            conds.append(f"TraceId = '{args.trace_id}'")
        elif args.trace_id:
            continue  # metrics carry no trace id
        if before:
            conds.append(f"{col} < toDateTime64('{before}', 9)")
        statements.append(f"ALTER TABLE {table} DELETE WHERE {' AND '.join(conds) or '1'} SETTINGS mutations_sync = 2")
    # derived trace index: remove entries whose trace no longer exists in otel_traces
    statements.append("ALTER TABLE otel_traces_trace_id_ts DELETE WHERE TraceId NOT IN (SELECT TraceId FROM otel_traces) "
                      "SETTINGS mutations_sync = 2")
    for sql in statements:
        ch(args.env, sql)
        print("ok:", sql.split(" SETTINGS")[0][:110])
    remaining = ch(args.env, "SELECT count() FROM otel_traces").strip()
    index = ch(args.env, "SELECT count() FROM otel_traces_trace_id_ts").strip()
    print(f"remaining otel_traces rows: {remaining}; trace index rows: {index}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
