"""Real-PostgreSQL tests for the factory sink: opt-in, needs TEST_DATABASE_URL.

Same contract as the Olist integration file -- an empty, disposable database,
each test inside a transaction that is rolled back, and a skip (not a pass)
when no URL is configured. These are the only tests that see the CHECK
constraints, the CHAR(7) month label, NUMERIC scale and the view's
PERCENTILE_CONT, because SQLite accepts none of them.

    export TEST_DATABASE_URL='postgresql+psycopg://kiarash@localhost:5432/olist_stage2_test'
    python -m unittest discover -s tests -p 'test_factory_sink_postgres.py' -v
"""
import dataclasses
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
STAGE1_SRC = Path(__file__).resolve().parents[2] / "stage-1-kiarash-akbari" / "src"
STAGE1_TESTS = Path(__file__).resolve().parents[2] / "stage-1-kiarash-akbari" / "tests"
for path in (str(STAGE1_SRC), str(STAGE1_TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import sqlalchemy as sa  # noqa: E402

from config_helpers import make_config  # noqa: E402
from data_factory.clean import CLEAN_COLUMNS  # noqa: E402
from data_factory.config import DatabaseConfig  # noqa: E402
from data_factory.database import (  # noqa: E402
    SINK_CASE_TABLE,
    SINK_CONFLICT_TABLE,
    SINK_META_TABLE,
    SINK_PROVENANCE_TABLE,
    SINK_QUALITY_TABLE,
    SINK_WIDE_TABLE,
    load_stage1_sink,
)
from data_factory.errors import DataError  # noqa: E402
from data_factory.outputs import DATASET_FILENAME, WIDE_FILENAME, sha256_file  # noqa: E402
from data_factory.profile import REPORT_FILENAME  # noqa: E402

DDL = (SQL_DIR / "009_factory_sink_schema.sql").read_text(encoding="utf-8")
TEST_URL = os.environ.get("TEST_DATABASE_URL")

CASE_ROWS = [
    {"unique_key": "11", "created_at": "2025-01-03T00:57:07",
     "closed_at": "2025-01-03T01:30:07", "duration_seconds": "1980",
     "is_ambiguous": "false", "modzcta": "10001", "population": "1000"},
    {"unique_key": "12", "created_at": "2025-01-11T21:15:00",
     "closed_at": "", "duration_seconds": "", "is_ambiguous": "true",
     "modzcta": "", "population": ""},
    {"unique_key": "13", "created_at": "2025-02-01T09:00:00",
     "closed_at": "2025-02-01T09:00:00", "duration_seconds": "0",
     "is_ambiguous": "false", "modzcta": "10001", "population": "1000"},
]


def case_line(**overrides) -> str:
    row = dict.fromkeys(CLEAN_COLUMNS, "")
    row.update({
        "agency": "NYPD",
        "complaint_type": "Noise - Residential",
        "descriptor": "Loud Music/Party",
        "status": "Closed",
        "borough": "QUEENS",
        "community_board": "4",
        "incident_zip": "11354",
    })
    row.update(overrides)
    return ",".join(str(row[column]) for column in CLEAN_COLUMNS)


def write_outputs(curated: Path, case_rows=None, wide_body=None, report=None) -> Path:
    case_rows = CASE_ROWS if case_rows is None else case_rows
    lines = [",".join(CLEAN_COLUMNS)] + [case_line(**row) for row in case_rows]
    (curated / DATASET_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")

    wide_header = ",".join([
        "modzcta", "population", "complaint_type",
        "count_2025-01", "count_2025-02",
        "rate_per_10k_2025-01", "rate_per_10k_2025-02",
    ])
    wide_body = (
        "10001,1000,Noise - Residential,3,1,30.0000,10.0000\n"
        "UNKNOWN,,Noise - Residential,2,4,,40.0000\n"
    ) if wide_body is None else wide_body
    (curated / WIDE_FILENAME).write_text(wide_header + "\n" + wide_body, encoding="utf-8")

    if report is None:
        stats = {"rows": len(case_rows), "null_count": 1, "null_percent": 33.3333,
                 "distinct_values": len(case_rows), "distinct_capped": False,
                 "min": "11", "max": "13"}
        report = {
            "distinct_cap": 10000,
            "columns": {"unique_key": stats, "closed_at": dict(stats, min=None)},
            "borough_modzcta_conflicts": [
                {"modzcta": "10001", "boroughs": ["QUEENS", "BROOKLYN"]},
            ],
            "borough_modzcta_conflict_count": 1,
        }
    (curated / REPORT_FILENAME).write_text(json.dumps(report), encoding="utf-8")
    return curated


@unittest.skipUnless(TEST_URL, "TEST_DATABASE_URL is not set")
class FactorySinkPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = sa.create_engine(TEST_URL)
        if cls.engine.dialect.name != "postgresql":
            raise unittest.SkipTest("the factory sink schema is PostgreSQL DDL")

    def setUp(self):
        self.conn = self.engine.connect()
        self.addCleanup(self.conn.close)
        self.tx = self.conn.begin()
        self.addCleanup(self.tx.rollback)
        existing = self.conn.scalar(
            sa.text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'factory')")
        )
        if existing:
            self.fail("Refusing to run: the factory schema already exists; use a disposable database")
        self.conn.execute(sa.text(DDL))

        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.curated = Path(self._folder.name) / "curated"
        self.curated.mkdir()
        self.cfg = dataclasses.replace(
            make_config(self.curated),
            database=DatabaseConfig(
                url=TEST_URL,
                password_env="ABSENT_FOR_TEST",
                olist_csv_dir=self.curated / "csv",
                stage1_curated_dir=self.curated,
                batch_size=2,
                sql_migrations_dir=SQL_DIR,
            ),
        )

    # -- helpers ---------------------------------------------------------

    def load(self, **kwargs):
        write_outputs(self.curated, **kwargs)
        return load_stage1_sink(self.cfg, connection=self.conn)

    def rows(self, table):
        return self.conn.execute(
            sa.text(f"SELECT * FROM factory.{table} ORDER BY 1, 2, 3")
        ).mappings().all()

    def scalar(self, sql, **params):
        return self.conn.execute(sa.text(sql), params).scalar()

    def assertRejected(self, sql, constraint, **params):
        savepoint = self.conn.begin_nested()
        with self.assertRaises(sa.exc.IntegrityError) as ctx:
            self.conn.execute(sa.text(sql), params)
        self.assertIn(constraint, str(ctx.exception.orig))
        savepoint.rollback()

    # -- schema ----------------------------------------------------------

    def test_all_six_sink_tables_exist_after_009(self):
        found = set(sa.inspect(self.conn).get_table_names(schema="factory"))
        self.assertEqual(found, {SINK_CASE_TABLE, SINK_WIDE_TABLE, SINK_QUALITY_TABLE,
                                SINK_META_TABLE, SINK_CONFLICT_TABLE,
                                SINK_PROVENANCE_TABLE})
        views = set(sa.inspect(self.conn).get_view_names(schema="factory"))
        self.assertEqual(views, {"v_median_duration_by_type"})

    def test_timestamp_columns_keep_the_delivered_naive_type(self):
        stored = self.scalar(
            """SELECT format_type(atttypid, atttypmod) FROM pg_attribute
               WHERE attrelid = 'factory.case_file'::regclass AND attname = 'created_at'"""
        )
        self.assertEqual(stored, "timestamp without time zone")

    # -- loading ---------------------------------------------------------

    def test_load_counts_match_the_fixture_files(self):
        results = self.load()
        self.assertEqual(results[SINK_CASE_TABLE]["loaded"], 3)
        self.assertEqual(results[SINK_CASE_TABLE]["raw"], 3)
        self.assertEqual(results[SINK_WIDE_TABLE]["loaded"], 4, "two wide rows, two months")
        self.assertEqual(results[SINK_WIDE_TABLE]["months"], 2)
        self.assertEqual(results[SINK_QUALITY_TABLE]["loaded"], 2)
        self.assertEqual(results[SINK_QUALITY_TABLE]["conflicts"], 1)
        self.assertEqual(results[SINK_PROVENANCE_TABLE]["loaded"], 3)

    def test_stage1_boolean_text_lands_as_a_real_boolean(self):
        self.load()
        values = {
            row["is_ambiguous"]
            for row in self.conn.execute(
                sa.text("SELECT is_ambiguous FROM factory.case_file")
            ).mappings()
        }
        self.assertEqual(values, {True, False})
        self.assertTrue(all(isinstance(value, bool) for value in values))

    def test_naive_timestamps_are_not_shifted(self):
        self.load()
        created = self.scalar(
            "SELECT created_at FROM factory.case_file WHERE unique_key = 11"
        )
        self.assertEqual(created, datetime(2025, 1, 3, 0, 57, 7))
        closed = self.scalar(
            "SELECT closed_at FROM factory.case_file WHERE unique_key = 12"
        )
        self.assertIsNone(closed, "an open case stays NULL")

    def test_numeric_rate_keeps_its_scale_and_its_null(self):
        self.load()
        rows = {
            row["month_start"]: row
            for row in self.conn.execute(sa.text(
                "SELECT * FROM factory.complaint_area_month WHERE modzcta = '10001'"
            )).mappings()
        }
        self.assertEqual(rows["2025-01"]["rate_per_10k"], Decimal("30.0000"))
        self.assertEqual(str(rows["2025-01"]["rate_per_10k"]), "30.0000")
        unknown = {
            row["month_start"]: row["rate_per_10k"]
            for row in self.conn.execute(sa.text(
                "SELECT * FROM factory.complaint_area_month WHERE modzcta = 'UNKNOWN'"
            )).mappings()
        }
        self.assertIsNone(unknown["2025-01"], "a delivered empty rate is unknown, not zero")
        self.assertEqual(unknown["2025-02"], Decimal("40.0000"))

    def test_unknown_area_is_a_storable_bucket(self):
        self.load()
        wide = self.rows(SINK_WIDE_TABLE)
        self.assertIn("UNKNOWN", {row["modzcta"] for row in wide})
        self.assertIsNone(self.scalar(
            "SELECT population FROM factory.complaint_area_month WHERE modzcta = 'UNKNOWN'"
        ))
        self.assertIsNone(self.scalar(
            "SELECT modzcta FROM factory.case_file WHERE unique_key = 12"
        ))

    def test_provenance_hash_matches_the_bytes_on_disk(self):
        self.load()
        for table, name in (("case_file", DATASET_FILENAME),
                            ("complaint_area_month", WIDE_FILENAME),
                            ("column_quality", REPORT_FILENAME)):
            with self.subTest(table=table):
                row = self.conn.execute(
                    sa.text("SELECT file_name, sha256, rows_loaded FROM factory.load_run "
                            "WHERE sink_table = :t"),
                    {"t": table},
                ).mappings().one()
                self.assertEqual(row["file_name"], name)
                self.assertEqual(row["sha256"], sha256_file(self.curated / name))
                self.assertGreaterEqual(row["rows_loaded"], 1)
        self.assertIsNotNone(self.scalar(
            "SELECT loaded_at FROM factory.load_run WHERE sink_table = 'case_file'"
        ))

    def test_a_second_load_converges_instead_of_appending(self):
        first = self.load()
        before = {table: len(self.rows(table)) for table in
                  (SINK_CASE_TABLE, SINK_WIDE_TABLE, SINK_QUALITY_TABLE)}
        second = self.load()
        after = {table: len(self.rows(table)) for table in before}
        self.assertEqual(before, after, "truncate-then-insert must not double the rows")
        self.assertEqual(first[SINK_CASE_TABLE]["loaded"], second[SINK_CASE_TABLE]["loaded"])
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM factory.load_run"), 3,
            "one provenance row per input file, not one per run",
        )

    def test_a_failure_leaves_the_previous_rows_exactly_as_they_were(self):
        self.load()
        before = {table: len(self.rows(table)) for table in
                  (SINK_CASE_TABLE, SINK_WIDE_TABLE, SINK_QUALITY_TABLE)}
        broken = dict(CASE_ROWS[0])
        write_outputs(self.curated, case_rows=[broken, dict(CASE_ROWS[1], is_ambiguous="maybe")])
        savepoint = self.conn.begin_nested()
        with self.assertRaises(DataError) as ctx:
            load_stage1_sink(self.cfg, connection=self.conn)
        self.assertEqual(ctx.exception.context["column"], "is_ambiguous")
        savepoint.rollback()
        after = {table: len(self.rows(table)) for table in before}
        self.assertEqual(before, after, "the truncated state must not survive the failure")

    # -- constraints -----------------------------------------------------

    def test_named_constraints_reject_bad_rows(self):
        self.load()
        cases = [
            ("INSERT INTO factory.case_file (unique_key, created_at, is_ambiguous, agency, "
             "complaint_type, status, borough, incident_zip) VALUES "
             "(99, '2025-01-01T00:00:00', false, 'NYPD', 'Noise', 'Closed', 'QUEENS', '1234')",
             "ck_factory_case_file_zip"),
            ("INSERT INTO factory.case_file (unique_key, created_at, is_ambiguous, agency, "
             "complaint_type, status, borough, closed_at) VALUES "
             "(98, '2025-01-01T00:00:00', false, 'NYPD', 'Noise', 'Closed', 'QUEENS', "
             "'2024-12-31T23:59:59')", "ck_factory_case_file_order"),
            ("INSERT INTO factory.complaint_area_month (modzcta, complaint_type, month_start, "
             "complaint_count) VALUES ('10001', 'Noise', '2025-03', -1)",
             "ck_factory_complaint_area_month_count"),
            # A 10-char label would be a truncation error, not a constraint
            # violation, so the bad value uses the length the column allows.
            ("INSERT INTO factory.complaint_area_month (modzcta, complaint_type, month_start, "
             "complaint_count) VALUES ('10001', 'Noise', '2025-13', 1)",
             "ck_factory_complaint_area_month_month"),
            ("INSERT INTO factory.complaint_area_month (modzcta, complaint_type, month_start, "
             "complaint_count) VALUES ('1000', 'Noise', '2025-03', 1)",
             "ck_factory_complaint_area_month_area"),
            ("INSERT INTO factory.quality_meta (id, distinct_cap, conflict_count) "
             "VALUES (2, 100, 0)", "ck_factory_quality_meta_single"),
        ]
        for sql, constraint in cases:
            with self.subTest(constraint=constraint):
                self.assertRejected(sql, constraint)

    def test_primary_keys_reject_duplicates(self):
        self.load()
        self.assertRejected(
            "INSERT INTO factory.complaint_area_month (modzcta, complaint_type, month_start, "
            "complaint_count) VALUES ('10001', 'Noise - Residential', '2025-01', 9)",
            "pk_factory_complaint_area_month",
        )
        self.assertRejected(
            "INSERT INTO factory.column_quality (column_name, rows_total, null_count, "
            "null_percent, distinct_capped) VALUES ('unique_key', 1, 0, 0, false)",
            "pk_factory_column_quality",
        )

    def test_quality_meta_is_a_single_row_table(self):
        self.load()
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM factory.quality_meta"), 1)
        self.assertEqual(self.scalar("SELECT distinct_cap FROM factory.quality_meta"), 10000)
        self.assertRejected(
            "INSERT INTO factory.quality_meta (id, distinct_cap, conflict_count) "
            "VALUES (1, 10, 0)", "pk_factory_quality_meta",
        )

    def test_conflicts_are_joinable_and_agree_with_the_report(self):
        self.load()
        pairs = self.conn.execute(sa.text(
            """SELECT b.modzcta, b.borough, c.borough AS from_cases
               FROM factory.borough_conflict b
               JOIN factory.case_file c ON c.modzcta = b.modzcta
               ORDER BY b.borough, c.unique_key"""
        )).mappings().all()
        self.assertEqual({(row["modzcta"], row["borough"]) for row in pairs},
                         {("10001", "BROOKLYN"), ("10001", "QUEENS")})
        self.assertEqual(self.scalar("SELECT conflict_count FROM factory.quality_meta"),
                         self.scalar(f"SELECT COUNT(*) FROM factory.{SINK_CONFLICT_TABLE}"))

    # -- the view --------------------------------------------------------

    def test_view_computes_the_median_the_stage_exists_for(self):
        self.load()
        rows = self.conn.execute(sa.text(
            """SELECT complaint_type, status, cases_with_duration, median_seconds
               FROM factory.v_median_duration_by_type ORDER BY status"""
        )).mappings().all()
        closed = [row for row in rows if row["status"] == "Closed"]
        self.assertEqual(len(rows), 1, "the open case has no duration to aggregate")
        self.assertEqual(rows[0]["status"], "Closed")
        self.assertEqual(closed[0]["cases_with_duration"], 2)
        self.assertEqual(closed[0]["median_seconds"], 990.0, "(0 + 1980) / 2")