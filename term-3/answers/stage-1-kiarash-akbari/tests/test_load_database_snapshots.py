"""When the snapshot views see the rows a load wrote, relative to its transaction.

Phase 0 found that a fresh `load-database --reset` left every olist materialized
view committed but empty (`pg_matviews.ispopulated = t`, zero rows), while a run
without `--reset` filled the same views. The cause was an ordering fact: the
refresh ran before the views existed, and 005-008 then created them on a second
connection, from a schema whose rows were still invisible to it.

The load now runs reset, migrations, load, refresh and sink on one connection in
one transaction, so a view created after the load reads the load's own rows, and a
view that already existed is refreshed from them before the single commit. This
file pins that in two ways:

* a ledger test, where the fake records which rows each connection can see, so a
  view built or refreshed anywhere but on the load's own connection comes out empty;
* a real-PostgreSQL test, run against an empty disposable database, which is where
  the defect was first observed.

Like the rest of stage 1 this is unittest, not pytest: the suite runs through
`python -m unittest discover -s tests`.
"""

import contextlib
import csv
import dataclasses
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import sqlalchemy as sa
from config_helpers import make_config

from data_factory.config import DatabaseConfig
from data_factory.database import (
    OLIST_CSV_FILES,
    OLIST_MATERIALIZED_VIEWS,
    OLIST_SCHEMA,
    sql_migration_files,
)
from data_factory.errors import DataError

ANSWERS_ROOT = Path(__file__).resolve().parents[2]
# `__name__`, not a literal: the module is `test_load_database_snapshots` under
# `discover -s tests` and `tests.test_load_database_snapshots` when named directly, and
# a hard-coded target silently patches nothing in one of the two.
MODULE = __name__


def stage2_sql_dir():
    """Locate the stage-2 `sql` directory, which sits beside stage-1 here and beside
    the answers in `dotin`, under two different names.

    Exactly one match is required. Zero means the layout or this helper is broken, and
    several mean the choice would be alphabetical luck -- both are worth a failure with
    the searched root and the sorted matches in the message, and neither is worth a
    skip, because a skip is how a real check quietly stops checking.
    """
    return sorted(path for path in ANSWERS_ROOT.glob("stage-2*/sql") if path.is_dir())


def require_stage2_sql_dir(test):
    found = stage2_sql_dir()
    if len(found) != 1:
        test.fail(
            f"expected exactly one stage-2*/sql directory under {ANSWERS_ROOT}, found "
            f"{len(found)}: {found or 'none'}; the fixtures need the real 001-009 scripts"
        )
    return found[0]


SNAPSHOT_MIGRATION = "005_snap.sql"
SINK_MIGRATION = "009_factory_sink_schema.sql"
ID = "0" * 32
ZIP = "01122"


class Ledger:
    """The one property the fix is about: visibility, not execution order.

    `stage` holds rows written by the run, `visible` holds rows that are durable.
    The load's own connection always sees `stage`; any other connection sees only
    `visible`, and only once the load's transaction is closed. A commit makes the
    staged rows durable. A view built by a migration, or rebuilt by a refresh,
    reads whatever its connection can see at that moment.
    """

    def __init__(self):
        self.stage = 0
        self.visible = 0
        self.rows = {}
        self.log = []
        self.connection = None

    def load(self, cfg, connection=None):
        self.stage = 150
        self.log.append(("load_olist", id(connection)))
        return {}

    @property
    def visible_rows(self):
        """What any other connection can read right now, 0 if nothing is durable."""
        if self.connection is not None and self.connection.open:
            return 0
        return self.visible

    def seen_by(self, bind):
        return self.stage if bind is self.connection else self.visible_rows

    def apply(self, bind, sql_dir, include=None):
        path = Path("sql") / SNAPSHOT_MIGRATION
        if include is not None and not include(path):
            return []
        self.log.append(("create_view", id(bind)))
        if SNAPSHOT_MIGRATION in self.rows:
            return []
        self.rows[SNAPSHOT_MIGRATION] = True
        for view in OLIST_MATERIALIZED_VIEWS:
            self.rows[view] = self.seen_by(bind)
        return [SNAPSHOT_MIGRATION]

    def commit(self, connection):
        self.visible = self.stage
        connection.open = False
        self.log.append(("commit", id(connection)))

    def refresh(self, connection):
        """Refresh the views that exist, from what this connection sees right now."""
        self.log.append(("refresh", id(connection)))
        connection.execute("SELECT matviewname FROM pg_matviews")
        rows = self.seen_by(connection)
        for view in OLIST_MATERIALIZED_VIEWS:
            if view in self.rows:
                self.rows[view] = rows


