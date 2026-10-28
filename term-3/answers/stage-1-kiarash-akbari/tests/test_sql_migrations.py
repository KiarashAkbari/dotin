"""Migration discovery and grouping tests, in the suite's own runner.

These are unittest.TestCase on purpose: the suite runs through
`python -m unittest discover -s tests` and stage 1 does not depend on pytest, so
a pytest-only test file here is an import error, not a collected test.
"""
import contextlib
import dataclasses
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from config_helpers import make_config
from data_factory.config import DatabaseConfig, parse_config
from data_factory.database import (
    apply_sql_migrations,
    sql_migration_files,
)
from data_factory.errors import DataError


class RecordingCursor:
    def __init__(self, log, fail_on=None):
        self.log = log
        self.fail_on = fail_on

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql):
        self.log.append(sql)
        if self.fail_on is not None and self.fail_on in sql:
            raise RuntimeError(f"syntax error at or near {self.fail_on!r}")


class FakeEngine:
    """Stands in for the driver connection apply_sql_migrations talks to."""

    def __init__(self, log, fail_on=None):
        self.log = log
        self.fail_on = fail_on
        self.begin_calls = 0

    @contextlib.contextmanager
    def begin(self):
        self.begin_calls += 1
        yield self

    @property
    def connection(self):
        return self

    @property
    def driver_connection(self):
        return self

    def cursor(self):
        return RecordingCursor(self.log, self.fail_on)

    def dispose(self):
        self.log.append("dispose")


class SqlMigrationFileTests(unittest.TestCase):
    def test_sql_migration_files_are_direct_and_ordered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "002_views.sql").write_text("SELECT 2;", encoding="utf-8")
            (root / "001_schema.sql").write_text("SELECT 1;", encoding="utf-8")
            (root / "queries").mkdir()
            (root / "queries" / "999_dashboard.sql").write_text("SELECT 999;", encoding="utf-8")
            (root / "rejected").mkdir()
            (root / "rejected" / "000_rejected.sql").write_text("SELECT 0;", encoding="utf-8")

            files = sql_migration_files(root)

            self.assertEqual([path.name for path in files], ["001_schema.sql", "002_views.sql"])

    def test_sql_migration_files_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DataError):
                sql_migration_files(Path(tmp) / "missing")

    def test_database_config_accepts_optional_sql_migrations_dir(self):
        config = parse_config(
            {
                "source": {
                    "url": "https://example.test",
                    "agency": "NYPD",
                    "complaint_types": ["Noise - Residential"],
                    "columns": ["unique_key"],
                    "start_date": date(2025, 1, 1),
                    "end_date": date(2025, 1, 2),
                    "page_size": 1000,
                    "app_token_env": "TOKEN",
                    "timezone": "America/New_York",
                },
                "http": {
                    "timeout_seconds": 1,
                    "max_retries": 0,
                    "backoff_base_seconds": 0,
                    "backoff_factor": 1,
                    "retry_statuses": [500],
                },
                "paths": {
                    "raw_dir": "raw",
                    "curated_dir": "curated",
                    "quarantine_dir": "quarantine",
                    "manifest_path": "manifest.json",
                },
                "quality": {
                    "max_quarantine_ratio": 0.5,
                    "min_rows_for_threshold": 1,
                },
                "reference": {
                    "url": "https://example.test",
                    "columns": ["modzcta"],
                    "limit": 1,
                    "path": "reference.json",
                },
                "database": {
                    "url": "postgresql+psycopg://user@localhost/db",
                    "password_env": "PASSWORD",
                    "olist_csv_dir": "csv",
                    "stage1_curated_dir": "curated",
                    "batch_size": 1,
                    "sql_migrations_dir": "sql",
                },
            }
        )

        self.assertIsNotNone(config.database)
        self.assertEqual(config.database.sql_migrations_dir, Path("sql"))


class ApplySqlMigrationsTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.sql_dir = Path(self._folder.name)
        for name, body in (
            ("001_base.sql", "CREATE SCHEMA olist;"),
            ("005_snap.sql", "CREATE MATERIALIZED VIEW olist.cover AS SELECT 1;"),
            ("009_sink.sql", "CREATE SCHEMA factory;"),
        ):
            (self.sql_dir / name).write_text(body, encoding="utf-8")

    def test_include_filter_selects_one_group(self):
        log = []
        applied = apply_sql_migrations(
            FakeEngine(log),
            self.sql_dir,
            include=lambda path: path.name.startswith("009"),
        )
        self.assertEqual(applied, ["009_sink.sql"])
        self.assertEqual(log, ["CREATE SCHEMA factory;"])

    def test_empty_selection_is_not_an_error(self):
        log = []
        applied = apply_sql_migrations(
            FakeEngine(log),
            self.sql_dir,
            include=lambda path: False,
        )
        self.assertEqual(applied, [])
        self.assertEqual(log, [])

    def test_selection_runs_in_one_transaction(self):
        engine = FakeEngine(log := [])
        applied = apply_sql_migrations(
            engine, self.sql_dir, include=lambda path: path.name[:3] in {"001", "005"}
        )
        self.assertEqual(applied, ["001_base.sql", "005_snap.sql"])
        self.assertEqual(engine.begin_calls, 1, "two files, one transaction")

    def test_failure_names_the_script_that_broke(self):
        engine = FakeEngine(log := [], fail_on="factory")
        with self.assertRaises(DataError) as ctx:
            apply_sql_migrations(engine, self.sql_dir)
        self.assertEqual(Path(ctx.exception.context["file"]).name, "009_sink.sql")
        self.assertIn("syntax error", ctx.exception.context["error"])
        self.assertEqual(log, [
            "CREATE SCHEMA olist;",
            "CREATE MATERIALIZED VIEW olist.cover AS SELECT 1;",
            "CREATE SCHEMA factory;",
        ], "the earlier scripts ran inside the transaction that is rolled back")


class CliMigrationOrderTests(unittest.TestCase):
    """The groups, and the order they run in, are part of the contract."""

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.cfg = dataclasses.replace(
            make_config(Path(self._folder.name)),
            database=DatabaseConfig(
                url="postgresql+psycopg://user@localhost:5432/db",
                password_env="ABSENT_FOR_TEST",
                olist_csv_dir=Path(self._folder.name) / "csv",
                stage1_curated_dir=Path(self._folder.name) / "curated",
                batch_size=100,
                sql_migrations_dir=Path(self._folder.name) / "sql",
            ),
        )
        self.calls = []
        self.sink_present = True
        patches = [
            mock.patch("data_factory.cli.sa.create_engine",
                                side_effect=lambda url: self._engine()),
            mock.patch("data_factory.cli.build_database_url",
                                side_effect=lambda cfg: "url"),
            mock.patch("data_factory.cli.apply_sql_migrations",
                                side_effect=self._apply),
            mock.patch("data_factory.cli.load_olist",
                                side_effect=lambda cfg: self.calls.append("load_olist") or {}),
            mock.patch("data_factory.cli.load_stage1_sink",
                                side_effect=lambda cfg: self.calls.append("load_stage1_sink") or {}),
            mock.patch("data_factory.cli.reset_olist_schema",
                                side_effect=lambda engine: self.calls.append("reset_olist")),
            mock.patch("data_factory.cli.reset_factory_schema",
                                side_effect=lambda engine: self.calls.append("reset_factory")),
        ]
        self.stack = contextlib.ExitStack()
        for patcher in patches:
            self.stack.enter_context(patcher)
        self.addCleanup(self.stack.close)

    def _engine(self):
        self.calls.append("create_engine")
        return FakeEngine([])

    def _apply(self, engine, sql_dir, include=None):
        names = sorted(path for path in (
            "001_base.sql", "005_snap.sql", "009_sink.sql"
        ) if include is None or include(Path("sql") / path))
        if not self.sink_present:
            names = [name for name in names if not name.startswith("009")]
        self.calls.append("apply:" + ",".join(name[:3] for name in names))
        return names

    def test_base_then_load_then_sink_then_snapshots(self):
        from data_factory.cli import _load_database

        _load_database(self.cfg)
        self.assertEqual(self.calls, [
            "create_engine",
            "apply:001",
            "load_olist",
            "create_engine",
            "apply:009",
            "apply:005",
            "load_stage1_sink",
        ])

    def test_reset_drops_both_schemas_before_loading(self):
        from data_factory.cli import _load_database

        _load_database(self.cfg, reset=True)
        self.assertEqual(self.calls[:3], ["create_engine", "reset_olist", "reset_factory"])

    def test_skip_sql_migrations_skips_both_groups_and_the_sink(self):
        from data_factory.cli import _load_database

        _load_database(self.cfg, apply_migrations=False)
        self.assertEqual(
            self.calls,
            ["create_engine", "load_olist"],
            "no SQL at all: neither migration group nor the sink load",
        )

    def test_sink_can_be_left_out_of_a_run(self):
        from data_factory.cli import _load_database

        _load_database(self.cfg, with_sink=False)
        self.assertEqual(self.calls, [
            "create_engine",
            "apply:001",
            "load_olist",
            "create_engine",
            "apply:005",
        ], "009 is not applied and the curated files are not loaded")

    def test_missing_sink_migration_is_an_error_not_a_skip(self):
        from data_factory.cli import _load_database

        self.sink_present = False
        with self.assertRaises(DataError) as ctx:
            _load_database(self.cfg)
        self.assertEqual(ctx.exception.context["expected"], "009_factory_sink_schema.sql")


if __name__ == "__main__":
    unittest.main()
