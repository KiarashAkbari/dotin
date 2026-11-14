"""The ingest wiring, tested without a server: config, table shape, paging, quarantine.

There is no PostgreSQL-gated counterpart of this file, and no test here reads
``TEST_DATABASE_URL``: the server-dependent parts - applying ``010``/``011``,
inserting into a schema, reading the digest back - are exercised by the commands in
``README.md`` against a real database, whose printed output is the evidence. What is
proven offline is everything that is a property of this code rather than of a server:
the config contract and the migration set, the column list the insert will use, the
page store being fed exactly as stage 1 feeds it, the quarantine file, the fail-fast
limit, and the read path against a real (sqlite) view.
"""

from __future__ import annotations

import contextlib
import csv
import inspect
import io
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

HERE = Path(__file__).resolve().parent
for candidate in (
    HERE,
    HERE.parent / "src",
    HERE.parents[1] / "stage-1" / "src",
):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from data_factory.config import parse_config
from data_factory.errors import ConfigError, DataError
from data_factory.quality import QuarantineThresholdError
from fixtures import write_csv

from credit import load as credit_load
from credit.columns import (
    CSV_COLUMNS,
    DB_COLUMNS,
    DB_SCHEMA,
    DB_TABLE,
    RUN_COLUMNS,
)
from credit.config import (
    load_credit_config,
    parse_credit_config,
    read_toml,
)
from credit.load import (
    application_table,
    apply_migrations,
    checksum_mismatch_hint,
    collect_records,
    digest_query,
    insert_records,
    migration_names,
    reason_counts,
    reset_credit,
    resolve_credit_config,
    run_table,
)
from credit.read import VIEWS, load_frame, resolve_view, row_count

#: the aggregate names written into the manifest and compared by the runbook
DIGEST_KEYS = (
    "rows_total",
    "defaults",
    "income_missing",
    "age_missing",
    "utilization_mean",
    "age_min",
    "age_max",
    "utilization_above_1",
    "code_30_59",
    "code_90",
    "code_60_89",
    "distinct_ids",
)

STAGE1_KEYS = {
    "source": {
        "url": "https://example.invalid/api",
        "dataset_id": "x",
        "start_date": "2024-01-01",
        "end_date": "2024-01-31",
        "page_size": 1000,
    },
    "http": {
        "max_retries": 3,
        "timeout_seconds": 30,
        "backoff_base_seconds": 1.0,
        "backoff_factor": 2.0,
    },
    "paths": {"raw_dir": "data/raw", "curated_dir": "data/curated"},
    "quality": {"max_quarantine_ratio": 0.05, "min_rows": 100},
}


def config_values(base: Path, **overrides: object) -> dict:
    """A config whose every path is inside ``base``.

    The scratch directories are spelled out rather than left to the defaults: the
    shipped defaults climb out of ``config/`` on purpose, and resolving them against a
    test's temporary directory would land them in ``/tmp`` and let one test read
    another test's page store.
    """
    values = {
        "csv_path": str(base / "cs-training.csv"),
        "sql_migrations_dir": str(base / "sql"),
        "curated_dir": str(base / "curated"),
        "raw_pages_dir": str(base / "pages"),
        "quarantine_dir": str(base / "quarantine"),
    }
    values.update(overrides)
    return values