class FakeLockConnection:
    """The lock connection of `cli._load_database`, with a commit that is logged.

    `open` follows SQLAlchemy: a write autobegins a transaction, commit and rollback end
    it, and closing while it is open discards everything it held.
    """

    def __init__(self, ledger):
        self.ledger = ledger
        self.ledger.connection = self
        self.open = False

    def in_transaction(self):
        return self.open

    def execute(self, statement, parameters=None):
        # Only the statements the load itself runs are modelled; the advisory-lock
        # bookkeeping in the `finally` is a read, so it leaves the transaction shut.
        if not str(statement).lstrip().upper().startswith("SELECT"):
            self.open = True

    def commit(self):
        self.ledger.commit(self)

    def rollback(self):
        self.ledger.log.append(("rollback", id(self)))
        self.ledger.stage = self.ledger.visible
        self.open = False

    def close(self):
        if self.open:
            self.rollback()


class FakeEngine:
    def __init__(self, ledger):
        self.ledger = ledger

    def connect(self):
        return FakeLockConnection(self.ledger)

    def dispose(self):
        return None


class FakeTest:
    """A test-case stand-in: `fail` raises and keeps the message, as TestCase does."""

    def fail(self, message=""):
        raise AssertionError(message)


class SnapshotRefreshOrderTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        root = Path(self._folder.name)

        sql_dir = root / "sql"
        sql_dir.mkdir()
        for name in ("001_base.sql", SNAPSHOT_MIGRATION, SINK_MIGRATION):
            (sql_dir / name).write_text("-- test migration\n", encoding="utf-8")

        self.cfg = dataclasses.replace(
            make_config(root),
            database=DatabaseConfig(
                url="postgresql+psycopg://user@localhost:5432/db",
                password_env="ABSENT_FOR_TEST",
                olist_csv_dir=root / "csv",
                stage1_curated_dir=root / "curated",
                batch_size=100,
                sql_migrations_dir=sql_dir,
            ),
        )
        self.ledger = Ledger()
        patches = [
            mock.patch(
                "data_factory.cli.sa.create_engine", side_effect=lambda url: FakeEngine(self.ledger)
            ),
            mock.patch("data_factory.cli.build_database_url", side_effect=lambda cfg: "url"),
            mock.patch("data_factory.cli.load_olist", side_effect=self.ledger.load),
            mock.patch("data_factory.cli.refresh_olist_snapshots", side_effect=self.ledger.refresh),
            mock.patch("data_factory.cli.apply_sql_migrations", side_effect=self.ledger.apply),
            mock.patch("data_factory.cli.reset_olist_schema", side_effect=lambda connection: None),
            mock.patch("data_factory.cli.reset_factory_schema", side_effect=lambda connection: None),
            mock.patch(
                "data_factory.cli.load_stage1_sink", side_effect=lambda cfg, connection=None: {}
            ),
        ]
        stack = contextlib.ExitStack()
        for patcher in patches:
            stack.enter_context(patcher)
        self.addCleanup(stack.close)

    def _events(self):
        return [event for event, _ in self.ledger.log]

    def test_snapshots_created_after_the_load_see_the_loaded_rows(self):
        from data_factory.cli import _load_database

        _load_database(self.cfg, reset=True, with_sink=False)
        views = dict.fromkeys(OLIST_MATERIALIZED_VIEWS, self.ledger.visible_rows)
        self.assertEqual(self.ledger.visible_rows, 150, "the load itself is durable")
        self.assertEqual(
            {view: self.ledger.rows[view] for view in OLIST_MATERIALIZED_VIEWS},
            views,
            "a view committed empty is a wrong answer, not an early one",
        )

    def test_one_commit_publishes_the_load_and_its_views_together(self):
        from data_factory.cli import _load_database

        _load_database(self.cfg, reset=True, with_sink=False)
        log = self._events()
        self.assertEqual(log.count("load_olist"), 1)
        self.assertEqual(
            log,
            ["load_olist", "refresh", "create_view", "commit"],
            "refresh what exists, create the rest, then publish everything at once",
        )
        self.assertFalse(self.ledger.connection.open, "the run must not close an open transaction")

    def test_every_step_runs_on_the_connection_that_loaded_the_data(self):
        from data_factory.cli import _load_database

        _load_database(self.cfg, reset=True, with_sink=False)
        loaded = self.ledger.log[self._events().index("load_olist")][1]
        self.assertIn("refresh", self._events())
        self.assertIn("create_view", self._events())
        for event, ident in self.ledger.log:
            self.assertEqual(ident, loaded, f"{event} needs the load's transaction")

    def test_a_second_run_refreshes_the_views_that_already_exist(self):
        from data_factory.cli import _load_database

        _load_database(self.cfg, with_sink=False)
        self.ledger.log.clear()
        self.ledger.load = lambda cfg, connection=None: (
            setattr(self.ledger, "stage", 275) or self.ledger.log.append(("load_olist", id(connection))) or {}
        )
        with mock.patch("data_factory.cli.load_olist", side_effect=self.ledger.load):
            _load_database(self.cfg, with_sink=False)
        self.assertEqual(self.ledger.visible_rows, 275)
        self.assertEqual(
            {view: self.ledger.rows[view] for view in OLIST_MATERIALIZED_VIEWS},
            dict.fromkeys(OLIST_MATERIALIZED_VIEWS, 275),
            "an existing view must not keep the previous load's rows",
        )

    def test_a_run_without_migrations_refreshes_before_its_commit(self):
        from data_factory.cli import _load_database

        _load_database(self.cfg, apply_migrations=False)
        self.assertEqual(self._events(), ["load_olist", "refresh", "commit"])

    def test_the_stage2_sql_directory_requirement_fails_rather_than_skips(self):
        # No database needed: a broken layout is a failure with the counts in it,
        # in both directions, because "0 matches" and "2 matches" mean different things.
        with (
            mock.patch(f"{MODULE}.stage2_sql_dir", list),
            self.assertRaises(AssertionError) as zero,
        ):
            require_stage2_sql_dir(FakeTest())
        self.assertIn("found 0:", str(zero.exception))
        matches = [ANSWERS_ROOT / "stage-2/sql", ANSWERS_ROOT / "stage-2-x/sql"]
        with (
            mock.patch(f"{MODULE}.stage2_sql_dir", lambda: matches),
            self.assertRaises(AssertionError) as two,
        ):
            require_stage2_sql_dir(FakeTest())
        self.assertIn("found 2:", str(two.exception))
        self.assertIn(str(ANSWERS_ROOT), str(two.exception))
        # outside every patch: this checkout must satisfy its own requirement
        one = require_stage2_sql_dir(self)
        self.assertEqual([one], stage2_sql_dir())
        self.assertTrue(one.is_dir(), "the real stage-2 sql directory should be a directory")

    def test_a_failed_run_does_not_refresh_after_its_rollback(self):
        from data_factory.cli import _load_database

        def fail(engine, sql_dir, include=None):
            if include is not None and include(Path("sql") / SINK_MIGRATION):
                raise RuntimeError("sink migration explodes")
            return self.ledger.apply(engine, sql_dir, include=include)

        with (
            mock.patch("data_factory.cli.apply_sql_migrations", side_effect=fail),
            self.assertRaises(RuntimeError),
        ):
            _load_database(self.cfg, reset=True)
        log = self._events()
        self.assertIn("rollback", log)
        self.assertNotIn("commit", log, "a failed run never commits")
        self.assertEqual(self.ledger.visible_rows, 0, "a rolled back run leaves nothing durable")


