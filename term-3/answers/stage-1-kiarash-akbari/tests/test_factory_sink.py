"""Offline tests for the Stage 1 database sink: no PostgreSQL, no network."""
import dataclasses
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from config_helpers import make_config
from data_factory.clean import CLEAN_COLUMNS
from data_factory.cli import (
    _is_base_migration,
    _is_sink_migration,
    _is_snapshot_migration,
    build_parser,
)
from data_factory.config import DatabaseConfig
from data_factory.database import (
    NARROW_COLUMNS,
    SINK_CASE_TABLE,
    SINK_CONFLICT_TABLE,
    SINK_META_TABLE,
    SINK_PROVENANCE_TABLE,
    SINK_QUALITY_TABLE,
    SINK_TABLES,
    SINK_WIDE_TABLE,
    _convert,
    factory_sink_paths,
    load_csv,
    load_stage1_sink,
    narrow_wide_rows,
    parse_quality_report,
    sink_months,
    truncate_factory,
)
from data_factory.errors import DataError
from data_factory.outputs import DATASET_FILENAME, WIDE_FILENAME
from data_factory.profile import REPORT_FILENAME

import sqlalchemy as sa

MONTHS = ("2025-01", "2025-02")


def wide_header(months=MONTHS):
    return (
        ["modzcta", "population", "complaint_type"]
        + [f"count_{month}" for month in months]
        + [f"rate_per_10k_{month}" for month in months]
    )


def wide_row(area="10001", ctype="Noise - Residential", population="1000", counts=None,
             rates=None):
    counts = {"2025-01": "3", "2025-02": "0"} if counts is None else counts
    rates = {"2025-01": "30.0000", "2025-02": ""} if rates is None else rates
    row = {
        "modzcta": area,
        "population": population,
        "complaint_type": ctype,
    }
    for month in MONTHS:
        row[f"count_{month}"] = counts.get(month)
        row[f"rate_per_10k_{month}"] = rates.get(month)
    return row


class WideHeaderContractTests(unittest.TestCase):
    def test_months_come_from_the_header(self):
        self.assertEqual(list(sink_months(wide_header())), list(MONTHS))

    def test_rejects_missing_or_duplicated_count_columns(self):
        for header in (["modzcta", "complaint_type"],
                       wide_header() + ["count_2025-01"]):
            with self.subTest(header=header[-1]), self.assertRaises(DataError) as ctx:
                sink_months(header)
            self.assertIn("counts", ctx.exception.context)

    def test_rejects_rate_and_count_sets_that_differ(self):
        header = wide_header()
        header.remove("rate_per_10k_2025-02")
        with self.assertRaises(DataError) as ctx:
            sink_months(header)
        self.assertEqual(
            (ctx.exception.context["counts"], ctx.exception.context["rates"]),
            (list(MONTHS), ["2025-01"]),
        )

    def test_rejects_month_label_that_is_not_a_month(self):
        header = ["modzcta", "count_2025-13", "rate_per_10k_2025-13"]
        with self.assertRaises(DataError):
            sink_months(header)


class UnpivotTests(unittest.TestCase):
    def test_one_wide_row_becomes_one_row_per_month(self):
        rows = list(narrow_wide_rows(wide_row(), MONTHS))
        self.assertEqual([row["month_start"] for row in rows], list(MONTHS))
        self.assertEqual(rows[0]["complaint_count"], "3")
        self.assertEqual(rows[0]["rate_per_10k"], "30.0000")
        self.assertIsNone(rows[1]["rate_per_10k"], "an empty rate must stay NULL, not 0")
        self.assertEqual(rows[1]["complaint_count"], "0", "a zero count is data, not a gap")
        self.assertEqual({row["modzcta"] for row in rows}, {"10001"})
        self.assertEqual(set(rows[0]), set(NARROW_COLUMNS))

    def test_unknown_area_is_a_real_area(self):
        rows = list(narrow_wide_rows(wide_row(area="UNKNOWN", population=None), MONTHS))
        self.assertEqual({row["modzcta"] for row in rows}, {"UNKNOWN"})
        self.assertIsNone(rows[0]["population"])

    def test_rejects_rows_without_key_fields(self):
        for column in ("modzcta", "complaint_type"):
            row = wide_row()
            row[column] = None
            with self.subTest(column=column), self.assertRaises(DataError):
                list(narrow_wide_rows(row, MONTHS))

    def test_rejects_empty_count(self):
        row = wide_row(counts={"2025-01": "", "2025-02": "1"})
        with self.assertRaises(DataError) as ctx:
            list(narrow_wide_rows(row, MONTHS))
        self.assertEqual(ctx.exception.context["month"], "2025-01")