class CreditConfigTests(unittest.TestCase):
    def test_the_example_file_is_the_documented_shape(self):
        example = HERE.parent / "config" / "credit.example.toml"
        data = read_toml(example)
        cfg = parse_credit_config(data["credit"], base=example.parent)
        self.assertEqual(cfg.schema, DB_SCHEMA)
        # the shipped directory is the migration list, and it has to carry both
        # files or the staging view a fresh file is scored through never exists
        self.assertEqual(
            migration_names(cfg),
            ["010_credit_schema.sql", "011_credit_staging.sql"],
        )
        self.assertEqual(cfg.page_size, 10_000)
        # relative paths climb out of config/, exactly as stage 1's do
        self.assertTrue(cfg.csv_path.is_absolute())
        self.assertEqual(cfg.csv_path.name, "cs-training.csv")

    def test_missing_required_key_names_the_key(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaises(ConfigError) as caught,
        ):
            parse_credit_config({"sql_migrations_dir": str(Path(tmp))}, base=Path(tmp))
        self.assertIn("csv_path", str(caught.exception))

    def test_page_size_and_quarantine_share_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for key, bad in (
                ("page_size", 0),
                ("page_size", "10"),
                ("quarantine_max_share", 1.5),
            ):
                with self.subTest(key=key, bad=bad), self.assertRaises(ConfigError):
                    parse_credit_config(config_values(base, **{key: bad}), base=base)

    def test_schema_must_be_a_sql_identifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for bad in ("credit; drop", "1credit", ""):
                with self.subTest(bad=bad), self.assertRaises(ConfigError):
                    parse_credit_config(config_values(base, schema=bad), base=base)

    def test_stage1_still_parses_when_a_credit_section_is_present(self):
        """Adding [credit] cannot break the factory: stage 1 ignores what it does not read.

        Checked against stage 1's real ``config/default.toml`` rather than a hand-written
        one, so the test cannot drift into a shape the parser never accepted. If stage 1
        tightens its config schema, this test fails here instead of the credit ingest
        mysteriously refusing to start.
        """
        import tomllib

        stage1 = HERE.parents[1] / "stage-1" / "config" / "default.toml"
        if not stage1.is_file():
            self.skipTest(f"stage 1 config not on this path: {stage1}")
        plain = stage1.read_text(encoding="utf-8")
        parsed = tomllib.loads(plain)
        self.assertNotIn("credit", parsed)
        with_credit = tomllib.loads(
            plain
            + '\n[credit]\ncsv_path = "data/raw/cs-training.csv"\nsql_migrations_dir = "sql"\n'
        )
        self.assertIn("credit", with_credit)
        self.assertEqual(parse_config(parsed), parse_config(with_credit))

    def test_resolve_reports_both_places_it_tried(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage1 = Path(tmp) / "default.toml"
            stage1.write_text('[source]\nurl = "x"\n', encoding="utf-8")
            with self.assertRaises(DataError) as caught:
                resolve_credit_config(stage1, None)
        context = caught.exception.context
        self.assertEqual(len(context["tried"]), 2)
        self.assertIn("required", context)

    def test_standalone_file_needs_a_credit_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credit.toml"
            path.write_text("[other]\nx = 1\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_credit_config(path)

    def test_missing_file_points_at_the_example_beside_it(self):
        # The path alone sent a reader to the README for a command they had already
        # been told once. The example is next to the file, so say to copy it - and only
        # when the example is actually there, or the advice becomes a second dead end.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copyfile(
                HERE.parent / "config" / "credit.example.toml",
                root / "credit.example.toml",
            )
            with self.assertRaises(ConfigError) as caught:
                load_credit_config(root / "credit.toml")
        message = str(caught.exception)
        self.assertTrue(message.startswith("Config file not found: "))
        self.assertIn("cp ", message)
        self.assertIn("credit.example.toml", message)

    def test_missing_file_without_an_example_states_the_contract(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            self.assertRaises(ConfigError) as caught,
        ):
            load_credit_config(Path(tmp) / "credit.toml")
        message = str(caught.exception)
        self.assertNotIn("cp ", message)
        self.assertIn("[credit]", message)


class TableShapeTests(unittest.TestCase):
    def test_insert_target_covers_the_contract_in_order(self):
        table = application_table(DB_SCHEMA)
        self.assertEqual([column.name for column in table.columns], list(DB_COLUMNS))
        self.assertEqual(table.schema, DB_SCHEMA)
        self.assertEqual(table.name, DB_TABLE)

    def test_run_target_matches_the_provenance_columns(self):
        table = run_table(DB_SCHEMA)
        self.assertEqual([column.name for column in table.columns], list(RUN_COLUMNS))

    def test_staging_table_uses_the_same_shape_as_the_load_target(self):
        from credit.columns import DB_STAGING_TABLE

        staging = application_table(DB_SCHEMA, DB_STAGING_TABLE)
        load = application_table(DB_SCHEMA)
        self.assertEqual(
            [(c.name, str(c.type)) for c in staging.columns],
            [(c.name, str(c.type)) for c in load.columns],
        )
        self.assertEqual(staging.schema, DB_SCHEMA)

    def test_digest_query_is_aggregates_only(self):
        sql = digest_query(DB_SCHEMA)
        # the runbook's parity step compares these twelve numbers between two loads,
        # so the key set is a documented contract and not an implementation detail
        self.assertEqual(len(DIGEST_KEYS), 12)
        for alias in DIGEST_KEYS:
            with self.subTest(alias=alias):
                self.assertIn(f"AS {alias}", sql)
        self.assertIn("count(*)", sql)
        self.assertIn(f"FROM {DB_SCHEMA}.{DB_TABLE}", sql)
        for forbidden in ("SELECT *", "LIMIT", "row_id ="):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, sql)


