"""Load Olist twice from an empty schema and compare everything that should match.

Each run: `python -m data_factory load-database --reset --skip-factory-sink`
(the one-command build, run from the stage-1 folder), then the closed-month
snapshot refresh, then a content fingerprint of every olist table and the
result hash of the five dashboard queries. The two runs must agree.

DESTRUCTIVE: --reset drops and rebuilds the olist schema of the configured
database. Use a disposable database.

    OLIST_DB_PASSWORD=... python scripts/determinism_two_loads.py \
        --config ../stage-1-kiarash-akbari/config/local.toml \
        --out data/curated/evidence/determinism-two-loads.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from psycopg import sql as pgsql

STAGE2 = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE2 / "src"))

from olist_perf import bench, db, evidence  # noqa: E402

TABLES = ("customers", "sellers", "product_category_name_translation", "products",
          "orders", "order_items", "payments", "order_reviews", "geolocation",
          "monthly_category_snapshot")
# Content hash independent of physical row order. refreshed_at is a timestamp
# of the refresh itself, so the snapshot is hashed without it.
FINGERPRINT = pgsql.SQL(
    "SELECT count(*), md5(string_agg(h, '' ORDER BY h)) "
    "FROM (SELECT md5(CAST(t AS text)) AS h FROM {} AS t) AS rows")
SNAPSHOT_FINGERPRINT = pgsql.SQL(
    "SELECT count(*), md5(string_agg(h, '' ORDER BY h)) FROM (SELECT md5(CAST(("
    "month_start, product_category_name, sales_value, order_count, "
    "average_order_value) AS text)) AS h FROM olist.monthly_category_snapshot) AS rows")


def one_run(config: Path) -> dict:
    started = perf_counter()
    completed = subprocess.run(
        [sys.executable, "-m", "data_factory", "load-database", "--config",
         str(config), "--reset", "--skip-factory-sink"],
        cwd=db.STAGE1_DIR, env=os.environ.copy(), capture_output=True, text=True)
    load_s = round(perf_counter() - started, 1)
    if completed.returncode != 0:
        raise SystemExit(f"load-database failed ({completed.returncode}):\n"
                         f"{completed.stderr[-2000:]}")
    engine = db.make_engine(config)
    try:
        refresh = evidence.refresh_closed_months(engine)
        fingerprints = {}
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
            with raw.cursor() as cursor:
                for table in TABLES:
                    statement = (SNAPSHOT_FINGERPRINT if table == "monthly_category_snapshot"
                                 else FINGERPRINT.format(pgsql.Identifier("olist", table)))
                    cursor.execute(statement)
                    count, digest = cursor.fetchone()
                    fingerprints[table] = {"rows": count, "md5": digest}
            connection.rollback()
        queries = {}
        with db.session(engine) as connection:
            for name in bench.QUERY_FILES:
                source = bench.read_query(STAGE2 / "sql" / "queries" / name)
                _, rows, digest = bench.run_once(connection, source)
                queries[name] = {"rows": rows, "result_sha256": digest}
            connection.rollback()
    finally:
        engine.dispose()
    return {"load_seconds_wall": load_s, "load_stdout": completed.stdout.splitlines(),
            "snapshot_refresh": refresh, "tables": fingerprints, "queries": queries}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    config = args.config.resolve()
    runs = [one_run(config), one_run(config)]
    same_tables = runs[0]["tables"] == runs[1]["tables"]
    same_queries = runs[0]["queries"] == runs[1]["queries"]
    report = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command": "python -m data_factory load-database --config <config> "
                   "--reset --skip-factory-sink (cwd: stage-1), twice",
        "identical_tables": same_tables, "identical_query_results": same_queries,
        "runs": runs,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str)
                        + "\n", encoding="utf-8")
    print(f"identical_tables={same_tables} identical_query_results={same_queries}")
    return 0 if same_tables and same_queries else 1


if __name__ == "__main__":
    raise SystemExit(main())
