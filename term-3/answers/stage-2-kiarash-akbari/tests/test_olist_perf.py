"""Offline tests for src/olist_perf plus a few PostgreSQL checks (TEST_DATABASE_URL)."""
import ast
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pandas as pd
import sqlalchemy as sa

STAGE2 = Path(__file__).resolve().parents[1]
for path in (STAGE2 / "src", STAGE2.parent / "stage-1-kiarash-akbari" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from olist_perf import bench, bridge, db, evidence  # noqa: E402

PLAN = {
    "Plan": {
        "Node Type": "Aggregate", "Plan Rows": 4, "Actual Rows": 4,
        "Actual Loops": 1, "Total Cost": 10.0, "Actual Total Time": 2.0,
        "Plans": [
            {"Node Type": "Seq Scan", "Relation Name": "orders",
             "Plan Rows": 9, "Actual Rows": 9, "Actual Loops": 1},
            {"Node Type": "Index Only Scan", "Relation Name": "order_reviews",
             "Index Name": "ix_order_reviews_order_score",
             "Plan Rows": 9, "Actual Rows": 9, "Actual Loops": 1},
        ],
    },
}


class DigestTests(unittest.TestCase):
    def test_float_noise_below_twelve_digits_does_not_change_the_hash(self):
        a = bench.result_digest([(1, 0.27412345678901234)])
        b = bench.result_digest([(1, 0.27412345678901299)])
        self.assertEqual(a, b)

    def test_a_real_difference_changes_the_hash(self):
        self.assertNotEqual(bench.result_digest([(1, Decimal("1.5"))]),
                            bench.result_digest([(1, Decimal("1.6"))]))

    def test_decimal_scale_does_not_matter(self):
        self.assertEqual(bench.result_digest([(Decimal("2.50"),)]),
                         bench.result_digest([(Decimal("2.5"),)]))

    def test_row_order_matters(self):
        self.assertNotEqual(bench.result_digest([(1,), (2,)]),
                            bench.result_digest([(2,), (1,)]))


class PlanTests(unittest.TestCase):
    def test_plan_is_flattened_with_depth(self):
        nodes = bench.plan_nodes(PLAN)
        self.assertEqual([n["depth"] for n in nodes], [0, 1, 1])
        self.assertEqual(nodes[2]["index"], "ix_order_reviews_order_score")

    def test_scan_summary_separates_seq_and_index_scans(self):
        self.assertEqual(bench.scan_summary(PLAN), {
            "seq_scan": ["orders"], "index_scan": ["ix_order_reviews_order_score"]})

    def test_measure_rejects_too_few_runs(self):
        with self.assertRaises(ValueError):
            bench.measure(mock.Mock(), "SELECT 1", repeats=2)

    def test_query_files_are_read_without_the_trailing_semicolon(self):
        for name in bench.QUERY_FILES:
            for folder in ("queries", "baseline"):
                with self.subTest(folder=folder, query=name):
                    text = bench.read_query(STAGE2 / "sql" / folder / name)
                    self.assertFalse(text.endswith(";"))
                    self.assertNotIn(";", text.split("--")[-1].strip()[-1:])


class ConfigTests(unittest.TestCase):
    def test_olist_config_overrides_the_default_path(self):
        with mock.patch.dict(os.environ, {db.CONFIG_ENV: "cfg/x.toml"}):
            self.assertEqual(db.config_path(), (STAGE2 / "cfg" / "x.toml").resolve())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(db.config_path(),
                             db.STAGE1_DIR / "config" / "default.toml")

    def test_missing_password_fails_fast_instead_of_prompting(self):
        cfg = db.STAGE1_DIR / "config" / "default.toml"
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "OLIST_DB_PASSWORD"):
                db.make_engine(cfg)

    def test_session_settings_never_touch_the_server_configuration(self):
        for path in (STAGE2 / "src" / "olist_perf").glob("*.py"):
            code = "\n".join(line.split("#", 1)[0]
                             for line in path.read_text(encoding="utf-8").splitlines())
            with self.subTest(file=path.name):
                self.assertNotIn("ALTER SYSTEM", code)
        self.assertEqual(db.SESSION_SETTINGS["max_parallel_workers_per_gather"], "0")