class ResetStatementTests(unittest.TestCase):
    """The ledger is why a reset must delete a row as well as drop a schema."""

    class Result:
        def __init__(self, value: object) -> None:
            self.value = value

        def scalar(self):
            return self.value

    class Recorder:
        """Stands in for a connection, and answers the ledger probe on command."""

        def __init__(self, *, ledger_present: bool = True) -> None:
            self.statements: list[tuple[str, object]] = []
            self.ledger_present = ledger_present

        def execute(self, statement, params=None):
            text = str(statement)
            self.statements.append((text, params))
            if text.startswith("SELECT to_regclass"):
                return ResetStatementTests.Result(self.ledger_present)
            return ResetStatementTests.Result(None)

    class Engine:
        def __init__(self, recorder: ResetStatementTests.Recorder) -> None:
            self.recorder = recorder

        def begin(self):
            recorder = self.recorder

            class _Ctx:
                def __enter__(inner):
                    return recorder

                def __exit__(inner, *exc):
                    return False

            return _Ctx()

    def test_reset_drops_the_schema_and_every_ledger_row_this_stage_owns(self):
        """One DELETE per migration file, by bind parameter, nothing wider.

        The ledger is keyed by name, so a reset that clears only the first file would
        tell the next run the staging view is already there in a schema it just
        dropped. And a reset that cleared the whole table would undo stage 2's own
        history, which is the damage this test exists to make impossible.
        """
        recorder = self.Recorder()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sql = root / "sql"
            sql.mkdir()
            (sql / "010_credit_schema.sql").write_text("SELECT 1;\n", encoding="utf-8")
            (sql / "011_credit_staging.sql").write_text("SELECT 2;\n", encoding="utf-8")
            cfg = parse_credit_config(
                config_values(root, sql_migrations_dir=str(sql)), base=root
            )
            # discovery reads the directory, so the reset runs while it still exists
            reset_credit(self.Engine(recorder), cfg)  # type: ignore[arg-type]
        self.assertEqual(len(recorder.statements), 4)
        drop, params = recorder.statements[0]
        self.assertIn("DROP SCHEMA IF EXISTS credit CASCADE", drop)
        self.assertIsNone(params)
        probe, params = recorder.statements[1]
        self.assertIn("to_regclass", probe)
        # the table name is a bind parameter too, so the probe cannot be steered
        self.assertEqual(params, {"table": "public.data_factory_migrations"})
        names = []
        for delete, params in recorder.statements[2:]:
            self.assertIn("DELETE FROM", delete)
            self.assertIn("data_factory_migrations", delete)
            self.assertIn("WHERE migration_name = :name", delete)
            # a migration name is a bind parameter, never interpolated into the text
            self.assertNotIn("credit_schema.sql", delete)
            names.append(params["name"])
        self.assertEqual(names, ["010_credit_schema.sql", "011_credit_staging.sql"])

    def test_reset_on_a_database_without_a_ledger_drops_only_the_schema(self):
        """The first run on an empty database must be resettable.

        The ledger is created by the migration runner, which has not run yet at reset
        time - so a reset that assumed the table existed crashed before the load began,
        with an UndefinedTable traceback rather than a decision.
        """
        recorder = self.Recorder(ledger_present=False)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sql = root / "sql"
            sql.mkdir()
            (sql / "010_credit_schema.sql").write_text("SELECT 1;\n", encoding="utf-8")
            cfg = parse_credit_config(
                config_values(root, sql_migrations_dir=str(sql)), base=root
            )
            reset_credit(self.Engine(recorder), cfg)  # type: ignore[arg-type]
        text = " | ".join(statement for statement, _ in recorder.statements)
        self.assertEqual(len(recorder.statements), 2)
        self.assertIn("DROP SCHEMA IF EXISTS credit CASCADE", text)
        self.assertNotIn("DELETE FROM", text)