class TypedConversionTests(unittest.TestCase):
    """The sink reuses load_csv, so the typed contract is shared, not forked."""

    def test_stage1_boolean_text(self):
        column = sa.Column("is_ambiguous", sa.Boolean(), nullable=False)
        for text in ("true", "TRUE", "True ", True):
            with self.subTest(text=text):
                self.assertIs(_convert(text, column), True)
        for text in ("false", "FALSE", False):
            with self.subTest(text=text):
                self.assertIs(_convert(text, column), False)

    def test_boolean_rejects_everything_else(self):
        column = sa.Column("is_ambiguous", sa.Boolean(), nullable=False)
        for value in ("1", "yes", "", "closed"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _convert(value, column)

    def test_boolean_null_follows_nullability(self):
        """A missing flag is NULL in a nullable column and an error in a NOT NULL one.

        Inventing False for a missing flag would quietly turn a gap into an answer,
        so the loader keeps the shared contract's rule instead of softening it.
        An empty cell never reaches here: read_csv_batches turns "" into None.
        """
        self.assertIsNone(_convert(None, sa.Column("b", sa.Boolean(), nullable=True)))
        with self.assertRaises(ValueError):
            _convert(None, sa.Column("b", sa.Boolean(), nullable=False))

    def test_other_types_delegate_to_the_shared_contract(self):
        self.assertEqual(_convert("42", sa.Column("n", sa.Integer())), 42)
        self.assertEqual(
            _convert("2025-01-03T00:57:07", sa.Column("t", sa.DateTime())),
            datetime(2025, 1, 3, 0, 57, 7),
        )
        with self.assertRaises(ValueError):
            _convert("nan", sa.Column("n", sa.Float()))


class SinkFileDiscoveryTests(unittest.TestCase):
    def test_filenames_come_from_the_writing_modules(self):
        self.assertEqual(
            SINK_TABLES,
            {
                SINK_CASE_TABLE: DATASET_FILENAME,
                SINK_WIDE_TABLE: WIDE_FILENAME,
                SINK_QUALITY_TABLE: REPORT_FILENAME,
            },
            "the sink must read the names the writers use, never copies of them",
        )

    def test_missing_curated_dir_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DataError) as ctx:
                factory_sink_paths(Path(tmp) / "absent")
            self.assertIn("dir", ctx.exception.context)

    def test_partial_output_lists_exactly_the_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            curated = Path(tmp)
            (curated / DATASET_FILENAME).write_text("x\n", encoding="utf-8")
            with self.assertRaises(DataError) as ctx:
                factory_sink_paths(curated)
            names = sorted(Path(item).name for item in ctx.exception.context["files"])
            self.assertEqual(names, sorted([REPORT_FILENAME, WIDE_FILENAME]))
            self.assertIn("hint", ctx.exception.context)

    def test_sink_is_its_own_migration_group(self):
        path = Path("009_factory_sink_schema.sql")
        self.assertTrue(_is_sink_migration(path))
        self.assertFalse(_is_base_migration(path))
        self.assertFalse(_is_snapshot_migration(path))
        for name in ("001_schema.sql", "005_snapshot.sql", "004_rejected.sql"):
            with self.subTest(name=name):
                self.assertFalse(_is_sink_migration(Path(name)))


