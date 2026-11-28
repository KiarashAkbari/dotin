"""Evidence for the stage-2 report: before/after plans, index value and cost.

Every experiment that changes the schema (dropping an index to see the plan
without it, building the rejected index, inserting rows to time writes) runs
inside one transaction that is always rolled back, so the database ends
exactly as it started. Identifiers are composed with psycopg.sql.Identifier;
values are bound parameters.
"""
from __future__ import annotations

import statistics
from datetime import timedelta
from pathlib import Path
from time import perf_counter

import sqlalchemy as sa
from psycopg import sql as pgsql

from . import bench, db

SCHEMA = "olist"

# Each 004 index: its table and the optimized query whose plan justifies it.
INDEXES = {
    "ix_orders_purchase_ts": ("orders", "q1_monthly_category_sales.sql"),
    "ix_orders_inactive": ("orders", "q5_payment_distribution.sql"),
    "ix_orders_dispatch_cover": ("orders", "q4_top_sellers.sql"),
    "ix_order_reviews_order_score": ("order_reviews", "q3_delay_and_satisfaction.sql"),
    "ix_payments_order_type_inst": ("payments", "q5_payment_distribution.sql"),
}
# Q1's index only matters for the live tail; its evidence keeps the snapshot
# through this month, so three months (2018-07..2018-09, plus the 4 orders of
# 2018-10) are aggregated live. Deleted inside the rolled-back transaction.
Q1_EVIDENCE_SNAPSHOT_THROUGH = "2018-06-01"
TRIM_SNAPSHOT = sa.text(
    "DELETE FROM olist.monthly_category_snapshot WHERE month_start > CAST(:month AS date)"
)

# Indexes that were built, measured and dropped (sql/rejected/004_rejected_*).
REJECTED_INDEXES = {
    "ix_orders_delivered_partial": (
        "CREATE INDEX ix_orders_delivered_partial ON olist.orders "
        "(customer_id) INCLUDE (order_purchase_timestamp, "
        "order_delivered_customer_date, order_estimated_delivery_date) "
        "WHERE order_status = 'delivered' "
        "AND order_delivered_customer_date IS NOT NULL",
        "orders", "q2_delivery_by_state_month.sql"),
    "ix_order_items_seller_order": (
        "CREATE INDEX ix_order_items_seller_order ON olist.order_items "
        "(seller_id, order_id)",
        "order_items", "q4_top_sellers.sql"),
}


# Write cost is measured on copies in a scratch schema created inside a
# transaction that is rolled back: the real tables never receive the rows, so
# their heap and indexes do not bloat. LIKE ... INCLUDING ALL copies the
# primary key, CHECK constraints and the 004 indexes; foreign keys are not
# copied, so FK checks are excluded from both variants alike.
SCRATCH_SCHEMA = "olist_perf_scratch"
WRITE_TABLES = ("orders", "order_items", "order_reviews", "payments")


def _drop_index(connection: sa.Connection, name: str) -> None:
    raw = connection.connection.driver_connection
    with raw.cursor() as cursor:
        cursor.execute(pgsql.SQL("DROP INDEX {}").format(pgsql.Identifier(SCHEMA, name)))


def _compact(result: dict) -> dict:
    """Timing plus a readable plan, without the raw JSON plan."""
    keep = {k: v for k, v in result.items() if k != "plan"}
    if "plan" in result:
        keep["plan_nodes"] = bench.plan_nodes(result["plan"])
    return keep


def before_after(engine: sa.Engine, stage2: Path, *, repeats: int = 15,
                 warmups: int = 3) -> dict:
    """Baseline query without the 004 indexes vs optimized query with them.

    "Before" drops every 004 index inside a transaction (rolled back), so it is
    the database the baseline was written for. Both must return the same rows.
    """
    out = {}
    with db.session(engine) as connection:
        for name in bench.QUERY_FILES:
            baseline = bench.read_query(stage2 / "sql" / "baseline" / name)
            optimized = bench.read_query(stage2 / "sql" / "queries" / name)
            connection.begin()
            try:
                for index in INDEXES:
                    _drop_index(connection, index)
                before = bench.measure(connection, baseline, repeats=repeats,
                                       warmups=warmups, end_transaction=False)
            finally:
                connection.rollback()
            after = bench.measure(connection, optimized, repeats=repeats,
                                  warmups=warmups)
            out[name] = {
                "before": _compact(before),
                "after": _compact(after),
                "same_result": (before["rows"], before["result_sha256"])
                == (after["rows"], after["result_sha256"]),
                "speedup": round(before["median_ms"] / after["median_ms"], 2),
            }
    return out