class MigrationSetTests(unittest.TestCase):
    """What the loader applies, and how a new file joins that set.

    The runner used to be told the name of one migration in the config, which meant a
    second schema file needed a config edit nobody remembered - so `011` was never
    applied by any code path, and the staging view a fresh file is scored through did
    not exist anywhere. Discovery over the directory makes that class of mistake
    structurally impossible, and these tests are the shape of that claim.
    """

    def _cfg(self, root: Path, sql: Path):
        return parse_credit_config(
            config_values(root, sql_migrations_dir=str(sql)), base=root
        )

    def test_the_shipped_directory_is_the_set(self):
        cfg = self._cfg(HERE.parent, HERE.parent / "sql")
        self.assertEqual(
            migration_names(cfg),
            ["010_credit_schema.sql", "011_credit_staging.sql"],
        )

    def test_a_new_file_needs_no_config_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sql = root / "sql"
            (sql / "queries").mkdir(parents=True)
            for name in ("011_b.sql", "010_a.sql", "012_c.sql"):
                (sql / name).write_text("SELECT 1;\n", encoding="utf-8")
            # a subdirectory is measurement SQL, and prose is not SQL
            (sql / "queries" / "013_report.sql").write_text(
                "SELECT 2;\n", encoding="utf-8"
            )
            (sql / "NOTES.md").write_text("hi\n", encoding="utf-8")
            cfg = self._cfg(root, sql)
            self.assertEqual(
                migration_names(cfg), ["010_a.sql", "011_b.sql", "012_c.sql"]
            )

    def test_an_empty_directory_fails_before_the_database_is_reached(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sql").mkdir()
            cfg = self._cfg(root, root / "sql")
            with self.assertRaises(DataError) as caught:
                apply_migrations(None, cfg)  # type: ignore[arg-type]
        self.assertIn("no credit migrations", str(caught.exception))
        self.assertEqual(caught.exception.context["dir"], str(root / "sql"))

    def test_a_missing_directory_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = self._cfg(root, root / "absent")
            with self.assertRaises(DataError) as caught:
                apply_migrations(None, cfg)  # type: ignore[arg-type]
        self.assertIn("credit.sql_migrations_dir does not exist", str(caught.exception))

    def test_the_retired_key_is_refused_rather_than_ignored(self):
        """A config that still sets `migration` must be told, not silently accepted.

        Someone's copy of the old example file is the likely case, and an ignored key is
        the worst outcome: it reads as if it still selects something.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            values = config_values(root)
            values["migration"] = "010_credit_schema.sql"
            with self.assertRaises(ConfigError) as caught:
                parse_credit_config(values, base=root)
        message = str(caught.exception)
        self.assertIn("credit.migration is no longer a setting", message)
        self.assertIn("sql_migrations_dir", message)

    def test_a_checksum_mismatch_is_answered_with_the_recovery_path(self):
        """A drifted ledger has to say what to run, not only what it found.

        The guard is stage 1's and it is right to refuse: the bytes on disk are not the
        bytes that built the schema. But on its own the message leaves the operator
        reading library code, so this stage appends the ledger query and the one command
        that rebuilds it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sql = root / "sql"
            sql.mkdir()
            (sql / "010_credit_schema.sql").write_text("SELECT 1;\n", encoding="utf-8")
            cfg = self._cfg(root, sql)
            original = credit_load.apply_sql_migrations

            def drift(engine: object, directory: object) -> list[str]:
                raise DataError(
                    "SQL migration checksum mismatch",
                    context={
                        "file": "010_credit_schema.sql",
                        "expected": "aa11",
                        "actual": "bb22",
                    },
                )

            credit_load.apply_sql_migrations = drift  # type: ignore[assignment]
            try:
                with self.assertRaises(DataError) as caught:
                    apply_migrations(None, cfg)  # type: ignore[arg-type]
            finally:
                credit_load.apply_sql_migrations = original  # type: ignore[assignment]
        message = str(caught.exception)
        self.assertIn("SQL migration checksum mismatch", message)
        self.assertIn("--reset", message)
        self.assertIn("public.data_factory_migrations", message)
        self.assertEqual(caught.exception.context["file"], "010_credit_schema.sql")
        self.assertEqual(caught.exception.context["actual"], "bb22")

    def test_another_migration_failure_keeps_its_own_message(self):
        """Only the checksum guard gets the recovery note bolted on.

        A migration that fails for SQL reasons is a different problem, and appending
        "rebuild this stage" to it would point at the one action that cannot help.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sql = root / "sql"
            sql.mkdir()
            (sql / "010_credit_schema.sql").write_text("SELECT 1;\n", encoding="utf-8")
            cfg = self._cfg(root, sql)
            original = credit_load.apply_sql_migrations

            def broken(engine: object, directory: object) -> list[str]:
                raise DataError('syntax error at or near "SELEKT"', context={})

            credit_load.apply_sql_migrations = broken  # type: ignore[assignment]
            try:
                with self.assertRaises(DataError) as caught:
                    apply_migrations(None, cfg)  # type: ignore[arg-type]
            finally:
                credit_load.apply_sql_migrations = original  # type: ignore[assignment]
        self.assertEqual(str(caught.exception), 'syntax error at or near "SELEKT"')
        self.assertNotIn("--reset", str(caught.exception))

    def test_the_hint_is_a_query_anyone_can_run_and_one_command(self):
        text = checksum_mismatch_hint()
        self.assertIn("SELECT migration_name", text)
        self.assertIn("ORDER BY applied_at", text)
        self.assertIn("python -m credit.load --reset", text)


class CollectRecordsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "sql").mkdir()
        self.csv = write_csv(self.tmp / "cs-training.csv", rows=137)
        self.cfg = parse_credit_config(config_values(self.tmp), base=self.tmp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_every_row_of_a_clean_file_reaches_the_report(self):
        records, report, fetch = collect_records(self.cfg)
        self.assertEqual(report.total, 137)
        self.assertEqual(report.quarantined, 0)
        self.assertEqual(len(records), 137)
        self.assertEqual(list(records[0].keys()), list(DB_COLUMNS))
        self.assertEqual(fetch.pages_total, 1)
        self.assertEqual(fetch.requests_this_page if False else fetch.pages_fetched, 1)

    def test_the_page_size_bounds_the_store_not_the_result(self):
        cfg = replace(self.cfg, page_size=25)
        _records, report, fetch = collect_records(cfg)
        self.assertEqual(report.total, 137)
        self.assertEqual(fetch.pages_fetched, 6)  # 25 * 5 + 12
        self.assertEqual(fetch.pages_total, 6)

    def test_a_bad_row_is_quarantined_with_its_reason_and_the_good_rows_survive(self):
        lines = self.csv.read_text().splitlines()
        # a count that is not a number at all: the contract has no way to read it
        broken = lines[1].split(",")
        broken[CSV_COLUMNS.index("age")] = "abc"
        lines.insert(3, ",".join(broken))
        self.csv.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        records, report, _fetch = collect_records(self.cfg)
        self.assertEqual(report.total, 138)
        self.assertEqual(len(records), 137)
        self.assertEqual(report.quarantined, 1)
        counts = reason_counts(report)
        self.assertEqual(counts["UNPARSEABLE_NUMBER"], 1)
        quarantine = self.tmp / "quarantine" / "quarantine.jsonl"
        self.assertTrue(quarantine.is_file(), msg=str(quarantine))
        text = quarantine.read_text(encoding="utf-8")
        self.assertIn("UNPARSEABLE_NUMBER", text)
        self.assertIn("abc", text)

    def test_a_file_too_corrupt_to_trust_aborts_the_run(self):
        lines = self.csv.read_text().splitlines()
        broken_row = lines[1].split(",")
        broken_row[CSV_COLUMNS.index("SeriousDlqin2yrs")] = "2"
        for index in range(2, 12):
            lines[index] = ",".join(broken_row)
        self.csv.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        # min_rows is 1000 by default, so 137 rows never trip the limit: lower it
        cfg = replace(self.cfg, quarantine_min_rows=10)
        with self.assertRaises(QuarantineThresholdError):
            collect_records(cfg)

    def test_a_missing_file_says_where_to_put_it(self):
        cfg = replace(self.cfg, csv_path=self.tmp / "nope.csv")
        with self.assertRaises(DataError) as caught:
            collect_records(cfg)
        self.assertIn("Kaggle", caught.exception.context["hint"])


class ManifestTests(unittest.TestCase):
    def test_manifest_records_a_file_source_and_no_http(self):
        result = credit_load.IngestResult(
            schema=DB_SCHEMA,
            file_name="cs-training.csv",
            sha256="0" * 64,
            fingerprint="a" * 12,
            file_rows=137,
            pages_total=6,
            pages_fetched=6,
            resumed_from_page=0,
            already_complete=False,
            rows_loaded=136,
            rows_rejected=1,
            reasons={"VALUE_OUT_OF_DOMAIN": 1},
            quarantine_ratio=1 / 137,
            digest={"rows_total": 136},
            migrations_applied=("010_credit_schema.sql",),
        )
        manifest = result.as_manifest(started_at="now", finished_at="later")
        self.assertEqual(manifest["source"]["kind"], "file")
        self.assertEqual(manifest["http"]["requests_this_run"], 0)
        self.assertEqual(manifest["quality"]["rows_loaded"], 136)
        self.assertEqual(manifest["paging"]["pages_total"], 6)
        self.assertEqual(manifest["database"]["table"], f"{DB_SCHEMA}.{DB_TABLE}")
        self.assertEqual(manifest["quality"]["by_reason"]["VALUE_OUT_OF_DOMAIN"], 1)


class ReadViewTests(unittest.TestCase):
    """The read path, against a real view - sqlite's, so the test needs no server.

    The view created here is a *simpler* select than the migration's: what is under
    test is this module's plumbing (identifier validation, chunking, ordering, empty
    result), and the view's own expressions are tested against PostgreSQL.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        credit_db = self.tmp / "credit.db"
        self.engine = sa.create_engine(f"sqlite:///{self.tmp / 'main.db'}")

        # every pooled connection needs its own ATTACH, so that part is an event; the
        # objects and the rows belong to one explicit transaction so they are written
        # once and every connection sees the same committed state
        @sa.event.listens_for(self.engine, "connect")
        def attach(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute(f"ATTACH DATABASE '{credit_db.as_posix()}' AS credit")
            cursor.close()

        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "CREATE TABLE credit.application (row_id integer primary key, "
                    "serious_dlqin_2yrs integer, monthly_income real)"
                )
            )
            connection.execute(
                sa.text(
                    "CREATE VIEW credit.v_model_input AS SELECT row_id, serious_dlqin_2yrs, "
                    "monthly_income, CASE WHEN monthly_income IS NULL THEN 1 ELSE 0 END "
                    "AS income_missing FROM credit.application"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO credit.application VALUES (1, 1, 60.0), (2, 0, NULL)"
                )
            )

    def tearDown(self):
        self.engine.dispose()
        self._tmp.cleanup()

    def test_load_frame_returns_the_view_ordered_by_id(self):
        frame = load_frame(self.engine, "input", schema=DB_SCHEMA)
        self.assertEqual(
            list(frame.columns),
            ["row_id", "serious_dlqin_2yrs", "monthly_income", "income_missing"],
        )
        self.assertEqual(frame["row_id"].tolist(), [1, 2])
        self.assertEqual(frame["income_missing"].tolist(), [0, 1])
        self.assertEqual(row_count(self.engine, "input", schema=DB_SCHEMA), 2)

    def test_chunking_changes_nothing_but_the_number_of_reads(self):
        small = load_frame(self.engine, "input", schema=DB_SCHEMA, chunk_size=1)
        big = load_frame(self.engine, "input", schema=DB_SCHEMA, chunk_size=1000)
        pd.testing.assert_frame_equal(small, big)

    def test_view_names_are_whitelisted(self):
        with self.assertRaises(ValueError):
            resolve_view("application")
        self.assertEqual(resolve_view("input"), f"{DB_SCHEMA}.v_model_input")
        self.assertEqual(set(VIEWS), {"input", "missingness", "staging_input", "raw"})

    def test_a_schema_that_is_not_an_identifier_is_refused(self):
        for bad in ("credit; drop table x", "Credit", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resolve_view("input", bad)

    def test_load_frame_rejects_a_nonsense_chunk_size(self):
        with self.assertRaises(ValueError):
            load_frame(self.engine, "input", schema=DB_SCHEMA, chunk_size=0)


class InsertRecordsTests(unittest.TestCase):
    def test_batches_are_inserted_in_full_and_counted(self):
        engine = sa.create_engine("sqlite:///:memory:")
        table = application_table(None, "application")
        pd.DataFrame({column.name: [] for column in table.columns}).to_sql(
            "application", engine, index=False
        )
        rows = [{"row_id": i, "serious_dlqin_2yrs": i % 2} for i in range(7)]
        with engine.begin() as connection:
            inserted = insert_records(connection, table, rows, batch_size=3)
        self.assertEqual(inserted, 7)
        with engine.connect() as connection:
            stored = pd.read_sql_query(
                "SELECT row_id, serious_dlqin_2yrs FROM application", connection
            )
        self.assertEqual(len(stored), 7)
        self.assertEqual(stored["row_id"].tolist(), list(range(7)))
        engine.dispose()

    def test_batch_size_below_one_is_refused(self):
        engine = sa.create_engine("sqlite:///:memory:")
        table = application_table(None, "application")
        pd.DataFrame({column.name: [] for column in table.columns}).to_sql(
            "application", engine, index=False
        )
        with engine.begin() as connection, self.assertRaises(ValueError):
            insert_records(connection, table, [], batch_size=0)
        engine.dispose()


class DryRunCliTests(unittest.TestCase):
    """The CLI wrapper, which is the only part a person types by hand.

    ``--dry-run`` is the first command anyone runs on their own file, and it is the one
    that must not need a database. These two tests are the difference between that
    working and it failing for a reason the message does not explain.
    """

    def _project(self, tmp: Path) -> Path:
        write_csv(tmp / "cs-training.csv", rows=40)
        lines = [
            "[credit]",
            'csv_path = "cs-training.csv"',
            'sql_migrations_dir = "sql"',
            'curated_dir = "curated"',
            'raw_pages_dir = "pages"',
            'quarantine_dir = "quarantine"',
        ]
        (tmp / "credit.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return tmp

    def test_dry_run_counts_the_file_it_was_pointed_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(Path(tmp))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = credit_load.main(
                    [
                        "--dry-run",
                        "--credit-config",
                        str(project / "credit.toml"),
                    ]
                )
            line = out.getvalue().strip()
            self.assertEqual(code, 0, line)
            self.assertIn("file=cs-training.csv", line)
            self.assertIn("rows=40 valid=40 quarantined=0", line)
            # csv_path was relative to the config file, so reaching the rows at all
            # proves the anchoring the README promises for both stages. A dry run
            # quarantines and pages but must not publish a manifest.
            self.assertFalse((project / "curated").exists())
            self.assertTrue((project / "quarantine" / "quarantine.jsonl").is_file())

    def test_missing_config_exits_with_the_hint_and_no_boilerplate(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copyfile(
                HERE.parent / "config" / "credit.example.toml",
                Path(tmp) / "credit.example.toml",
            )
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = credit_load.main(
                    ["--dry-run", "--credit-config", f"{tmp}/credit.toml"]
                )
            message = err.getvalue()
            self.assertNotEqual(code, 0)
            self.assertIn("credit.example.toml", message)
            self.assertNotIn("context={}", message)


class MakeFixtureTests(unittest.TestCase):
    """The fixture writer is part of the evidence story, so it is tested like the rest.

    A run that "only" exercised the pipeline on synthetic data is worth reporting if and
    only if the synthetic file has the same *shape* as the real one - and the shape that
    took two rounds to learn is the empty first header cell.
    """

    def setUp(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "make_fixture", HERE.parent / "scripts" / "make_fixture.py"
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_the_default_seed_is_the_generator_default(self):
        signature = inspect.signature(write_csv)
        self.assertEqual(self.module.DEFAULT_SEED, signature.parameters["seed"].default)

    def test_it_blanks_only_the_first_header_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cs-training.csv"
            write_csv(path, rows=40)
            with path.open(newline="", encoding="utf-8") as handle:
                before = handle.readlines()
            self.module.blank_index_header(path)
            with path.open(newline="", encoding="utf-8") as handle:
                after = handle.readlines()
            self.assertEqual(len(before), len(after))
            self.assertEqual(
                next(csv.reader([after[0]]))[0], "", "the id cell must be empty"
            )
            self.assertEqual(after[1:], before[1:], "no data row may change")
            self.assertEqual(
                next(csv.reader([after[0]]))[1:],
                next(csv.reader([before[0]]))[1:],
                "the other names must survive untouched",
            )
            # a second call is a no-op, so a re-run cannot drift the shape
            digest = self.module.sha256(path)
            self.module.blank_index_header(path)
            self.assertEqual(self.module.sha256(path), digest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
