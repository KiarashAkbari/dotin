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

import ast
import dataclasses
import hashlib
import importlib.util
import os
import subprocess
import unittest
from unittest import mock
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

#: the performance harness is a script, not a module: loaded by path so the plan parser can be
#: tested on a canned EXPLAIN, which is the half of the log that a reviewer can check at all
_SPEC = importlib.util.spec_from_file_location(
    "frame_performance_run", STAGE / "docs" / "evidence" / "frame-performance-run.py"
)
perf = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(perf)
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

    def test_the_duration_comes_from_the_clock_and_judges_the_best_run(self):
        # seconds, because the injected clock is monotonic(): 0.010 s is what the brief counts
        ticks = iter([0.0, 0.010, 0.010, 0.020])
        metadata = self.build(clock=lambda: next(ticks))
        self.assertEqual(metadata["query_ms_mean"], 10.0)
        self.assertEqual(metadata["query_ms_best"], 10.0)
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

    def test_build_times_exactly_the_statement_the_performance_log_measures(self):
        """The harness and the code must time one query, not two that look alike.

        Checklist 5.3 is closed by a log, so the sentence "376 ms, under the ceiling" is only about
        ``capstone features`` if the measured text is the text the code runs. A private copy in the
        harness -- one that drops the count wrapper, or adds a ``LIMIT`` to keep the run quick --
        would produce a green log about a query nobody ships.
        """
        timed = features.FRAME_TIMING_SQL.format(frame=features.FRAME_VIEW)
        read = features.FRAME_READ_SQL.format(frame=features.FRAME_VIEW)
        connection = RecordingConnection()
        self.build(connection=connection, repeat=3)
        self.assertEqual(connection.statements.count(timed), 3, msg="one timing per repeat, no more")
        self.assertNotIn(read, connection.statements, msg="the shipped path does not pay for transfer")

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
                "judged_on",
                "migrations_applied",
                "query_ms_best",
                "query_ms_mean",
                "query_ms_runs",
                "rows",
                "source_columns_read",
            ],
        )
        self.assertEqual(metadata["frame"], features.FRAME_VIEW)
        self.assertEqual(metadata["features"], len(FEATURE_NAMES))
        self.assertEqual(metadata["migrations_applied"], ["001_first.sql", "002_second.sql"])


