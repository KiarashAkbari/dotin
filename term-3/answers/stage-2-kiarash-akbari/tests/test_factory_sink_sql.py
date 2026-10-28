"""Text-level contract tests for 009_factory_sink_schema.sql.

No database is touched here: these tests hold the DDL to the promises the
loader depends on, so a schema edit that would break a load fails in the
offline suite instead of during a delivery run.
"""
import re
import sys
import unittest
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
STAGE1_SRC = Path(__file__).resolve().parents[2] / "stage-1-kiarash-akbari" / "src"
if str(STAGE1_SRC) not in sys.path:
    sys.path.insert(0, str(STAGE1_SRC))

from data_factory.cli import (  # noqa: E402
    _is_base_migration,
    _is_sink_migration,
    _is_snapshot_migration,
)
from data_factory.clean import CLEAN_COLUMNS  # noqa: E402
from data_factory.database import (  # noqa: E402
    FACTORY_SCHEMA,
    NARROW_COLUMNS,
    PROFILE_KEYS,
    SINK_CASE_TABLE,
    SINK_CONFLICT_TABLE,
    SINK_META_TABLE,
    SINK_PROVENANCE_TABLE,
    SINK_QUALITY_TABLE,
    SINK_TABLES,
    SINK_WIDE_TABLE,
    sql_migration_files,
)

MIGRATION = SQL_DIR / "009_factory_sink_schema.sql"
DDL = MIGRATION.read_text(encoding="utf-8")
TABLES = {
    name: body
    for name, body in re.findall(
        r"CREATE TABLE IF NOT EXISTS factory\.(\w+) \((.*?)\n\);", DDL, re.DOTALL
    )
}


def column_names(block: str) -> list[str]:
    """Column names of a table block, in declaration order.

    Four-space lines carry columns and the first line of a constraint; a
    multi-line constraint continues at eight spaces and its closing paren sits
    at four again, so both the indent and the leading keywords are checked.
    """
    names = []
    for line in block.splitlines():
        if not line.startswith("    ") or line.startswith("     "):
            continue
        text = line.strip()
        if not text or text.startswith(("CONSTRAINT", "--", ")")):
            continue
        names.append(text.split()[0])
    return names


class MigrationDiscoveryTests(unittest.TestCase):
    def test_009_is_discovered_last_and_only_by_the_sink_group(self):
        files = sql_migration_files(SQL_DIR)
        self.assertIn(MIGRATION.name, [path.name for path in files])
        self.assertEqual(files[-1].name, MIGRATION.name)
        for path in files:
            sink = _is_sink_migration(path)
            self.assertFalse(sink and _is_base_migration(path), path.name)
            self.assertFalse(sink and _is_snapshot_migration(path), path.name)
        self.assertIn("009_factory_sink_schema.sql", MIGRATION.name)

    def test_rejected_experiment_keeps_its_number(self):
        """004 belongs to sql/rejected and stays out of the applied chain."""
        self.assertFalse((SQL_DIR / "004_factory_sink_schema.sql").exists())
        self.assertFalse(any(path.name.startswith("004") for path in sql_migration_files(SQL_DIR)))
        self.assertTrue((SQL_DIR / "rejected" / "004_rejected_orders_cover.sql").exists())


class LoaderAgreementTests(unittest.TestCase):
    def test_every_table_the_loader_names_is_created(self):
        self.assertEqual(
            sorted(TABLES),
            sorted({SINK_CASE_TABLE, SINK_WIDE_TABLE, SINK_QUALITY_TABLE,
                    SINK_META_TABLE, SINK_CONFLICT_TABLE, SINK_PROVENANCE_TABLE}),
        )

    def test_case_file_mirrors_the_delivered_columns_exactly(self):
        self.assertEqual(column_names(TABLES[SINK_CASE_TABLE]), list(CLEAN_COLUMNS))

    def test_wide_table_is_stored_narrow(self):
        self.assertEqual(column_names(TABLES[SINK_WIDE_TABLE]), list(NARROW_COLUMNS))
        self.assertRegex(
            TABLES[SINK_WIDE_TABLE],
            r"PRIMARY KEY \(modzcta, complaint_type, month_start\)",
        )

    def test_quality_columns_cover_the_report_stats(self):
        """Three names are storage-specific; the rest are the report's own keys."""
        stored = set(column_names(TABLES[SINK_QUALITY_TABLE]))
        mapped = {"min": "min_value", "max": "max_value", "rows": "rows_total"}
        for key in PROFILE_KEYS:
            self.assertIn(mapped.get(key, key), stored, key)
        self.assertIn("column_name", stored)