def index_evidence(engine: sa.Engine, stage2: Path, *, repeats: int = 9,
                   warmups: int = 2) -> dict:
    """For each 004 index: its query with the index and with it dropped."""
    out = {}
    with db.session(engine) as connection:
        for index, (table, query) in INDEXES.items():
            source = bench.read_query(stage2 / "sql" / "queries" / query)
            runs = {}
            for variant in ("with_index", "without_index"):
                connection.begin()
                try:
                    if query.startswith("q1"):
                        connection.execute(TRIM_SNAPSHOT,
                                           {"month": Q1_EVIDENCE_SNAPSHOT_THROUGH})
                    if variant == "without_index":
                        _drop_index(connection, index)
                    runs[variant] = _compact(bench.measure(
                        connection, source, repeats=repeats, warmups=warmups,
                        end_transaction=False))
                finally:
                    connection.rollback()
            used = any(node["index"] == index
                       for node in runs["with_index"]["plan_nodes"])
            out[index] = {
                "table": table, "query": query, "used_in_plan": used,
                "median_with_ms": runs["with_index"]["median_ms"],
                "median_without_ms": runs["without_index"]["median_ms"],
                "saved_ms": round(runs["without_index"]["median_ms"]
                                  - runs["with_index"]["median_ms"], 3),
                "same_result": runs["with_index"]["result_sha256"]
                == runs["without_index"]["result_sha256"],
                **runs,
            }
    return out


def rejected_indexes(engine: sa.Engine, stage2: Path, *, repeats: int = 9,
                     warmups: int = 2) -> dict:
    """Build each rejected index in a transaction, measure its query, roll back."""
    out = {}
    with db.session(engine) as connection:
        for name, (ddl, table, query) in REJECTED_INDEXES.items():
            source = bench.read_query(stage2 / "sql" / "queries" / query)
            runs = {"without_index": _compact(bench.measure(
                connection, source, repeats=repeats, warmups=warmups))}
            connection.begin()
            try:
                raw = connection.connection.driver_connection
                started = perf_counter()
                with raw.cursor() as cursor:
                    cursor.execute(pgsql.SQL(ddl))
                    cursor.execute(pgsql.SQL("ANALYZE {}").format(
                        pgsql.Identifier(SCHEMA, table)))
                build_ms = (perf_counter() - started) * 1000
                size = connection.scalar(sa.text(
                    "SELECT pg_relation_size(c.oid) FROM pg_class AS c "
                    "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = :schema AND c.relname = :name"),
                    {"schema": SCHEMA, "name": name})
                runs["with_index"] = _compact(bench.measure(
                    connection, source, repeats=repeats, warmups=warmups,
                    end_transaction=False))
            finally:
                connection.rollback()
            out[name] = {
                "query": query, "definition": ddl,
                "build_ms": round(build_ms, 3), "index_bytes": size,
                "used_in_plan": any(node["index"] == name
                                    for node in runs["with_index"]["plan_nodes"]),
                "median_with_ms": runs["with_index"]["median_ms"],
                "median_without_ms": runs["without_index"]["median_ms"],
                "same_result": runs["with_index"]["result_sha256"]
                == runs["without_index"]["result_sha256"],
                **runs,
            }
    return out


def index_sizes(engine: sa.Engine) -> list[dict]:
    """Every index of the olist tables with its size and its table's heap size."""
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(sa.text(
            "SELECT t.relname AS table_name, i.relname AS index_name, "
            "pg_relation_size(i.oid) AS index_bytes, "
            "pg_relation_size(t.oid) AS table_bytes, "
            "i.relname = ANY(CAST(:ours AS text[])) AS from_004 "
            "FROM pg_index x JOIN pg_class t ON t.oid = x.indrelid "
            "JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = :schema AND t.relkind = 'r' "
            "ORDER BY t.relname, i.relname"),
            {"schema": SCHEMA, "ours": list(INDEXES)}).mappings()]


