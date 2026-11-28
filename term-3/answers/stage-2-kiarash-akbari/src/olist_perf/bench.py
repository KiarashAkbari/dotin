"""Repeatable query timing and EXPLAIN (ANALYZE, BUFFERS) capture.

Protocol (same for every query and every variant):
  * one session with ``db.SESSION_SETTINGS``;
  * ``warmups`` unmeasured runs, then ``repeats`` measured runs;
  * a run is timed on the client from execute to the last fetched row, so the
    number includes planning, execution, transfer and Python decoding;
  * the result rows are hashed; every run of a query must return the same hash;
  * the plan is captured once with EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) and
    its server-side Execution Time is reported next to the client timing.
"""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from time import perf_counter

import sqlalchemy as sa
from psycopg import sql as pgsql

from . import db

BUDGET_MS = 200.0
QUERY_FILES = (
    "q1_monthly_category_sales.sql",
    "q2_delivery_by_state_month.sql",
    "q3_delay_and_satisfaction.sql",
    "q4_top_sellers.sql",
    "q5_payment_distribution.sql",
)
EXPLAIN_TEMPLATE = pgsql.SQL("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {}")


def read_query(path: Path) -> str:
    """Return one versioned query without its trailing semicolon."""
    return Path(path).read_text(encoding="utf-8").strip().removesuffix(";")


def _canonical(value):
    if isinstance(value, float):
        # Floating aggregates (CORR) may differ in the last bits when the
        # summation order changes; 12 significant digits is the contract.
        return float(f"{value:.12g}")
    if isinstance(value, Decimal):
        return str(value.normalize()) if value == value else "NaN"
    return value


def result_digest(rows) -> tuple[int, str]:
    """SHA-256 over the rows in returned order, after canonical float rounding."""
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        payload = json.dumps([_canonical(v) for v in row], default=str,
                             ensure_ascii=False, separators=(",", ":"))
        digest.update(payload.encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def run_once(connection: sa.Connection, source: str) -> tuple[float, int, str]:
    started = perf_counter()
    rows = connection.exec_driver_sql(source).fetchall()
    elapsed = (perf_counter() - started) * 1000
    count, digest = result_digest(rows)
    return elapsed, count, digest


def explain(connection: sa.Connection, source: str) -> dict:
    """EXPLAIN ANALYZE one statement; the query text is composed, not concatenated."""
    raw = connection.connection.driver_connection
    with raw.cursor() as cursor:
        cursor.execute(EXPLAIN_TEMPLATE.format(pgsql.SQL(source)))
        document = cursor.fetchone()[0]
    if isinstance(document, str):
        document = json.loads(document)
    return document[0]


def plan_nodes(plan: dict) -> list[dict]:
    """Flatten a JSON plan into readable rows (depth, node, relation, rows)."""
    rows: list[dict] = []

    def walk(node: dict, depth: int) -> None:
        rows.append({
            "depth": depth,
            "node": node.get("Node Type"),
            "relation": node.get("Relation Name"),
            "index": node.get("Index Name"),
            "est_rows": node.get("Plan Rows"),
            "actual_rows": node.get("Actual Rows"),
            "loops": node.get("Actual Loops"),
            "total_cost": node.get("Total Cost"),
            "actual_total_ms": node.get("Actual Total Time"),
        })
        for child in node.get("Plans", []):
            walk(child, depth + 1)

    walk(plan["Plan"], 0)
    return rows


def scan_summary(plan: dict) -> dict[str, list[str]]:
    """Which relations are read by Seq Scan and which through an index."""
    summary: dict[str, list[str]] = {"seq_scan": [], "index_scan": []}
    for row in plan_nodes(plan):
        if row["node"] == "Seq Scan":
            summary["seq_scan"].append(row["relation"])
        elif row["node"] in ("Index Scan", "Index Only Scan", "Bitmap Index Scan"):
            summary["index_scan"].append(row["index"] or row["relation"])
    return summary


def measure(connection: sa.Connection, source: str, *, repeats: int = 15,
            warmups: int = 3, with_plan: bool = True,
            end_transaction: bool = True) -> dict:
    """Time one statement under the protocol in the module docstring."""
    if repeats < 3 or warmups < 0:
        raise ValueError("use at least 3 measured runs and non-negative warmups")
    for _ in range(warmups):
        run_once(connection, source)
    samples = [run_once(connection, source) for _ in range(repeats)]
    signatures = {(rows, digest) for _, rows, digest in samples}
    if len(signatures) != 1:
        raise RuntimeError("the result changed between repeated runs")
    timings = [elapsed for elapsed, _, _ in samples]
    result = {
        "elapsed_ms": [round(t, 3) for t in timings],
        "median_ms": round(statistics.median(timings), 3),
        "p95_ms": round(sorted(timings)[math.ceil(0.95 * len(timings)) - 1], 3),
        "max_ms": round(max(timings), 3),
        "under_budget_median": statistics.median(timings) < BUDGET_MS,
        "all_runs_under_budget": max(timings) < BUDGET_MS,
        "rows": samples[0][1],
        "result_sha256": samples[0][2],
        "sql_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }
    if with_plan:
        plan = explain(connection, source)
        result["server_execution_ms"] = plan.get("Execution Time")
        result["planning_ms"] = plan.get("Planning Time")
        result["scans"] = scan_summary(plan)
        result["plan"] = plan
    if end_transaction:
        connection.rollback()
    return result


def environment(connection: sa.Connection) -> dict:
    return {
        "postgresql_version": connection.scalar(sa.text("SELECT version()")),
        "settings": {
            row.name: row.setting for row in connection.execute(sa.text(
                "SELECT name, setting FROM pg_settings WHERE name = ANY(:names) "
                "ORDER BY name"), {"names": sorted(db.SESSION_SETTINGS) + [
                    "shared_buffers", "random_page_cost", "effective_cache_size"]})
        },
    }


def benchmark_dir(engine: sa.Engine, query_dir: Path, *, repeats: int = 15,
                  warmups: int = 3, with_plan: bool = True) -> dict:
    """Measure the five dashboard queries of one directory in one session."""
    report = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "query_dir": Path(query_dir).name if Path(query_dir).name != "queries"
        else "queries",
        "protocol": __doc__.strip().splitlines()[2:9],
        "repeats": repeats, "warmups": warmups, "budget_ms": BUDGET_MS,
        "queries": {},
    }
    with db.session(engine) as connection:
        report["environment"] = environment(connection)
        connection.rollback()
        for name in QUERY_FILES:
            source = read_query(Path(query_dir) / name)
            report["queries"][name] = measure(
                connection, source, repeats=repeats, warmups=warmups,
                with_plan=with_plan)
    return report