@unittest.skipUnless(
    os.environ.get("TEST_DATABASE_URL"),
    "TEST_DATABASE_URL is not set: the empty-database check needs a real PostgreSQL",
)
class EmptyDatabaseSnapshotTests(unittest.TestCase):
    """The phase-0 experiment as a test: a rebuilt schema, `--reset`, real views.

    Point TEST_DATABASE_URL at a disposable database that nothing else uses,
    because the run under test drops and recreates the whole olist schema and its
    migration ledger. Then run

        createdb phase0_test
        TEST_DATABASE_URL=postgresql+psycopg://user@localhost:5432/phase0_test \
            python -m unittest discover -s tests

    Without the variable the class skips, so the suite stays runnable offline.
    """

    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        root = Path(self._folder.name)
        self.url = os.environ["TEST_DATABASE_URL"]
        self.engine = sa.create_engine(self.url)
        self.addCleanup(self.engine.dispose)
        with self.engine.connect() as probe:
            named = sa.make_url(self.url).database
            if probe.scalar(sa.text("SELECT current_database()")) != named:
                self.fail(f"TEST_DATABASE_URL must connect to {named!r}, not its default database")

        self.sql_dir = root / "sql"
        self.sql_dir.mkdir()
        source = require_stage2_sql_dir(self)
        for path in sql_migration_files(source):
            (self.sql_dir / path.name).write_text(
                path.read_text(encoding="utf-8"), encoding="utf-8"
            )

        # 001 is applied here for one reason: the fixtures need the same column
        # list the loader will reflect. The run under test drops this schema again.
        self.csv_dir = root / "csv"
        self.csv_dir.mkdir()
        with self.engine.connect() as connection:
            connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {OLIST_SCHEMA} CASCADE"))
            connection.execute(
                sa.text((self.sql_dir / "001_create_olist_schema.sql").read_text(encoding="utf-8"))
            )
            self._write_fixtures(connection)
            connection.commit()

        # A URL without a host but with a `host` query parameter is a Unix-socket
        # URL: build_database_url would drop the socket path, so keep it verbatim.
        parts = urlsplit(os.environ["TEST_DATABASE_URL"])
        url = (
            os.environ["TEST_DATABASE_URL"]
            if parts.hostname is None and parts.query
            else self.engine.url.render_as_string(hide_password=False)
        )
        self.cfg = dataclasses.replace(
            make_config(root),
            database=DatabaseConfig(
                url=url,
                password_env="ABSENT_FOR_TEST",
                olist_csv_dir=self.csv_dir,
                stage1_curated_dir=root / "curated",
                batch_size=100,
                sql_migrations_dir=self.sql_dir,
            ),
        )

    def _fixture_rows(self):
        return {
            "customers": [[ID, ID, ZIP, "city", "SP"]],
            "sellers": [[ID, ZIP, "city", "SP"]],
            "product_category_name_translation": [["books", "books"]],
            "products": [[ID, "books", 10, 10, 1, 100, 10, 10, 10]],
            "geolocation": [[ZIP, -23.5, -46.6, "city", "SP"]],
            "orders": [
                [
                    ID,
                    ID,
                    "delivered",
                    "2018-01-01 00:00:00",
                    "2018-01-02 00:00:00",
                    "2018-01-04 00:00:00",
                    "2018-01-11 00:00:00",
                    "2018-01-20 00:00:00",
                ]
            ],
            "order_items": [[ID, 1, ID, ID, "2018-01-05 00:00:00", 10.0, 2.0]],
            "payments": [[ID, 1, "credit_card", 1, 12.0]],
            "order_reviews": [[ID, ID, 1, None, None, "2018-01-12 00:00:00", None]],
        }

    def _write_fixtures(self, connection):
        """One CSV per olist table, header = exactly what the loader will insert."""
        metadata = sa.MetaData()
        for table, values in self._fixture_rows().items():
            target = sa.Table(table, metadata, schema=OLIST_SCHEMA, autoload_with=connection)
            columns = [
                column.name
                for column in target.columns
                if column.identity is None and column.computed is None
            ]
            self.assertEqual(len(columns), len(values[0]), f"{table} fixture arity")
            path = self.csv_dir / OLIST_CSV_FILES[table]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                for row in values:
                    writer.writerow(["" if value is None else value for value in row])

    def _view_state(self):
        with self.engine.connect() as connection:
            names = [
                row[0]
                for row in connection.execute(
                    sa.text(
                        "SELECT matviewname FROM pg_matviews"
                        " WHERE schemaname = :schema ORDER BY matviewname"
                    ),
                    {"schema": OLIST_SCHEMA},
                )
            ]
            state = {}
            for name in names:
                populated = connection.scalar(
                    sa.text(
                        "SELECT ispopulated FROM pg_matviews"
                        " WHERE schemaname = :schema AND matviewname = :name"
                    ),
                    {"schema": OLIST_SCHEMA, "name": name},
                )
                rows = connection.scalar(sa.text(f"SELECT count(*) FROM {OLIST_SCHEMA}.{name}"))
                state[name] = (bool(populated), rows)
            return state

    def test_a_reset_run_leaves_no_empty_view_behind(self):
        from data_factory.cli import _load_database

        _load_database(self.cfg, reset=True, with_sink=False)
        state = self._view_state()
        self.assertEqual(sorted(state), sorted(OLIST_MATERIALIZED_VIEWS))
        empty = sorted(name for name, (_pop, rows) in state.items() if rows == 0)
        self.assertEqual(empty, [], f"committed but unpopulated: {state}")

    def test_a_reset_view_agrees_with_the_tables_the_run_committed(self):
        from data_factory.cli import _load_database

        _load_database(self.cfg, reset=True, with_sink=False)
        view = "mv_q1_monthly_category_sales"
        with self.engine.connect() as connection:
            expected = connection.scalar(
                sa.text(
                    "SELECT count(DISTINCT o.order_id) FROM olist.orders AS o"
                    " JOIN olist.order_items AS i ON i.order_id = o.order_id"
                )
            )
            actual = connection.scalar(sa.text(f"SELECT count(*) FROM {OLIST_SCHEMA}.{view}"))
        self.assertEqual(actual, expected, f"{view} disagrees with its tables")

    def _run_with_lock_timeout(self, **kwargs):
        """Run a load where any lock wait fails after 5 s instead of hanging.

        PGOPTIONS reaches every libpq connection the run opens. A step that ran on
        a second connection while the load holds its table locks would wait on the
        load forever; here it fails with lock_timeout instead, and the thread guard
        turns a hang that lock_timeout cannot see into a failure as well.
        """
        from data_factory.cli import _load_database

        outcome = {}

        def target():
            try:
                _load_database(self.cfg, **kwargs)
                outcome["ok"] = True
            except Exception as exc:  # noqa: BLE001 - re-raised in the test thread below
                outcome["error"] = exc

        with mock.patch.dict(os.environ, {"PGOPTIONS": "-c lock_timeout=5000"}):
            probe = sa.create_engine(self.engine.url)
            try:
                with probe.connect() as connection:
                    self.assertEqual(connection.scalar(sa.text("SHOW lock_timeout")), "5s")
            finally:
                probe.dispose()
            worker = threading.Thread(target=target, daemon=True)
            worker.start()
            worker.join(timeout=120)
        self.assertFalse(worker.is_alive(), "load-database is still waiting after 120 s")
        if "error" in outcome:
            raise outcome["error"]
        self.assertTrue(outcome.get("ok"))

    def test_a_run_with_pending_migrations_never_waits_on_itself(self):
        # Fresh: every migration is pending and 005-008 read the tables the load
        # has just emptied and filled. Second: the views exist and are refreshed.
        with contextlib.redirect_stdout(io.StringIO()):
            self._run_with_lock_timeout(reset=True, with_sink=False)
            self._run_with_lock_timeout(with_sink=False)
        state = self._view_state()
        self.assertEqual(sorted(state), sorted(OLIST_MATERIALIZED_VIEWS))
        self.assertEqual([name for name, (_p, rows) in state.items() if rows == 0], [])
        with self.engine.connect() as connection:
            for table, rows in self._fixture_rows().items():
                count = connection.scalar(sa.text(f"SELECT count(*) FROM {OLIST_SCHEMA}.{table}"))
                self.assertEqual(count, len(rows), f"{table} after a reload")

    def test_a_failed_reset_run_keeps_the_previous_data(self):
        from data_factory.cli import _load_database

        with contextlib.redirect_stdout(io.StringIO()):
            _load_database(self.cfg, reset=True, with_sink=False)
        before = self._view_state()
        # Break the last file the load reads, so the reset, the migrations and
        # the earlier tables have all run when the error arrives.
        reviews = self.csv_dir / OLIST_CSV_FILES["order_reviews"]
        lines = reviews.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace(",1,", ",not-a-score,", 1)
        reviews.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(DataError):
            _load_database(self.cfg, reset=True, with_sink=False)
        with self.engine.connect() as connection:
            orders = connection.scalar(sa.text(f"SELECT count(*) FROM {OLIST_SCHEMA}.orders"))
            ledger = connection.scalar(sa.text("SELECT count(*) FROM public.data_factory_migrations"))
        self.assertEqual(orders, 1, "the rolled back reset must leave the old rows in place")
        self.assertGreater(ledger, 0, "the rolled back reset must leave the ledger in place")
        self.assertEqual(self._view_state(), before)

    def _ledger(self):
        with self.engine.connect() as connection:
            return dict(
                connection.execute(
                    sa.text("SELECT migration_name, checksum FROM public.data_factory_migrations")
                ).all()
            )

    def test_a_reset_forgets_only_the_olist_ledger_rows(self):
        from data_factory.cli import _load_database
        from data_factory.database import reset_factory_schema

        with contextlib.redirect_stdout(io.StringIO()):
            _load_database(self.cfg, reset=True, with_sink=False)
        others = {"009_factory_sink_schema.sql": "sink-history", "010_credit_schema.sql": "credit"}
        with self.engine.begin() as connection:
            for name, checksum in others.items():
                connection.execute(
                    sa.text(
                        "INSERT INTO public.data_factory_migrations (migration_name, checksum)"
                        " VALUES (:name, :checksum)"
                    ),
                    {"name": name, "checksum": checksum},
                )
        olist = {n: c for n, c in self._ledger().items() if n not in others}
        self.assertIn("001_create_olist_schema.sql", olist)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _load_database(self.cfg, reset=True, with_sink=False)
        ledger = self._ledger()
        self.assertEqual({n: ledger[n] for n in others}, others, "other stages keep their history")
        self.assertEqual({n: c for n, c in ledger.items() if n not in others}, olist)
        self.assertIn(f"ledger_forgotten={len(olist)} ", out.getvalue())

        with self.engine.connect() as connection:
            self.assertEqual(reset_factory_schema(connection), ["009_factory_sink_schema.sql"])
            remaining = connection.execute(
                sa.text("SELECT migration_name FROM public.data_factory_migrations")
            ).scalars()
            self.assertIn("010_credit_schema.sql", set(remaining))
            connection.rollback()


if __name__ == "__main__":
    unittest.main()