class EvidenceTables(unittest.TestCase):
    def test_every_004_index_is_in_the_evidence_map_and_the_migration(self):
        ddl = (STAGE2 / "sql" / "004_performance_indexes.sql").read_text(encoding="utf-8")
        created = set(__import__("re").findall(r"CREATE INDEX IF NOT EXISTS (\w+)", ddl))
        self.assertEqual(created, set(evidence.INDEXES))
        for name, (_table, query) in evidence.INDEXES.items():
            with self.subTest(index=name):
                self.assertIn(name, (STAGE2 / "sql" / "queries" / query).read_text(
                    encoding="utf-8") + ddl)

    def test_rejected_index_is_documented_and_not_applied(self):
        ddl = (STAGE2 / "sql" / "004_performance_indexes.sql").read_text(encoding="utf-8")
        self.assertNotRegex(ddl, r"CREATE INDEX[^;]*ix_orders_delivered_partial")
        self.assertIn("CREATE INDEX ix_orders_delivered_partial", evidence.REJECTED_INDEX_DDL)
        self.assertTrue(any("ix_orders_delivered_partial" in p.read_text(encoding="utf-8")
                            for p in (STAGE2 / "sql" / "rejected").glob("*.sql")))

    def test_size_summary_ratio(self):
        rows = [
            {"table_name": "t", "index_name": "pk", "index_bytes": 10,
             "table_bytes": 100, "from_004": False},
            {"table_name": "t", "index_name": "ix", "index_bytes": 30,
             "table_bytes": 100, "from_004": True},
            {"table_name": "u", "index_name": "pk", "index_bytes": 5,
             "table_bytes": 50, "from_004": False},
        ]
        summary = evidence.size_summary(rows)
        self.assertEqual(summary["total_004_bytes"], 30)
        self.assertEqual(summary["ratio"], 0.3)
        self.assertNotIn("u", summary["tables"])

    def test_export_dtypes_are_explicit_sqlalchemy_types(self):
        self.assertEqual(set(bridge.Q2_EXPORT_DTYPES), {
            "purchase_month", "customer_state", "delivered_orders", "late_orders",
            "late_ratio"})
        for value in bridge.Q2_EXPORT_DTYPES.values():
            self.assertIsInstance(value, sa.types.TypeEngine)

    def test_write_typed_rejects_a_frame_without_the_columns(self):
        with self.assertRaises(ValueError):
            bridge.write_typed(mock.Mock(), pd.DataFrame({"x": [1]}))


class NoStringBuiltSqlTests(unittest.TestCase):
    """Spec 8: no query text built by f-strings, % or + in the package."""

    def test_no_fstring_or_concatenation_reaches_execute(self):
        sql_words = ("SELECT", "INSERT", "DELETE", "CREATE", "DROP", "CALL", "EXPLAIN")
        for path in (STAGE2 / "src" / "olist_perf").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.JoinedStr):
                    text = "".join(v.value for v in node.values
                                   if isinstance(v, ast.Constant) and isinstance(v.value, str))
                    with self.subTest(file=path.name, line=node.lineno):
                        self.assertFalse(any(w in text.upper().split() for w in sql_words))
                if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
                    for side in (node.left, node.right):
                        if isinstance(side, ast.Constant) and isinstance(side.value, str):
                            with self.subTest(file=path.name, line=node.lineno):
                                self.assertFalse(any(
                                    side.value.upper().lstrip().startswith(w)
                                    for w in sql_words))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class BridgePostgresTests(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine(
            os.environ["TEST_DATABASE_URL"],
            connect_args={"application_name": db.APPLICATION_NAME})
        with self.engine.begin() as connection:
            connection.execute(sa.text("CREATE SCHEMA IF NOT EXISTS reporting"))
        self.addCleanup(self._drop)

    def _drop(self):
        with self.engine.begin() as connection:
            connection.execute(sa.text(
                "DROP TABLE IF EXISTS reporting.delivery_late_ratio_by_state_month"))
        self.engine.dispose()

    def test_to_sql_stores_our_types_not_pandas_defaults(self):
        frame = pd.DataFrame({
            "purchase_month": pd.to_datetime(["2018-01-01"]).date,
            "customer_state": ["SP"], "delivered_orders": [3], "late_orders": [1],
            "late_ratio": [1 / 3],
        })
        stored = bridge.write_typed(self.engine, frame)
        types = dict(zip(stored["column_name"], stored["data_type"]))
        self.assertEqual(types, {
            "purchase_month": "date", "customer_state": "character",
            "delivered_orders": "integer", "late_orders": "integer",
            "late_ratio": "numeric"})
        self.assertEqual(stored.attrs["rows_written"], 1)
        with self.engine.connect() as connection:
            value = connection.scalar(sa.text(
                "SELECT late_ratio FROM reporting.delivery_late_ratio_by_state_month"))
        self.assertEqual(value, Decimal("0.3333"))

    def test_no_session_is_left_after_dispose(self):
        with self.engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
        self.engine.dispose()
        self.assertEqual(db.leftover_sessions(self.engine), [])


if __name__ == "__main__":
    unittest.main()