class FramePerformanceHarnessTests(unittest.TestCase):
    """The two scripts behind the performance log, read as sources and run without a database.

    Neither script can be tested *as a measurement* here -- a duration belongs to a server, and this
    suite has none. What can be tested is everything that makes a measured number worth reading: that
    the query is the shipped one, that the scale is stated rather than assumed, that a scratch-only
    script refuses a database that holds rows, and that a refusal arrives as a sentence instead of a
    traceback. Each of those is a claim about text or about an exit code, and both are cheap.
    """

    HARNESS = STAGE / "docs" / "evidence" / "frame-performance-run.py"
    SEEDER = STAGE / "docs" / "evidence" / "frame-performance-synthetic.py"
    #: the questions the harness may ask that ``src/`` has no reason to ask: which server is this, how
    #: big is each table the frame reads, and whether the planner has statistics for them. Everything
    #: about the frame itself has to come from a constant in ``capstone.features`` -- the test below
    #: fails on any other statement, which is how a new diagnostic has to be *argued for* here rather
    #: than quietly added to the log
    DIAGNOSTICS = (
        "SELECT current_database()",
        "SELECT table_name FROM information_schema.columns",
        "SELECT count(*) FROM olist.",
        "SELECT count(*), count(*) FILTER (WHERE last_analyze IS NOT NULL",
    )
    #: the statements the harness shares with the shipped code, each with the call site that proves
    #: it is used and how many of them there are: timing once for the measurement and twice for the
    #: plan, read once for the transfer and once for its plan. A count that moves is a call site added
    #: or removed, which is exactly the edit that turns a log into a different claim.
    #: shared constants that exist for the log and must never be sent by ``src/``
    LOG_ONLY = ("FRAME_READ_SQL",)
    SHARED = {
        "FRAME_TIMING_SQL": ("FRAME_TIMING_SQL.format(frame=FRAME_VIEW)", 3),
        "FRAME_READ_SQL": ("FRAME_READ_SQL.format(frame=FRAME_VIEW)", 2),
        "FRAME_COUNT_SQL": ("FRAME_COUNT_SQL.format(frame=FRAME_VIEW)", 1),
        # parameterised rather than formatted: the schema and the view are its two %s
        "FRAME_COLUMNS_SQL": ("rows(cursor, FRAME_COLUMNS_SQL,", 1),
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.harness = cls.HARNESS.read_text(encoding="utf-8")
        cls.seeder = cls.SEEDER.read_text(encoding="utf-8")

    def run_script(self, path: Path, *, env: dict | None = None, args: list | None = None):
        base = {key: value for key, value in os.environ.items() if not key.startswith("CAPSTONE_")}
        base["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, str(path), *(args or [])],
            cwd=str(STAGE),
            env=dict(base, **(env or {})),
            capture_output=True,
            text=True,
        )

    def statements(self, source: str) -> list[str]:
        """Every string the script can send to the server, f-strings included.

        Parsed with ``ast`` rather than scanned line by line: the first draft of this test looked at
        lines that began with ``SELECT`` and passed a harness whose real queries were all arguments
        to a helper. A green test that cannot see the code it guards is worse than none.
        """
        found: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.lstrip().startswith("SELECT"):
                    found.append(" ".join(node.value.split()))
            elif isinstance(node, ast.JoinedStr):  # an f-string: the literal head is the query
                head = node.values[0]
                if isinstance(head, ast.Constant) and isinstance(head.value, str):
                    if head.value.lstrip().startswith("SELECT"):
                        found.append(" ".join(head.value.split()))
        return found

    def test_the_harness_imports_the_query_instead_of_restating_it(self) -> None:
        """The log's numbers are only about ``capstone features`` if the text is the shipped text.

        A private copy in the harness -- one that drops the count wrapper, or adds a ``LIMIT`` to keep
        the run quick -- would produce a green log about a query nobody ships, and the ceiling verdict
        would then be about a statement the model step never runs.
        """
        for name in ("FRAME_VIEW", "MINIMUM_FEATURES", "TECHNICAL_COLUMNS"):
            self.assertIn(name, self.harness, msg=f"the harness must use capstone's {name}")
        # a literal frame name would be a second definition of what is measured, and it would not
        # move when the view is renamed -- which is how a log ends up describing another tree
        self.assertNotIn('"capstone.v_feature_frame"', self.harness)
        for name, (call_site, uses) in self.SHARED.items():
            statement = getattr(features, name)
            self.assertNotIn(
                statement, self.harness, msg=f"{name} is imported to be used, not copied into the log"
            )
            self.assertEqual(
                self.harness.count(call_site), uses, msg=f"{call_site} should appear {uses} time(s)"
            )

    def test_the_harness_owns_no_query_beyond_its_diagnostics(self) -> None:
        """Anything the harness sends is either a shared constant or a documented diagnostic.

        The allowlist is short on purpose: the identity of the server, the row counts of the source
        tables, and whether those tables have statistics are the only facts a performance log needs
        that the shipped code has no reason to care about. A third one -- a filtered frame, a LIMIT, a different view -- means the log is measuring
        something other than what ``capstone features`` runs, and the ceiling verdict moves with it.
        """
        # no filter on anything the frame is measured with: a harness that narrows what it times
        # produces a fast plan and a true sentence about the wrong query. The rule is per-line rather
        # than per-file because the statistics diagnostic legitimately has its own WHERE, and a guard
        # that forbids that would only teach the next person to write the query twice
        measuring = [
            line
            for line in self.harness.splitlines()
            if any(name in line for name in self.SHARED) or "FROM olist." in line
        ]
        self.assertGreaterEqual(len(measuring), 7, msg="the call sites vanished; check the regex")
        for line in measuring:
            for forbidden in ("WHERE", "LIMIT", "OFFSET", "TABLESAMPLE", "ORDER BY"):
                self.assertNotIn(
                    forbidden, line, msg=f"a narrowed measurement statement: {line.strip()}"
                )
        shared = [getattr(features, name) for name in self.SHARED]
        found = self.statements(self.harness)
        self.assertTrue(found, msg="the harness stopped sending SELECTs; this test needs updating")
        for statement in found:
            collapsed = " ".join(statement.split())
            self.assertTrue(
                collapsed.startswith(self.DIAGNOSTICS) or collapsed.startswith(tuple(shared)),
                msg=f"a statement that is neither diagnostic nor shared: {collapsed}",
            )

    def test_the_shipped_module_sends_the_shared_statements_too(self) -> None:
        """Guard against the refactor the other way: constants the code only *declares*.

        If ``build`` kept its own copy and the harness imported the constant, the two would drift
        silently and every test above would pass. So each statement has to appear in a call, not just
        at module level -- counting on both sides is what makes the pair mean something.
        """
        shipped = (STAGE / "src" / "capstone" / "features.py").read_text(encoding="utf-8")
        for name in self.SHARED:
            if name in self.LOG_ONLY:
                # the transfer statement is the log's alone, and that is the point: a ``SELECT *`` in
                # the build path would make client speed part of the ceiling verdict
                self.assertEqual(
                    shipped.count(name),
                    1,
                    msg=f"{name} must stay out of the shipped path, declaration aside",
                )
                continue
            self.assertGreaterEqual(
                shipped.count(name),
                2,
                msg=f"{name} is declared in features.py and never sent there",
            )
        self.assertIn(
            "FRAME_TIMING_SQL.format(frame=FRAME_VIEW)",
            shipped,
            msg="build must time the constant the log times, not a literal that happens to match",
        )

    def test_the_plan_is_ranked_by_work_done_not_by_work_reported(self) -> None:
        """``hotspots`` has to subtract the child, or every log names the root and says nothing.

        The canned plan is the shape that fooled the first draft: an ``Aggregate`` of 100 ms whose
        only child is a ``Hash Join`` of 96 ms. Ranking by total time would blame the aggregate,
        and the advice would then propose an index for a node that is only waiting.
        """
        plan = [
            {
                "Plan": {
                    "Node Type": "Aggregate",
                    "Actual Total Time": 100.0,
                    "Actual Rows": 1,
                    "Actual Loops": 1,
                    "Plans": [
                        {
                            "Node Type": "Hash Join",
                            "Relation Name": "order_items",
                            "Actual Total Time": 96.0,
                            "Actual Rows": 48_750,
                            "Actual Loops": 2,
                            "Shared Hit Blocks": 212_468,
                            "Shared Read Blocks": 0,
                        }
                    ],
                }
            }
        ]
        hot = perf.hotspots(plan)
        self.assertEqual(hot[0]["node"], "Hash Join", msg="the root is not the work when its child is")
        self.assertAlmostEqual(hot[0]["self_ms"], 96.0, places=3)
        self.assertEqual(perf.self_time(plan[0]["Plan"]), 4.0)
        self.assertAlmostEqual(hot[0]["share"], 0.96, places=3)

    def test_the_advice_table_is_closed_and_only_fires_over_the_ceiling(self) -> None:
        hot = [{"node": "Sort", "relation": "orders", "self_ms": 90.0, "share": 0.9}]
        self.assertEqual(perf.advice(hot, None), [], msg="a query inside the ceiling gets no proposal")
        lines = perf.advice(hot, 12.0)
        self.assertTrue(any("work_mem" in line for line in lines), msg=lines)
        self.assertTrue(all("index" not in line for line in perf.advice(hot, None)))
        unknown = perf.advice([{"node": "Custom Scan", "relation": "", "self_ms": 1.0, "share": 1.0}], 5.0)
        self.assertTrue(any("advice table" in line for line in unknown), msg="an unmapped node is said")

    def test_temp_space_is_reported_whether_or_not_the_ceiling_held(self) -> None:
        """``spill`` reads the root's inclusive counters, and names only the operator that spilled.

        Two mistakes the same plan teaches. Summing the per-node temp counters triples the number,
        because a Gather and the hash under it report the same blocks; and listing every node with a
        non-zero counter calls the waiting parent a spiller, which sends a reader to the wrong
        ``work_mem``. Both are pinned here rather than discovered on the real server, because the log
        has to be trustworthy to whoever reads it next.
        """
        plan = [
            {
                "Plan": {
                    "Node Type": "Finalize Aggregate",
                    "Actual Total Time": 500.0,
                    "Temp Read Blocks": 2232,
                    "Temp Written Blocks": 2938,
                    "Plans": [
                        {
                            "Node Type": "Gather",
                            "Temp Read Blocks": 2232,
                            "Temp Written Blocks": 2938,
                            "Plans": [
                                {
                                    "Node Type": "Hash",
                                    "Hash Batches": 2,
                                    "Temp Read Blocks": 2232,
                                    "Temp Written Blocks": 2938,
                                }
                            ],
                        },
                        {
                            "Node Type": "Sort",
                            "Relation Name": "order_items",
                            "Sort Space Type": "Disk",
                            "Temp Written Blocks": 842,
                        },
                    ],
                }
            }
        ]
        info = perf.spill(plan)
        self.assertEqual(info["temp_read"], 2232, msg="the root's inclusive count, not three copies")
        self.assertEqual(info["temp_written"], 2938)
        self.assertEqual(info["batches"], 2, msg="the key is Hash Batches, and it is read from JSON")
        self.assertEqual(
            info["spilling"],
            ["Hash", "Sort on order_items"],
            msg="a parent that only inherits its child's counters is not the spiller",
        )
        clean = perf.spill({"Plan": {"Node Type": "Aggregate"}})
        self.assertIsNone(clean["batches"], msg="no hash nodes, so no batch count to report")
        self.assertEqual(clean["temp_read"], 0)
        self.assertEqual(clean["spilling"], [])

    def test_no_minimum_is_repeated_where_the_code_already_states_it(self) -> None:
        """The log compares against ``MINIMUM_FEATURES``; a literal 10 in the harness could drift.

        The brief's number lives in one place. A harness carrying its own copy would keep printing a
        pass for nine columns after the constant moved, which is the failure mode the checksum
        manifest was written to make impossible for SQL.
        """
        self.assertIn("MINIMUM_FEATURES", self.harness)
        self.assertNotIn("< 10", self.harness)
        self.assertNotIn(">= 10", self.harness)

    def test_a_missing_database_is_a_message_and_exit_zero(self) -> None:
        proc = self.run_script(self.HARNESS)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-500:])
        self.assertIn("not measured", proc.stdout)
        self.assertIn("CAPSTONE_DATABASE_URI", proc.stdout)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_the_seeder_refuses_a_database_it_was_not_told_to_fill(self) -> None:
        proc = self.run_script(self.SEEDER)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-500:])
        self.assertIn("CAPSTONE_SMOKE_ALLOW_DESTROY=1", proc.stdout)
        self.assertIn("Nothing was written", proc.stdout)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    def test_the_seeder_looks_for_rows_before_it_creates_any(self) -> None:
        """Order matters here more than anywhere else in the stage: the check is the only promise.

        There is no rollback in this script -- it is autocommit, because a 100k-row generate_series
        inside one transaction would hold locks on somebody's scratch cluster for minutes. So the
        refusal has to come first, and it must not be able to "clean up" what it finds: no TRUNCATE,
        no DELETE, in either direction.
        """
        self.assertLess(
            self.seeder.index("the target already holds rows"),
            self.seeder.index("generate(cursor, args.orders"),
            msg="a row check after the inserts is a message printed over a filled database",
        )
        executable = self.seeder.replace(self.seeder[: self.seeder.index("\n\n\ndef ")], "")
        self.assertIn(
            "if occupied:",
            self.seeder,
            msg="a guard behind a constant is not a guard; this test reads order, not intent",
        )
        for forbidden in ("TRUNCATE", "DELETE FROM"):
            self.assertNotIn(
                forbidden, executable, msg=f"{forbidden} would erase data, not test it"
            )
        self.assertIn("olist", self.seeder, msg="the refusal is about stage-2's schema specifically")

    def test_the_seeder_names_the_scale_it_wrote(self) -> None:
        """A performance log without counts in it is a number attached to no claim."""
        self.assertIn("the counts this run wrote", self.seeder)
        self.assertIn("--orders", self.seeder)
        self.assertIn("--zip-prefixes", self.seeder)


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