def rebuilt_sizes(engine: sa.Engine) -> list[dict]:
    """Size of each 004 index as the load left it vs right after REINDEX.

    004 runs before the CSV load, so its indexes grow by page splits during
    the inserts; REINDEX shows what a build after the load would occupy. Each
    REINDEX runs in a transaction that is rolled back.
    """
    size = sa.text(
        "SELECT pg_relation_size(c.oid) FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :schema AND c.relname = :name")
    out = []
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        for name in INDEXES:
            params = {"schema": SCHEMA, "name": name}
            try:
                loaded = connection.scalar(size, params)
                with raw.cursor() as cursor:
                    cursor.execute(pgsql.SQL("REINDEX INDEX {}").format(
                        pgsql.Identifier(SCHEMA, name)))
                rebuilt = connection.scalar(size, params)
            finally:
                connection.rollback()
            out.append({"index": name, "bytes_as_loaded": loaded,
                        "bytes_after_reindex": rebuilt,
                        "ratio": round(loaded / rebuilt, 3)})
    return out


def size_summary(rows: list[dict]) -> dict:
    """Per table: heap bytes, 004 index bytes, ratio; plus the total."""
    tables: dict[str, dict] = {}
    for row in rows:
        entry = tables.setdefault(row["table_name"], {
            "table_bytes": row["table_bytes"], "index_004_bytes": 0,
            "all_index_bytes": 0})
        entry["all_index_bytes"] += row["index_bytes"]
        if row["from_004"]:
            entry["index_004_bytes"] += row["index_bytes"]
    touched = {k: v for k, v in tables.items() if v["index_004_bytes"]}
    for entry in touched.values():
        entry["ratio_004_to_table"] = round(
            entry["index_004_bytes"] / entry["table_bytes"], 4)
    total_004 = sum(v["index_004_bytes"] for v in touched.values())
    total_heap = sum(v["table_bytes"] for v in touched.values())
    return {"tables": touched, "total_004_bytes": total_004,
            "total_heap_bytes_of_those_tables": total_heap,
            "ratio": round(total_004 / total_heap, 4)}


def _copy_table(connection: sa.Connection, table: str, keep_004: bool) -> None:
    raw = connection.connection.driver_connection
    with raw.cursor() as cursor:
        cursor.execute(pgsql.SQL("CREATE SCHEMA {}").format(
            pgsql.Identifier(SCRATCH_SCHEMA)))
        cursor.execute(pgsql.SQL("CREATE TABLE {} (LIKE {} INCLUDING ALL)").format(
            pgsql.Identifier(SCRATCH_SCHEMA, table), pgsql.Identifier(SCHEMA, table)))
        if keep_004:
            return
        cursor.execute(
            "SELECT i.relname FROM pg_index x "
            "JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_class t ON t.oid = x.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = %s AND t.relname = %s AND NOT x.indisprimary",
            (SCRATCH_SCHEMA, table))
        for (name,) in cursor.fetchall():
            cursor.execute(pgsql.SQL("DROP INDEX {}").format(
                pgsql.Identifier(SCRATCH_SCHEMA, name)))


def write_cost(engine: sa.Engine, *, repeats: int = 3) -> dict:
    """Time INSERT ... SELECT of a whole table into a copy, with vs without 004.

    "Without" keeps only the primary key, i.e. the table as 001 defines it.
    Variants alternate within each repeat so drift in machine load hits both.
    """
    out = {}
    with db.session(engine) as connection:
        for table in WRITE_TABLES:
            ours = [name for name, (t, _) in INDEXES.items() if t == table]
            timings = {"with_004_indexes": [], "without_004_indexes": []}
            rows = None
            insert = pgsql.SQL("INSERT INTO {} SELECT * FROM {}").format(
                pgsql.Identifier(SCRATCH_SCHEMA, table), pgsql.Identifier(SCHEMA, table))
            for _ in range(repeats):
                for variant in timings:
                    connection.begin()
                    try:
                        _copy_table(connection, table,
                                    keep_004=variant == "with_004_indexes")
                        raw = connection.connection.driver_connection
                        with raw.cursor() as cursor:
                            started = perf_counter()
                            cursor.execute(insert)
                            timings[variant].append((perf_counter() - started) * 1000)
                            rows = cursor.rowcount
                    finally:
                        connection.rollback()
            med_with = statistics.median(timings["with_004_indexes"])
            med_without = statistics.median(timings["without_004_indexes"])
            out[table] = {
                "rows_inserted": rows, "indexes_004": ours,
                "with_004_indexes_ms": [round(t, 1) for t in timings["with_004_indexes"]],
                "without_004_indexes_ms": [round(t, 1) for t in timings["without_004_indexes"]],
                "median_with_ms": round(med_with, 1),
                "median_without_ms": round(med_without, 1),
                "slowdown_ratio": round(med_with / med_without, 3),
            }
    return out


