"""The feature layer's sequencing, against a connection that is not a database.

``features.build`` is the one place the stage can still fail closed: it hashes the SQL before it
runs it, hands the frame's columns to the guard that lives inside the database, refuses a frame
that is too small or has no key, and only then judges the measured duration against the ceiling.
Every one of those is a claim about *order and refusal*, so all of them are testable with a stub
that records statements; none of them is a claim about how fast PostgreSQL is, and the stub has no
opinion to offer on that. The number itself stays where it belongs: a run on the student's
    database, recorded as unverified here until that run happens -- the Persian marker this stage
    writes is named in the checklist rather than repeated in a Python source file, because
    test_stage4_layout keeps Persian out of this directory.
"""

from __future__ import annotations

import dataclasses
import hashlib
import unittest
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))
# deliberately no stage-1 path: sys.path is process-wide, and putting stage-1/src on it here
# would make the chain's "ingest" step look ready to every other test in this directory -- see
# ChainPlanTest in test_capstone_cli.py, which pins that the plan blocks before ingest

from capstone import features
from capstone.audit import parse_audit
from capstone.errors import BudgetError, ConfigError, InputError
from capstone.pending import is_pending


class RecordingConnection:
    """A DBAPI-shaped stub that answers the queries ``build`` asks, and nothing else."""

    def __init__(
        self,
        *,
        source_columns: list[str] | None = None,
        columns: list[str] | None = None,
        rows: int = 99_281,
        clock: object = None,
        audit_rows: int = 53,
    ) -> None:
        self.statements: list[str] = []
        #: how many rows ``capstone.column_audit`` holds; 0 is the fresh-database case the
        #: pre-check exists for, so a test can dial it without faking a whole parser
        self.audit_rows = audit_rows
        self.commits = 0
        self.closed = False
        self.rows = rows
        self.columns = columns if columns is not None else ["order_id", *FEATURE_NAMES]
        self.source_columns = (
            source_columns if source_columns is not None else [f"orders.{name}" for name in COLUMN_SEEDS]
        )
        self._clock = clock

    def cursor(self) -> "RecordingCursor":
        return RecordingCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


class RecordingCursor:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection
        self._rows: list[tuple] = []

    def execute(self, sql: str, params=None) -> None:
        self.connection.statements.append(sql)
        if "count(*) FROM capstone.column_audit" in sql:
            self._rows = [(self.connection.audit_rows,)]
        elif "information_schema.columns" in sql:
            self._rows = [(name,) for name in self.connection.columns]
        elif "require_allowed" in sql:
            self._rows = [(None,)]  # the guard returns void; a row is a successful no-op
        elif "FROM capstone.v_feature_frame AS f" in sql:
            self._rows = [(self.connection.rows,)]
        elif "(SELECT * FROM capstone.v_feature_frame)" in sql:
            self._rows = [(self.connection.rows,)]
        elif "relname || '.' || attribute.attname" in sql:
            self._rows = [(name,) for name in self.connection.source_columns]
        else:
            self._rows = []

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self) -> tuple:
        return self._rows[0] if self._rows else (None,)

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


FEATURE_NAMES = [f"feature_{index}" for index in range(12)]
COLUMN_SEEDS = ["order_approved_at", "customer_id", "price", "freight_value"]


def manifest_path(root: Path) -> Path:
    return root / "docs" / "evidence" / "sql-checksums.txt"


def stage_config(root: Path, *, migrations: Path | None = None):
    """The tracked contract, re-rooted at a temp stage so paths resolve the way the loader wants.

    Self-contained rather than imported from ``tests/helpers.py`` for the same reason the guard
    module is: this file is also run as ``python -m unittest tests.test_capstone_features``, where
    ``helpers`` is not on the path.
    """
    from capstone.config import load_contract

    config = load_contract(STAGE / "config" / "capstone.example.toml")
    features_section = dataclasses.replace(
        config.features, sql_migrations_dir=migrations or (root / "sql")
    )
    return dataclasses.replace(config, root=root, features=features_section)


class BuildSequencingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "sql").mkdir()
        for name in ("001_first.sql", "002_second.sql"):
            (self.root / "sql" / name).write_text("CREATE OR REPLACE VIEW capstone.v AS SELECT 1;\n")
        manifest = self.root / "docs" / "evidence" / "sql-checksums.txt"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            "".join(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  sql/{path.name}\n"
                for path in sorted((self.root / "sql").glob("*.sql"))
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def build(self, connection=None, *, clock=None, repeat=2, audit=(), uri="postgresql://x/y"):
        return features.build(
            stage_config(self.root),
            list(audit),
            uri=uri,
            connect=lambda _uri: connection or RecordingConnection(),
            clock=clock or (lambda: 0.0),
            repeat=repeat,
            manifest=manifest_path(self.root),
        )

    def test_the_migrations_are_applied_in_file_order_before_anything_is_counted(self):
        """The order is the versioning: 001 then 002, whole files, before any query reads them."""
        connection = RecordingConnection()
        self.build(connection)
        self.assertEqual(
            connection.statements[:2],
            [
                (self.root / "sql" / "001_first.sql").read_text(),
                (self.root / "sql" / "002_second.sql").read_text(),
            ],
        )
        counted = next(
            i
            for i, sql in enumerate(connection.statements)
            if "FROM capstone.v_feature_frame AS f" in sql
        )
        self.assertLess(1, counted, msg="a count read before the migrations would describe the old view")
        # one commit after the DDL, so a guard that looks up a view sees the committed definition
        self.assertGreaterEqual(connection.commits, 1)

    def test_the_database_guard_runs_before_the_counts(self):
        connection = RecordingConnection()
        self.build(connection)
        guard = next(i for i, sql in enumerate(connection.statements) if "require_allowed" in sql)
        counted = next(
            i for i, sql in enumerate(connection.statements) if "FROM capstone.v_feature_frame AS f" in sql
        )
        self.assertLess(guard, counted, msg="a frame nobody audited must not reach the report")

    def test_the_closure_the_guard_uses_is_the_one_the_tests_use(self):
        """One dependency walk, not two: the guard and the live test read the same CTE."""
        self.assertIn(features.DEPENDENCY_CLOSURE_SQL.strip(), features.AUDIT_GUARD_SQL)
        self.assertIn(features.DEPENDENCY_CLOSURE_SQL.strip(), features.SOURCE_COLUMNS_SQL)

    def test_edited_sql_stops_the_run_before_a_connection_is_open(self):
        (self.root / "sql" / "001_first.sql").write_text("DROP VIEW capstone.v;\n", encoding="utf-8")
        opened: list[str] = []
        with self.assertRaises(InputError) as caught:
            features.build(
                stage_config(self.root),
                [],
                uri="postgresql://x/y",
                connect=lambda uri: opened.append(uri) or RecordingConnection(),
                manifest=manifest_path(self.root),
            )
        self.assertEqual(opened, [], msg="a mismatched manifest must refuse before connecting")
        self.assertIn("does not match the manifest", str(caught.exception))

    def test_a_missing_manifest_is_a_refusal_not_a_pass(self):
        manifest_path(self.root).unlink()
        with self.assertRaises(InputError) as caught:
            self.build()
        self.assertIn("is missing", str(caught.exception))

    def test_an_empty_sql_directory_is_refused(self):
        for path in (self.root / "sql").glob("*.sql"):
            path.unlink()
        with self.assertRaises(InputError) as caught:
            self.build()
        self.assertIn("empty one is not a frame", str(caught.exception))

    def test_a_frame_that_reads_no_source_column_is_refused(self):
        with self.assertRaises(InputError) as caught:
            self.build(RecordingConnection(source_columns=[]))
        self.assertIn("no column of the source schema", str(caught.exception))

    def test_a_frame_with_too_few_features_is_refused(self):
        columns = ["order_id", "feature_0", "feature_1"]
        with self.assertRaises(InputError) as caught:
            self.build(RecordingConnection(columns=columns))
        self.assertIn("at least 10", str(caught.exception))

    def test_a_frame_without_the_order_key_is_refused(self):
        columns = [*FEATURE_NAMES]  # twelve features, no key
        with self.assertRaises(InputError) as caught:
            self.build(RecordingConnection(columns=columns))
        self.assertIn("no order_id", str(caught.exception))

    def test_an_empty_frame_is_refused(self):
        with self.assertRaises(InputError) as caught:
            self.build(RecordingConnection(rows=0))
        self.assertIn("is empty", str(caught.exception))

    def test_the_audit_the_caller_hands_over_is_cross_checked(self):
        """The CSV and the table can disagree; the frame is judged against the copy in the repo."""
        audit = parse_audit(
            "table,column,available_at,at_approval,source,verdict\n"
            + "\n".join(
                f"orders,{name},at approval,true,witness,{verdict}"
                for name, verdict in zip(COLUMN_SEEDS, ["allowed", "allowed", "allowed", "forbidden"])
            ),
            origin="test",
        )
        with self.assertRaises(Exception) as caught:
            self.build(audit=audit)
        self.assertIn("orders.freight_value", str(caught.exception))

    def test_the_duration_comes_from_the_clock_and_judges_the_mean(self):
        # seconds, because the injected clock is monotonic(): 0.010 s is what the brief counts
        ticks = iter([0.0, 0.010, 0.010, 0.020])
        metadata = self.build(clock=lambda: next(ticks))
        self.assertEqual(metadata["query_ms_mean"], 10.0)
        self.assertEqual(metadata["cold_ms"], 10.0)
        self.assertEqual(metadata["query_ms_runs"], [10.0, 10.0])
        self.assertEqual(metadata["ceiling_ms"], 500)

    def test_a_slow_frame_raises_the_budget_error_the_brief_asks_for(self):
        ticks = iter([0.0, 0.900, 0.900, 1.800])
        with self.assertRaises(BudgetError) as caught:
            self.build(clock=lambda: next(ticks))
        self.assertIn("500 ms", str(caught.exception))
        self.assertIn("5.3", str(caught.exception))

    def test_the_connection_is_closed_whatever_happens(self):
        connection = RecordingConnection()
        self.build(connection)
        self.assertTrue(connection.closed)
        slow = RecordingConnection()
        with self.assertRaises(BudgetError):
            features.build(
                dataclasses.replace(
                    stage_config(self.root),
                    features=dataclasses.replace(
                        stage_config(self.root).features, feature_query_ms=1
                    ),
                ),
                [],
                uri="postgresql://x/y",
                connect=lambda _uri: slow,
                clock=iter([0.0, 5.0]).__next__,
                repeat=1,
                manifest=manifest_path(self.root),
            )
        self.assertTrue(slow.closed, msg="a refusal must not leak a connection")

    def test_no_uri_is_a_refusal_that_names_the_variable(self):
        import os

        saved = os.environ.pop("CAPSTONE_DATABASE_URI", None)
        try:
            with self.assertRaises(ConfigError) as caught:
                features.build(
                    stage_config(self.root),
                    [],
                    connect=lambda _u: RecordingConnection(),
                    uri=None,
                    manifest=manifest_path(self.root),
                )
            self.assertIn("CAPSTONE_DATABASE_URI", str(caught.exception))
        finally:
            if saved is not None:
                os.environ["CAPSTONE_DATABASE_URI"] = saved

    def test_repeat_below_one_is_refused_before_any_work(self):
        with self.assertRaises(InputError):
            self.build(repeat=0)

    def test_the_default_manifest_travels_with_the_code_not_with_the_config(self):
        """``--config`` may live anywhere, so the manifest cannot be sought beside it.

        A root read off the config would look for docs/evidence/ next to whatever file the user
        pointed at, find nothing, and refuse an intact tree -- or, worse, find a stale manifest
        from a copied directory and pass. The default is derived from the package instead.
        """
        default = features.STAGE_ROOT / features.CHECKSUM_MANIFEST
        self.assertEqual(default, STAGE / "docs" / "evidence" / "sql-checksums.txt")
        self.assertTrue(default.is_file(), msg=f"the default must resolve: {default}")
        # and it must be *satisfied* by the shipped tree, or the default is a trap: a run against
        # the repository as committed would refuse before it ever connected
        self.assertEqual(
            [],
            features.checksum_mismatches(features.sql_migrations(STAGE / "sql"), manifest=default),
        )

    def test_the_metadata_is_the_shape_the_model_step_consumes(self):
        metadata = self.build()
        self.assertEqual(
            sorted(metadata),
            [
                "ceiling_ms",
                "cold_ms",
                "columns",
                "features",
                "frame",
                "migrations_applied",
                "query_ms_mean",
                "query_ms_runs",
                "rows",
                "source_columns_read",
            ],
        )
        self.assertEqual(metadata["frame"], features.FRAME_VIEW)
        self.assertEqual(metadata["features"], len(FEATURE_NAMES))
        self.assertEqual(metadata["migrations_applied"], ["001_first.sql", "002_second.sql"])


class LayerStatusTests(unittest.TestCase):
    def test_the_feature_layer_is_no_longer_a_placeholder(self):
        """The chain reports readiness from the code, so this is the flip worth pinning.

        ROADMAP phase 4 owns the body; the guards in this file are what makes "it exists" mean
        "it refuses when it should" rather than "it returns something".
        """
        self.assertFalse(is_pending(features.build))
        self.assertTrue(callable(features.build))

    def test_the_ceiling_and_the_minimum_are_the_brief_s_numbers(self):
        self.assertEqual(features.MINIMUM_FEATURES, 10)
        self.assertEqual(
            sorted(features.TECHNICAL_COLUMNS),
            ["customer_id", "order_approved_at", "order_id", "order_status"],
        )


class DriverTests(unittest.TestCase):
    """Which module a URI means, and what the URI must look like once that is decided.

    The layer used to ``import psycopg2`` outright while the project's lock ships ``psycopg==3.3.6``
    (stages 2 and 3 pin it, ``../stage-1-kiarash-akbari/config/default.toml`` asks for ``postgresql+psycopg://``). On
    the environment every other stage installs, that meant the feature step and the live guards both
    died blaming a driver nobody in the repository uses -- so the mapping is the thing under test,
    not an implementation detail of a function that cannot be reached offline.
    """

    def test_the_dialect_names_the_module_and_is_then_dropped(self):
        self.assertEqual(
            features.driver_for("postgresql+psycopg://kiarash@localhost:5432/olist"),
            ("psycopg", "postgresql://kiarash@localhost:5432/olist"),
        )
        self.assertEqual(
            features.driver_for("postgresql+psycopg2://u@h/db"),
            ("psycopg2", "postgresql://u@h/db"),
        )

    def test_a_bare_postgresql_uri_follows_the_lock(self):
        """No dialect is not "no driver": it resolves to the one this project ships."""
        self.assertEqual(
            features.driver_for("postgresql://u@h:5433/olist"),
            ("psycopg", "postgresql://u@h:5433/olist"),
        )

    def test_a_dialect_left_on_the_uri_is_the_bug_this_strips(self):
        # psycopg and psycopg2 both reject "postgresql+psycopg://..." as an unknown scheme, so a
        # caller that forwards the config value verbatim gets an error about URIs, not about drivers
        module, dsn = features.driver_for("postgresql+psycopg://u@h/db")
        self.assertFalse(dsn.startswith("postgresql+"))
        self.assertEqual(module, "psycopg")

    def test_an_unshipped_driver_is_refused_by_name(self):
        with self.assertRaises(ConfigError) as caught:
            features.driver_for("postgresql+asyncpg://u@h/db")
        self.assertIn("asyncpg", str(caught.exception))
        self.assertIn("does not ship", str(caught.exception))

    def test_the_wrong_database_kind_says_nothing_was_guessed(self):
        with self.assertRaises(ConfigError) as caught:
            features.driver_for("sqlite:///olist.db")
        self.assertIn("nothing was guessed", str(caught.exception))

    def test_a_value_that_is_not_a_uri_at_all(self):
        with self.assertRaises(ConfigError) as caught:
            features.driver_for("localhost:5432/olist")
        self.assertIn("not a connection uri", str(caught.exception))

    def test_a_missing_driver_names_the_lock_and_the_way_out(self):
        """The refusal is the deliverable here: the code cannot install anything for the user."""
        from unittest import mock

        with mock.patch.object(features.importlib, "import_module") as fake:
            fake.side_effect = ModuleNotFoundError("No module named 'psycopg'")
            with self.assertRaises(ConfigError) as caught:
                features.connect("postgresql+psycopg://u@h/db", purpose="run the live SQL guards")
        message = str(caught.exception)
        self.assertIn("psycopg", message)
        self.assertIn("psycopg==3.3.6", message)
        self.assertIn("postgresql+psycopg2://", message, msg="the way out belongs in the error")
        self.assertIn("run the live SQL guards", message)
        fake.assert_called_once_with("psycopg")

    def test_autocommit_is_only_what_the_caller_asked_for(self):
        """Production paths commit explicitly; only the live tests may autocommit DDL."""
        from unittest import mock

        class Probe:
            autocommit = False

        probe = Probe()
        module = mock.Mock()
        module.connect.return_value = probe
        with mock.patch.object(features.importlib, "import_module", return_value=module):
            features.connect("postgresql://u@h/db")
        self.assertFalse(probe.autocommit)
        with mock.patch.object(features.importlib, "import_module", return_value=module):
            features.connect("postgresql://u@h/db", autocommit=True)
        self.assertTrue(probe.autocommit)


if __name__ == "__main__":
    unittest.main()
