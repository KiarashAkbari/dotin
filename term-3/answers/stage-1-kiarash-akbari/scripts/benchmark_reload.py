"""Measure how long emptying the Olist tables takes: the old DELETE loop vs one TRUNCATE.

Run it against a disposable database that already holds the full Olist load
(`python -m data_factory load-database --reset --skip-factory-sink`). Every
measurement runs in a transaction that is rolled back, so the data stays loaded:

* TRUNCATE: the statement `clear_olist_tables` runs now, timed --repeats times.
* DELETE: the per-table DELETE loop the loader used before, in its old order,
  under a statement_timeout of --delete-cap seconds per table, so a run that
  would take hours stops with "cancelled" instead of a number.
* DELETE of the first N parent orders, after their child rows are gone: the
  per-row referential checks that make the loop quadratic, at a few sizes.
* the whole repeated load (`load-database --skip-factory-sink` without --reset:
  clear, reload 9 CSVs, refresh the views, commit), timed once.

Usage (password from the environment, never from the config file):

    OLIST_DB_PASSWORD=... python scripts/benchmark_reload.py --config path/to/local.toml \
        --out docs/evidence/reload_delete_vs_truncate.json
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import platform
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa

from data_factory.cli import _load_database
from data_factory.config import load_config
from data_factory.database import (
    OLIST_SCHEMA,
    OLIST_TABLES,
    build_database_url,
    clear_olist_tables,
)
from data_factory.manifest import code_version

# The order the loader used before the fix (children first, so no FK error).
OLD_DELETE_ORDER = (
    "order_reviews",
    "payments",
    "order_items",
    "orders",
    "products",
    "sellers",
    "customers",
    "geolocation",
    "product_category_name_translation",
)
# Fixed table names from the constants above, quoted by the dialect.
DELETE_SQL = {table: f'DELETE FROM {OLIST_SCHEMA}."{table}"' for table in OLD_DELETE_ORDER}
ORDER_SAMPLE_SQL = sa.text(
    "DELETE FROM olist.orders WHERE order_id IN "
    "(SELECT order_id FROM olist.orders ORDER BY order_id LIMIT :n)"
)


def row_counts(connection: sa.Connection) -> dict[str, int]:
    return {
        table: connection.scalar(sa.text(f'SELECT count(*) FROM {OLIST_SCHEMA}."{table}"'))
        for table in OLIST_TABLES
    }


def timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def measure_truncate(engine: sa.Engine, repeats: int) -> dict:
    seconds = []
    for _ in range(repeats):
        with engine.connect() as connection:
            seconds.append(timed(lambda: clear_olist_tables(connection)))
            connection.rollback()
    return {
        "statement": "TRUNCATE TABLE <9 olist tables> RESTART IDENTITY",
        "runs_s": [round(s, 4) for s in seconds],
        "median_s": round(statistics.median(seconds), 4),
    }


def measure_delete(engine: sa.Engine, cap_s: int) -> dict:
    per_table = []
    status = "finished"
    with engine.connect() as connection:
        connection.execute(sa.text(f"SET LOCAL statement_timeout = {int(cap_s * 1000)}"))
        for table in OLD_DELETE_ORDER:
            start = time.perf_counter()
            try:
                connection.execute(sa.text(DELETE_SQL[table]))
            except sa.exc.OperationalError as exc:
                if getattr(exc.orig, "sqlstate", None) != "57014":  # query_canceled
                    raise
                per_table.append(
                    {"table": table, "seconds": round(time.perf_counter() - start, 2),
                     "status": "cancelled by statement_timeout"}
                )
                status = f"cancelled at {table} after {cap_s} s"
                break
            per_table.append(
                {"table": table, "seconds": round(time.perf_counter() - start, 3), "status": "done"}
            )
        connection.rollback()
    return {
        "statement": "DELETE FROM olist.<table>, one per table, old order",
        "statement_timeout_s": cap_s,
        "status": status,
        "per_table": per_table,
    }


def measure_order_rows(engine: sa.Engine, sizes: list[int], cap_s: int) -> list[dict]:
    """DELETE N orders once their children are gone: only the RI checks remain."""
    results = []
    for n in sizes:
        with engine.connect() as connection:
            connection.execute(sa.text(f"SET LOCAL statement_timeout = {int(cap_s * 1000)}"))
            for child in ("order_reviews", "payments", "order_items"):
                connection.execute(sa.text(DELETE_SQL[child]))
            start = time.perf_counter()
            try:
                connection.execute(ORDER_SAMPLE_SQL, {"n": n})
                seconds = round(time.perf_counter() - start, 3)
                status = "done"
            except sa.exc.OperationalError as exc:
                if getattr(exc.orig, "sqlstate", None) != "57014":
                    raise
                seconds = round(time.perf_counter() - start, 2)
                status = "cancelled by statement_timeout"
            connection.rollback()
        results.append({
            "orders_deleted": n,
            "seconds": seconds,
            "ms_per_order": round(1000 * seconds / n, 3) if status == "done" else None,
            "status": status,
        })
    return results


def measure_full_reload(cfg) -> dict:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        seconds = timed(lambda: _load_database(cfg, with_sink=False))
    return {
        "command": "load-database --skip-factory-sink (no --reset: TRUNCATE, reload, refresh, commit)",
        "seconds": round(seconds, 2),
        "output": out.getvalue().splitlines(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--delete-cap", type=int, default=600, help="seconds per DELETE")
    parser.add_argument("--order-sizes", default="250,500,1000,2000")
    parser.add_argument("--skip-full-reload", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    engine = sa.create_engine(build_database_url(cfg))
    try:
        with engine.connect() as connection:
            counts = row_counts(connection)
            server = connection.scalar(sa.text("SHOW server_version"))
        if counts["orders"] == 0:
            raise SystemExit("olist.orders is empty: run load-database --reset first")
        evidence = {
            "measured_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "code_version": code_version(),
            "environment": {
                "postgresql": server,
                "python": platform.python_version(),
                "cpu_count": os.cpu_count(),
                "note": "shared sandbox; other jobs ran on the same 2 vCPUs, so treat the "
                        "seconds as orders of magnitude, not as a benchmark of this machine",
            },
            "row_counts": counts,
            "truncate": measure_truncate(engine, args.repeats),
            "delete_order_rows": measure_order_rows(
                engine, [int(n) for n in args.order_sizes.split(",")], args.delete_cap
            ),
            "delete_loop": measure_delete(engine, args.delete_cap),
        }
        if not args.skip_full_reload:
            evidence["full_reload"] = measure_full_reload(cfg)
    finally:
        engine.dispose()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