class QualityReportParsingTests(unittest.TestCase):
    def write(self, directory: Path, payload) -> Path:
        path = directory / REPORT_FILENAME
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def profile(self, **overrides):
        stats = {
            "rows": 65, "null_count": 0, "null_percent": 0.0,
            "distinct_values": 65, "distinct_capped": False,
            "min": "1", "max": "9",
        }
        stats.update(overrides)
        return stats

    def test_report_shape_is_flattened_for_insert(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(Path(tmp), {
                "distinct_cap": 10000,
                "columns": {"unique_key": self.profile(), "closed_at": self.profile(
                    null_count=4, null_percent=6.1538, min=None, max="2025-06-30T23:59:00")},
                "borough_modzcta_conflicts": [
                    {"modzcta": "11385", "boroughs": ["BROOKLYN", "QUEENS"]},
                ],
                "borough_modzcta_conflict_count": 1,
            })
            parsed = parse_quality_report(path)
            self.assertEqual(parsed["distinct_cap"], 10000)
            self.assertEqual(parsed["conflict_count"], 1)
            self.assertEqual([row["column_name"] for row in parsed["profiles"]],
                             ["closed_at", "unique_key"])
            self.assertEqual(parsed["profiles"][0]["max_value"], "2025-06-30T23:59:00")
            self.assertIsNone(parsed["profiles"][0]["min_value"])
            self.assertEqual(
                parsed["conflicts"],
                [{"modzcta": "11385", "borough": "BROOKLYN"},
                 {"modzcta": "11385", "borough": "QUEENS"}],
            )

    def test_missing_profile_keys_are_reported_as_a_contract_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = self.profile()
            stats.pop("null_percent")
            path = self.write(Path(tmp), {"distinct_cap": 10, "columns": {"a": stats}})
            with self.assertRaises(DataError) as ctx:
                parse_quality_report(path)
            self.assertEqual(ctx.exception.context["missing"], ["null_percent"])

    def test_declared_conflict_count_must_match_the_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(Path(tmp), {
                "distinct_cap": 10,
                "columns": {"a": self.profile()},
                "borough_modzcta_conflicts": [
                    {"modzcta": "10001", "boroughs": ["QUEENS"]},
                ],
                "borough_modzcta_conflict_count": 7,
            })
            with self.assertRaises(DataError) as ctx:
                parse_quality_report(path)
            self.assertEqual(
                (ctx.exception.context["declared"], ctx.exception.context["listed"]),
                (7, 1),
            )

    def test_malformed_conflict_entries_are_rejected(self):
        for entry in (["QUEENS"], {"modzcta": "10001"}, {"modzcta": 1, "boroughs": ["Q"]},
                      {"modzcta": "10001", "boroughs": []}, {"modzcta": "10001", "boroughs": ["  "]}):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tmp:
                path = self.write(Path(tmp), {
                    "distinct_cap": 10,
                    "columns": {"a": self.profile()},
                    "borough_modzcta_conflicts": [entry],
                })
                with self.assertRaises(DataError):
                    parse_quality_report(path)

    def test_rejects_unreadable_or_wrongly_shaped_reports(self):
        cases = {
            "not json": "nope",
            "not an object": "[1, 2]",
            "no columns": json.dumps({"distinct_cap": 10}),
            "bad cap": json.dumps({"distinct_cap": 0, "columns": {"a": self.profile()}}),
            "cap is bool": json.dumps({"distinct_cap": True, "columns": {"a": self.profile()}}),
            "conflicts not a list": json.dumps(
                {"distinct_cap": 10, "columns": {"a": self.profile()},
                 "borough_modzcta_conflicts": {}}
            ),
        }
        for label, text in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / REPORT_FILENAME
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(DataError):
                    parse_quality_report(path)

    def test_rejects_storable_bound_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(Path(tmp), {
                "distinct_cap": 10,
                "columns": {"a": self.profile(min=[1, 2])},
            })
            with self.assertRaises(DataError) as ctx:
                parse_quality_report(path)
            self.assertEqual(ctx.exception.context["column"], "a")

    def test_no_csv_reader_is_involved(self):
        """The JSON report must not be read as CSV; a header error is wrong here."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(Path(tmp), {"distinct_cap": 10, "columns": {"a": {}}})
            with self.assertRaises(DataError) as ctx:
                parse_quality_report(path)
            self.assertEqual(ctx.exception.context["column"], "a")


class TruncateStatementTests(unittest.TestCase):
    def test_truncate_lists_every_sink_table(self):
        class Recorder:
            def __init__(self):
                self.statements = []

            def execute(self, statement):
                self.statements.append(str(statement))

        recorder = Recorder()
        truncate_factory(recorder)
        self.assertEqual(len(recorder.statements), 1)
        statement = recorder.statements[0]
        for table in (SINK_CASE_TABLE, SINK_WIDE_TABLE, SINK_QUALITY_TABLE,
                      SINK_META_TABLE, SINK_CONFLICT_TABLE, SINK_PROVENANCE_TABLE):
            with self.subTest(table=table):
                self.assertIn(f"factory.{table}", statement)
        self.assertIn("RESTART IDENTITY", statement)
        self.assertTrue(statement.startswith("TRUNCATE TABLE"))


class SinkRunsBeforeConnectingTests(unittest.TestCase):
    def test_incomplete_output_fails_without_touching_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "curated").mkdir()  # the dir exists, the outputs do not
            cfg = dataclasses.replace(
                make_config(Path(tmp)),
                database=DatabaseConfig(
                    url="postgresql+psycopg://user@localhost:5432/db",
                    password_env="ABSENT_FOR_TEST",
                    olist_csv_dir=Path(tmp) / "csv",
                    stage1_curated_dir=Path(tmp) / "curated",
                    batch_size=100,
                ),
            )
            with mock.patch("data_factory.database.sa.create_engine") as create_engine:
                with self.assertRaises(DataError) as ctx:
                    load_stage1_sink(cfg)
            self.assertIn("files", ctx.exception.context)
            create_engine.assert_not_called()

    def test_narrow_columns_match_the_loader_contract(self):
        self.assertEqual(
            NARROW_COLUMNS,
            ("modzcta", "complaint_type", "month_start",
             "complaint_count", "rate_per_10k", "population"),
        )


def case_row(**overrides) -> str:
    """One cases.csv line, built from the delivered column order."""
    row = dict.fromkeys(CLEAN_COLUMNS, "")
    row.update({
        "unique_key": "11",
        "created_at": "2025-01-03T00:57:07",
        "closed_at": "2025-01-03T01:30:07",
        "duration_seconds": "1980",
        "is_ambiguous": "false",
        "agency": "NYPD",
        "complaint_type": "Noise - Residential",
        "descriptor": "Loud Music/Party",
        "status": "Closed",
        "borough": "QUEENS",
        "community_board": "4",
        "incident_zip": "11354",
        "modzcta": "10001",
        "population": "1000",
    })
    row.update(overrides)
    return ",".join(row[column] for column in CLEAN_COLUMNS) + "\n"


class CaseFileLoadTests(unittest.TestCase):
    """The real load path for cases.csv, on a reflected table.

    SQLite stands in for the storage layer only: load_csv is the same function
    the factory schema uses, and this proves the typed columns receive Python
    bools and datetimes rather than relying on server coercion. The CHECK
    constraints and the factory schema qualifier are covered by
    stage-2/tests/test_factory_sink_postgres.py.
    """

    def setUp(self):
        self.engine = sa.create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        self.metadata = sa.MetaData()
        types = {
            "unique_key": sa.BigInteger(), "created_at": sa.DateTime(),
            "closed_at": sa.DateTime(), "duration_seconds": sa.BigInteger(),
            "is_ambiguous": sa.Boolean(), "agency": sa.Text(),
            "complaint_type": sa.Text(), "descriptor": sa.Text(),
            "status": sa.Text(), "borough": sa.Text(),
            "community_board": sa.Text(), "incident_zip": sa.String(5),
            "modzcta": sa.String(5), "population": sa.BigInteger(),
        }
        required = {"unique_key", "created_at", "is_ambiguous", "agency",
                    "complaint_type", "status", "borough"}
        self.table = sa.Table(SINK_CASE_TABLE, self.metadata, *[
            sa.Column(name, types[name], primary_key=(name == "unique_key"),
                      nullable=name not in required)
            for name in CLEAN_COLUMNS
        ])
        with self.engine.begin() as connection:
            self.metadata.create_all(connection)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.csv_path = Path(folder.name) / DATASET_FILENAME

    def load(self, rows, batch_size=2):
        self.csv_path.write_text(",".join(CLEAN_COLUMNS) + "\n" + "".join(rows),
                                encoding="utf-8")
        with self.engine.begin() as connection:
            return load_csv(connection, self.csv_path, None, SINK_CASE_TABLE, batch_size)

    def test_values_are_stored_typed(self):
        counts = self.load([
            case_row(),
            case_row(unique_key="12", is_ambiguous="true", closed_at="",
                     duration_seconds="", status="Open"),
        ])
        self.assertEqual(counts["loaded"], 2)
        with self.engine.connect() as connection:
            stored = connection.execute(
                sa.select(self.table).order_by(self.table.c.unique_key)
            ).mappings().all()
        first, second = stored
        self.assertIs(first["is_ambiguous"], False)
        self.assertIs(second["is_ambiguous"], True)
        self.assertEqual(first["created_at"], datetime(2025, 1, 3, 0, 57, 7))
        self.assertEqual(int(first["duration_seconds"]), 1980)
        self.assertIsNone(second["closed_at"], "an open case stays NULL, not a sentinel")
        self.assertIsNone(second["duration_seconds"])

    def test_empty_optional_cells_stay_null(self):
        self.load([case_row(modzcta="", incident_zip="", population="",
                            community_board="", descriptor="")])
        with self.engine.connect() as connection:
            row = connection.execute(sa.select(self.table)).mappings().one()
        for column in ("modzcta", "incident_zip", "population", "community_board",
                       "descriptor"):
            with self.subTest(column=column):
                self.assertIsNone(row[column])

    def test_a_row_that_breaks_the_contract_reports_its_line(self):
        """A bad value names its file, line and column, and writes nothing.

        batch_size=1 puts the good row in a batch of its own, so a surviving row
        could only come from a missing rollback, not from a batch boundary.
        """
        with self.assertRaises(DataError) as ctx:
            self.load([case_row(), case_row(unique_key="12", is_ambiguous="maybe")],
                      batch_size=1)
        self.assertEqual(ctx.exception.context["column"], "is_ambiguous")
        self.assertEqual(ctx.exception.context["line"], 3)
        self.assertEqual(ctx.exception.context["file"], str(self.csv_path))
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.scalar(sa.select(sa.func.count()).select_from(self.table)), 0,
                "the caller's transaction rolled back, so the good row is not stored either",
            )


class CliSinkFlagTests(unittest.TestCase):
    """The sink is opt-out, and the flag is the only way to opt out."""

    def test_sink_runs_unless_the_flag_is_given(self):
        parser = build_parser()
        self.assertIs(parser.parse_args(["load-database"]).skip_factory_sink, False)
        self.assertIs(
            parser.parse_args(["load-database", "--skip-factory-sink"]).skip_factory_sink,
            True,
        )

    def test_flag_combines_with_reset(self):
        args = build_parser().parse_args(["load-database", "--reset", "--skip-factory-sink"])
        self.assertTrue(args.reset)
        self.assertTrue(args.skip_factory_sink)


if __name__ == "__main__":
    unittest.main()