class ColumnTypeTests(unittest.TestCase):
    def test_case_timestamps_are_naive(self):
        """New York wall-clock text stays naive; loaded_at is the exception."""
        block = TABLES[SINK_CASE_TABLE]
        self.assertRegex(block, r"created_at\s+TIMESTAMP\s+NOT NULL")
        self.assertRegex(block, r"closed_at\s+TIMESTAMP,")
        self.assertNotIn("WITH TIME ZONE", block)

    def test_load_run_records_utc_and_a_full_hash(self):
        block = TABLES[SINK_PROVENANCE_TABLE]
        self.assertIn("loaded_at   TIMESTAMPTZ NOT NULL", block)
        self.assertIn("sha256 ~ '^[0-9a-f]{64}$'", block)
        self.assertRegex(block, r"PRIMARY KEY \(sink_table\)")

    def test_month_start_is_a_month_label_not_a_day(self):
        self.assertRegex(
            TABLES[SINK_WIDE_TABLE],
            r"month_start\s+CHAR\(7\)\s+NOT NULL",
        )
        self.assertIn("'^[0-9]{4}-(0[1-9]|1[0-2])$'", TABLES[SINK_WIDE_TABLE])


class SentinelTests(unittest.TestCase):
    def test_unknown_area_is_a_legal_wide_row(self):
        self.assertIn(
            "modzcta ~ '^([0-9]{5}|UNKNOWN)$'",
            TABLES[SINK_WIDE_TABLE],
        )
        self.assertEqual(
            re.search(r"modzcta\s+(\S+)", TABLES[SINK_WIDE_TABLE]).group(1),
            "TEXT",
            "a VARCHAR(5) would be the same length as UNKNOWN by luck, not by rule",
        )

    def test_case_file_keeps_the_five_digit_shape(self):
        block = TABLES[SINK_CASE_TABLE]
        self.assertRegex(block, r"modzcta\s+VARCHAR\(5\)")
        self.assertIn("incident_zip ~ '^[0-9]{5}$'", block)

    def test_the_sink_declares_no_foreign_keys(self):
        """Reconciliation is a query's job; a CASCADE would delete audit rows."""
        self.assertNotIn("REFERENCES", DDL)


class ConstraintHygieneTests(unittest.TestCase):
    def test_every_constraint_is_named_and_unique(self):
        names = re.findall(r"CONSTRAINT (\w+)", DDL)
        self.assertEqual(len(names), len(set(names)), "duplicate constraint names")
        self.assertTrue(all(name.startswith(("pk_factory_", "ck_factory_")) for name in names))
        self.assertGreaterEqual(len(names), 15)
        self.assertEqual(DDL.count("CHECK ("), sum(1 for n in names if n.startswith("ck_factory_")))

    def test_ddl_is_idempotent_and_holds_no_dml(self):
        self.assertEqual(DDL.count("CREATE TABLE IF NOT EXISTS"), len(TABLES))
        self.assertIn("CREATE OR REPLACE VIEW", DDL)
        self.assertNotIn("DROP ", DDL)
        for verb in ("INSERT INTO", "UPDATE factory", "DELETE FROM", "COPY "):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, DDL)

    def test_the_loader_owns_the_transaction(self):
        for keyword in ("BEGIN", "COMMIT", "ROLLBACK"):
            with self.subTest(keyword=keyword):
                self.assertIsNone(re.search(rf"^\s*{keyword}\s*;", DDL, re.MULTILINE))

    def test_no_performance_indexes_exist_yet(self):
        self.assertIsNone(re.search(r"CREATE (UNIQUE )?INDEX", DDL))
        self.assertIn("No performance indexes", DDL)


class ViewTests(unittest.TestCase):
    def test_view_reads_the_sink_and_filters_before_aggregating(self):
        view = DDL.split("CREATE OR REPLACE VIEW", 1)[1]
        self.assertIn(f"FROM {FACTORY_SCHEMA}.{SINK_CASE_TABLE}", view)
        self.assertIn("PERCENTILE_CONT(0.5)", view)
        self.assertIn("WHERE duration_seconds IS NOT NULL", view)
        self.assertNotIn("LIMIT", view)

    def test_only_one_view_is_defined(self):
        self.assertEqual(DDL.count("CREATE OR REPLACE VIEW"), 1)
        self.assertEqual(
            re.findall(r"CREATE OR REPLACE VIEW factory\.(\w+)", DDL),
            ["v_median_duration_by_type"],
        )


class ProvenanceContractTests(unittest.TestCase):
    def test_three_input_files_produce_the_sink(self):
        self.assertEqual(
            SINK_TABLES,
            {SINK_CASE_TABLE: "cases.csv",
             SINK_WIDE_TABLE: "complaints_by_area_month.csv",
             SINK_QUALITY_TABLE: "data_quality_report.json"},
        )

    def test_quality_meta_is_a_single_row_table(self):
        self.assertIn("CONSTRAINT ck_factory_quality_meta_single CHECK (id = 1)",
                      TABLES[SINK_META_TABLE])
        self.assertEqual(column_names(TABLES[SINK_META_TABLE]),
                         ["id", "distinct_cap", "conflict_count"])


if __name__ == "__main__":
    unittest.main()