# --- snapshot: refresh, view vs snapshot, live-tail sensitivity -------------

CLOSED_MONTHS = sa.text(
    "SELECT date_trunc('month', MIN(order_purchase_timestamp))::date AS first_month, "
    "(date_trunc('month', MAX(order_purchase_timestamp)) - INTERVAL '1 month')::date "
    "AS last_closed_month FROM olist.orders")
REFRESH_RANGE = sa.text(
    "CALL olist.refresh_monthly_category_snapshot_range("
    "CAST(:first_month AS date), CAST(:last_closed_month AS date))")
SNAPSHOT_STATE = sa.text(
    "SELECT COUNT(*) AS snapshot_rows, MIN(month_start) AS first_month, "
    "MAX(month_start) AS last_month, MAX(refreshed_at) AS refreshed_at "
    "FROM olist.monthly_category_snapshot")


def refresh_closed_months(engine: sa.Engine) -> dict:
    """Rebuild every closed month (all months before the newest purchase month).

    This is the job a scheduler would run after each load or each night. One
    transaction: if any month fails, no month changes.
    """
    with engine.begin() as connection:
        bounds = connection.execute(CLOSED_MONTHS).mappings().one()
        started = perf_counter()
        connection.execute(REFRESH_RANGE, dict(bounds))
        elapsed = (perf_counter() - started) * 1000
        state = connection.execute(SNAPSHOT_STATE).mappings().one()
    return {**{k: str(v) for k, v in bounds.items()},
            "refresh_ms": round(elapsed, 1),
            **{k: str(v) if k != "snapshot_rows" else v for k, v in state.items()}}


SNAPSHOT_ONLY = (
    "SELECT month_start AS purchase_month, product_category_name, sales_value, "
    "order_count AS unique_orders, average_order_value "
    "FROM olist.monthly_category_snapshot ORDER BY 1, 2 NULLS LAST")
MATVIEW_ONLY = (
    "SELECT purchase_month, product_category_name, sales_value, unique_orders, "
    "average_order_value FROM olist.mv_q1_monthly_category_sales "
    "ORDER BY 1, 2 NULLS LAST")
SPACE = sa.text(
    "SELECT CAST(:name AS text) AS relation, "
    "pg_total_relation_size(CAST(:name AS regclass)) AS total_bytes")

# A fresh order used to show freshness: a copy of one existing delivered order
# (same customer, product, seller) with a new id, dated in the given month.
FRESH_ORDER = sa.text(
    "WITH src AS (SELECT o.*, i.product_id, i.seller_id FROM olist.orders AS o "
    "JOIN olist.order_items AS i ON i.order_id = o.order_id "
    "WHERE o.order_status = 'delivered' ORDER BY o.order_id LIMIT 1), "
    "new_order AS (INSERT INTO olist.orders SELECT md5('freshness-probe'), "
    "customer_id, 'delivered', CAST(:ts AS timestamp), CAST(:ts AS timestamp), "
    "NULL, NULL, CAST(:ts AS timestamp) + INTERVAL '10 days' FROM src "
    "RETURNING order_id) "
    "INSERT INTO olist.order_items SELECT n.order_id, 1, s.product_id, s.seller_id, "
    "CAST(:ts AS timestamp), 100.00, 0.00 FROM new_order AS n CROSS JOIN src AS s")