class DriverErrorTypeTests(unittest.TestCase):
    """Which exception class a guard test should expect, decided by the URI and nothing else.

    A live test that wants to assert on a refused ``RAISE EXCEPTION`` needs the driver's class for
    it, and the obvious code -- try ``psycopg``, else ``psycopg2`` -- reads the interpreter. On a
    machine with both installed, the connection follows ``CAPSTONE_DATABASE_URI`` and the assertion
    follows whichever module imported first, so the guard's exception escapes ``assertRaises`` and
    the failure looks like a broken test. Reported from the student's database after the stale
    ``timestamptz`` overloads were dropped: one red test, and it was not the leak that was wrong.
    """

    @staticmethod
    def _driver(name: str, *, with_errors: bool = True):
        """A stand-in for a driver module: one exception class, no database behind it."""
        from types import ModuleType, SimpleNamespace

        module = ModuleType(name)
        if with_errors:
            module.errors = SimpleNamespace(RaiseException=type(f"{name}.RaiseException", (Exception,), {}))
        module.ProgrammingError = type(f"{name}.ProgrammingError", (Exception,), {})
        return module

    def resolve_with(self, **modules):
        """Point :func:`capstone.features._driver_and_dsn` at fakes instead of an import."""
        from unittest import mock

        return mock.patch.object(
            features.importlib, "import_module", side_effect=lambda name: modules[name]
        )

    def test_the_uri_picks_the_class_not_the_interpreter(self):
        """Two URIs, two drivers, two different classes -- from the same installed interpreter."""
        psycopg3 = self._driver("psycopg")
        psycopg2 = self._driver("psycopg2")
        modules = {"psycopg": psycopg3, "psycopg2": psycopg2}
        with self.resolve_with(**modules):
            got_new = features.raise_exception_type("postgresql+psycopg://u@h/db")
            got_old = features.raise_exception_type("postgresql+psycopg2://u@h/db")
            bare = features.raise_exception_type("postgresql://u@h/db")
        self.assertIs(got_new, psycopg3.errors.RaiseException)
        self.assertIs(got_old, psycopg2.errors.RaiseException)
        self.assertIsNot(got_new, got_old, msg="the two drivers share a class, so one resolves")
        self.assertIs(bare, got_new, msg="a bare URI follows the lock, as `driver_for` already says")

    def test_a_driver_without_an_errors_module_falls_back(self):
        """``psycopg2.errors`` is a 2.8 thing; older releases raise ``ProgrammingError``."""
        ancient = self._driver("psycopg2", with_errors=False)
        with self.resolve_with(psycopg2=ancient):
            self.assertIs(
                features.raise_exception_type("postgresql+psycopg2://u@h/db"), ancient.ProgrammingError
            )

    def test_the_refusal_when_no_driver_is_installed_names_the_uri(self):
        """The same error a connection would raise, so a guard fails for the reason it really has."""
        from unittest import mock

        from capstone.errors import ConfigError

        def missing(_name):
            raise ModuleNotFoundError("No module named 'psycopg'")

        with mock.patch.object(features.importlib, "import_module", side_effect=missing):
            with self.assertRaises(ConfigError) as caught:
                features.raise_exception_type("postgresql+psycopg://u@h/db")
        self.assertIn("postgresql+psycopg2://", str(caught.exception))

    def test_the_guard_file_imports_no_driver_of_its_own(self):
        """The regression check, offline: the live tests may not go back to guessing.

        Prose may name a driver -- the docstrings explaining this very bug have to -- so only import
        statements are read. This is the half that can run without a database, which is the half that
        keeps the other half honest.
        """
        import re
        from pathlib import Path

        source = (Path(__file__).resolve().parent / "test_capstone_sql_guards.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            [],
            re.findall(r"^\s*(?:from|import)\s+psycopg\w*", source, flags=re.MULTILINE),
            msg="resolve it through `capstone.features.raise_exception_type` instead",
        )
        self.assertIn("raise_exception_type", source)