def _total(connection: sa.Connection, source: str, month: str) -> str:
    rows = connection.exec_driver_sql(source).mappings().all()
    return str(sum(r["sales_value"] for r in rows
                   if str(r["purchase_month"]) == month))


def view_vs_snapshot(engine: sa.Engine, stage2: Path, *, repeats: int = 15,
                     warmups: int = 3) -> dict:
    """Q1 four ways: live view, snapshot table, materialized view, hybrid."""
    variants = {
        "live_view (sql/baseline/q1 over v_order_item_sales)":
            bench.read_query(stage2 / "sql" / "baseline" / bench.QUERY_FILES[0]),
        "snapshot_table_only": SNAPSHOT_ONLY,
        "materialized_view mv_q1 (007)": MATVIEW_ONLY,
        "hybrid (sql/queries/q1, chosen)":
            bench.read_query(stage2 / "sql" / "queries" / bench.QUERY_FILES[0]),
    }
    timings = {}
    with db.session(engine) as connection:
        for label, source in variants.items():
            timings[label] = _compact(bench.measure(
                connection, source, repeats=repeats, warmups=warmups))
        space = [dict(connection.execute(SPACE, {"name": name}).mappings().one())
                 for name in ("olist.monthly_category_snapshot",
                              "olist.mv_q1_monthly_category_sales")]
        refresh = []
        bounds = dict(connection.execute(CLOSED_MONTHS).mappings().one())
        connection.rollback()
        for _ in range(3):
            connection.begin()
            try:
                started = perf_counter()
                connection.execute(REFRESH_RANGE, bounds)
                refresh.append(round((perf_counter() - started) * 1000, 1))
            finally:
                connection.rollback()
        freshness = {}
        last_closed = str(bounds["last_closed_month"])
        tail = str((bounds["last_closed_month"].replace(day=28)
                    + timedelta(days=4)).replace(day=1))
        for label, month in (("order_in_a_closed_month", last_closed),
                             ("order_in_the_live_tail_month", tail)):
            connection.begin()
            try:
                before = {k: _total(connection, v, month) for k, v in variants.items()}
                connection.execute(FRESH_ORDER, {"ts": f"{month} 12:00:00"})
                after = {k: _total(connection, v, month) for k, v in variants.items()}
            finally:
                connection.rollback()
            freshness[label] = {
                "month": month, "added_item_value": "100.00",
                "sees_new_order": {k: before[k] != after[k] for k in variants},
            }
    return {"timings": timings, "space": space,
            "snapshot_refresh_all_closed_months_ms": refresh,
            "refresh_bounds": {k: str(v) for k, v in bounds.items()},
            "freshness": freshness}


def q1_tail_series(engine: sa.Engine, stage2: Path, *, repeats: int = 9,
                   warmups: int = 2,
                   snapshot_through=("2018-09-01", "2018-08-01", "2018-06-01",
                                     "2018-03-01", "2017-12-01", None)) -> list[dict]:
    """Q1 hybrid time as the snapshot covers fewer months (rolled back each time)."""
    source = bench.read_query(stage2 / "sql" / "queries" / bench.QUERY_FILES[0])
    out = []
    with db.session(engine) as connection:
        for month in snapshot_through:
            connection.begin()
            try:
                if month is None:
                    connection.execute(sa.text(
                        "DELETE FROM olist.monthly_category_snapshot"))
                else:
                    connection.execute(TRIM_SNAPSHOT, {"month": month})
                result = bench.measure(connection, source, repeats=repeats,
                                       warmups=warmups, end_transaction=False)
            finally:
                connection.rollback()
            out.append({"snapshot_through": month or "empty snapshot",
                        "median_ms": result["median_ms"],
                        "max_ms": result["max_ms"],
                        "result_sha256": result["result_sha256"],
                        "scans": result["scans"]})
    return out


EVIDENCE_DIR = Path(__file__).resolve().parents[2] / "data" / "curated" / "evidence"


def save(name: str, payload) -> Path:
    """Write one evidence JSON under data/curated/evidence/ (fixed name, overwritten)."""
    import json

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE_DIR / name
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str) + "\n",
                    encoding="utf-8")
    return path