class ConnectRefusalTests(unittest.TestCase):
    """A database that will not open is a state of the machine, and states get sentences.

    ``capstone external --report`` used to end in a traceback out of the driver when the port was
    closed: the URI was well-formed, so the two guards that run before it (no scheme, unknown driver,
    module missing) had nothing to say, and ``module.connect`` raised whatever its author's
    ``OperationalError`` is. Two things are pinned here -- that the failure arrives as a
    ``NotReadyError`` with 67 so the caller can branch on it, and that the translation does not eat
    the diagnosis or leak the password the driver may have echoed.
    """

    URI = "postgresql+psycopg://learner:sup3rs3cr3t@127.0.0.1:5599/olist_phase0_test"
    REFUSED = (
        'connection to server at "127.0.0.1", port 5599 failed: Connection refused\n'
        "\tIs the server running on that host and accepting TCP/IP connections?\n"
        "supplied password: sup3rs3cr3t"
    )

    def refusing_module(self, text: str | None = None) -> mock._patch_decorator:
        """Patch the one seam ``connect`` uses to reach a driver, so no driver has to be installed."""

        class Refusing:
            OperationalError = type("OperationalError", (Exception,), {})

            def connect(self, dsn: str):
                raise self.OperationalError(text or ConnectRefusalTests.REFUSED)

        return mock.patch.object(features, "_driver_and_dsn", return_value=(Refusing(), "dsn"))

    def test_a_refused_connection_raises_the_project_error_not_the_driver_one(self) -> None:
        from capstone.errors import NotReadyError

        with self.refusing_module():
            with self.assertRaises(NotReadyError) as caught:
                features.connect(self.URI, purpose="report the external signal")
        self.assertEqual(int(caught.exception.exit_code), int(features_not_ready_code()))
        message = str(caught.exception)
        self.assertIn("would not open", message)
        self.assertIn(
            "report the external signal", message, msg="the purpose has to survive, or a reader cannot tell which command failed"
        )
        self.assertIn("Connection refused", message, msg="the driver's own words are the diagnosis")
        self.assertIn("Nothing was read or written", message)

    def test_the_password_never_rides_along_in_the_refusal(self) -> None:
        with self.refusing_module():
            with self.assertRaises(Exception) as caught:
                features.connect(self.URI, purpose="report the external signal")
        self.assertNotIn("sup3rs3cr3t", str(caught.exception))
        self.assertIn(
            "«redacted»", str(caught.exception), msg="the driver echoed it, so the scrub is what this test is about"
        )

    def test_a_driver_dump_is_cut_short_rather_than_pasted(self) -> None:
        long_text = "line of driver noise " * 60
        with self.refusing_module(long_text):
            with self.assertRaises(Exception) as caught:
                features.connect(self.URI, purpose="report the external signal")
        self.assertLess(len(str(caught.exception)), 700)
        self.assertTrue(str(caught.exception).endswith(".") or str(caught.exception).count(" …") == 1)

    def test_the_refusal_survives_a_uri_the_parser_hates(self) -> None:
        """The scrubber parses the URI to find the secret; a malformed one is the input it exists for."""
        with self.refusing_module("Network is unreachable"):
            with self.assertRaises(Exception) as caught:
                features.connect("postgresql+psycopg://[::1", purpose="report the external signal")
        self.assertIn("Network is unreachable", str(caught.exception))


def features_not_ready_code() -> int:
    """67, read from the table rather than typed: a renumbering has to move the test too."""
    from capstone.exit_codes import ExitCode

    return int(ExitCode.NOT_READY)


if __name__ == "__main__":
    unittest.main()
