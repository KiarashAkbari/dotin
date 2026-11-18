"""Real PostgreSQL, a throwaway cluster, and the checks that only a server can answer.

Every other test in this directory reads text. That is what makes them runnable anywhere, and it is
also their ceiling: ``operator does not exist: oidvector = regtype[]`` sat inside a ``DO`` block in
``sql/004`` through a static suite, a checksum manifest, an eleven-mutation harness and a green
``capstone audit``, and only failed once psql pointed at a database. Nothing in the offline set can
tell a cast that exists from a cast that does not, so this module stops implying it can: it starts a
temp cluster, applies the shipped migrations with the shipped ``psql``, and asserts what the catalog
says afterwards.

Two ways to get a server, and one of them is not this sandbox's:

* ``pip install pgserver`` (the dev extra) -- a throwaway cluster is started under a temp directory,
  which is how the log in ``docs/evidence/postgres-smoke-run.txt`` was produced.
* ``CAPSTONE_SMOKE_DATABASE_URI=... CAPSTONE_SMOKE_ALLOW_DESTROY=1`` -- any PostgreSQL the reader
  trusts for schema-dropping work, e.g. a ``docker run --rm -p 55432:5432 postgres:16`` started for
  the purpose. Both variables are required: this module drops and recreates the ``capstone`` and
  ``olist`` schemas, so pointing it at a database that holds something you want back is the one
  mistake it cannot undo, and by default it refuses to be that mistake.

With neither, every test here *skips*, and the skip says why: "not verified" and "verified somewhere
else" are different claims, and a suite that blurs them is how the bug below got out.

What is proven here is structural -- the SQL parses, the objects exist, the guard fires, the drop
refuses, the loader is idempotent. What is not: this cluster holds zero orders, so nothing about row
counts, metrics, the 500 ms ceiling, or the as-of agreement on real data is decided by this file.
Those stay with the project database, and no line below claims otherwise.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers

STAGE = helpers.STAGE_ROOT
SQL_DIR = STAGE / "sql"
#: the file ``003``'s views read through; this module recreates that schema from it when it can
OLIST_DDL_NAME = "001_create_olist_schema.sql"

try:
    import pgserver  # type: ignore[import-not-found]

    HAVE_PGSERVER = True
    IMPORT_ERROR = ""
    # the wheel bundles a full PostgreSQL, psql included; that binary is what applies the migrations
    PSQL_BINARY = Path(str(pgserver.__file__)).parent / "pginstall" / "bin" / "psql"
except Exception as exc:  # noqa: BLE001 - any failure here means "no cluster on this machine"
    pgserver = None  # type: ignore[assignment]
    HAVE_PGSERVER = False
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    PSQL_BINARY = None

#: an outside server, if the reader is supplying one -- see the module docstring
EXTERNAL_URI = os.environ.get("CAPSTONE_SMOKE_DATABASE_URI", "").strip()
DESTROY_ALLOWED = os.environ.get("CAPSTONE_SMOKE_ALLOW_DESTROY", "") == "1"
#: pgserver ships a psql; a reader with their own server needs one on PATH
PSQL_ON_PATH = shutil.which("psql")
if EXTERNAL_URI and not DESTROY_ALLOWED:
    CAN_RUN, SKIP_REASON = False, (
        "CAPSTONE_SMOKE_DATABASE_URI is set without CAPSTONE_SMOKE_ALLOW_DESTROY=1, and this module "
        "drops and recreates the capstone and olist schemas -- say so on purpose, not by accident"
    )
elif EXTERNAL_URI and PSQL_ON_PATH is None:
    CAN_RUN, SKIP_REASON = False, "an outside URI was given but no psql is on PATH"
elif not EXTERNAL_URI and not HAVE_PGSERVER:
    CAN_RUN, SKIP_REASON = False, f"needs the pgserver dev extra to start a temp cluster -- {IMPORT_ERROR}"
elif not EXTERNAL_URI and not PSQL_ON_PATH and (PSQL_BINARY is None or not PSQL_BINARY.is_file()):
    CAN_RUN, SKIP_REASON = False, f"no psql to apply the migrations with (pgserver keeps it at {PSQL_BINARY})"
else:
    CAN_RUN, SKIP_REASON = True, ""

#: the four as-of functions ``004`` owns; its stale-drop block is limited to exactly these names
AS_OF_FUNCTIONS = (
    "customer_order_volume_before",
    "customer_days_since_prior_order",
    "seller_order_volume_before",
    "seller_mean_price_before",
)
STALE_SIGNATURE = "text, timestamp with time zone"
#: what the block prints when it converges a database, and what the refusal says when it cannot
DROP_NOTE = "dropped the stale overload capstone."
REFUSAL = "is depended on by"
NAIVE_SIGNATURE = "text, timestamp without time zone"
#: the frame's dependency closure, walked the way ``LiveSqlContractTests`` walks it on real data
FRAME_SOURCES_SQL = """
WITH RECURSIVE view_tree(oid) AS (
    SELECT 'capstone.v_feature_frame'::regclass::oid
    UNION
    SELECT referenced.oid
    FROM view_tree AS tree
    JOIN pg_rewrite AS rule ON rule.ev_class = tree.oid
    JOIN pg_depend AS dep
      ON dep.classid = 'pg_rewrite'::regclass AND dep.objid = rule.oid
     AND dep.refclassid = 'pg_class'::regclass
    JOIN pg_class AS referenced ON referenced.oid = dep.refobjid
    WHERE referenced.relkind IN ('r', 'v', 'm')
)
SELECT source.relname
FROM view_tree AS tree
JOIN pg_class AS source ON source.oid = tree.oid
JOIN pg_namespace AS namespace ON namespace.oid = source.relnamespace
WHERE namespace.nspname = 'olist'
ORDER BY 1
"""
SERVER = None
RUNNER = None
WORKDIR: Path | None = None
#: what the driver-facing tests connect with, so they follow the server the module was given
SMOKE_URI = ""


class PsqlRunner:
    """The psql that ships with the temp cluster (or the reader's own), driven as the README does.

    ``ON_ERROR_STOP=1`` and a checked exit code are the point: the migrations are applied exactly as a
    stranger applies them, so a statement the server rejects fails this module instead of printing a
    warning nobody reads back.
    """

    def __init__(self, uri: str) -> None:
        self.uri = uri
        self.binary = Path(PSQL_ON_PATH or PSQL_BINARY)
        if not self.binary.is_file():  # pragma: no cover - depends on the wheel's layout
            raise unittest.SkipTest(f"no psql to run the migrations with: {self.binary}")

    def run(self, *, sql: str = "", file: Path | None = None) -> tuple[int, str, str]:
        command = [str(self.binary), "-X", "-q", "-t", "-v", "ON_ERROR_STOP=1", self.uri]
        command += ["-f", str(file)] if file is not None else ["-c", sql]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    def apply(self, file: Path) -> None:
        code, out, err = self.run(file=file)
        if code:
            raise AssertionError(f"{file.name} failed on a real server (exit {code}):\n{err or out}")

    def run_sql(self, sql: str) -> str:
        code, out, err = self.run(sql=sql)
        if code:
            raise AssertionError(f"statement failed on the smoke cluster:\n{err or out}")
        return (err or out).strip()

    def values(self, sql: str) -> list[str]:
        return [line.strip() for line in self.run_sql(sql).splitlines() if line.strip()]


def setUpModule() -> None:  # noqa: N802 - unittest's hook
    global SERVER, RUNNER, WORKDIR, SMOKE_URI
    if not CAN_RUN:
        return
    if EXTERNAL_URI:
        SMOKE_URI = EXTERNAL_URI
        RUNNER = PsqlRunner(SMOKE_URI)
        rebuild()
        return
    WORKDIR = Path(tempfile.mkdtemp(prefix="capstone-smoke-"))
    # "stop", not "delete": the cluster lives under WORKDIR, which tearDownModule removes wholesale,
    # so the server is only asked to shut up shop
    SERVER = pgserver.get_server(WORKDIR / "pgdata", cleanup_mode="stop")
    SMOKE_URI = str(SERVER.get_uri())
    RUNNER = PsqlRunner(SMOKE_URI)
    rebuild()


def tearDownModule() -> None:  # noqa: N802 - unittest's hook
    global SERVER, RUNNER, WORKDIR
    if SERVER is not None:
        SERVER.cleanup()
    if WORKDIR is not None:
        shutil.rmtree(WORKDIR, ignore_errors=True)
    SERVER, RUNNER, WORKDIR, SMOKE_URI = None, None, None, ""


def olist_ddl() -> Path | None:
    """Where the stage-2 DDL lives, in either tree: ``stage-2`` here, ``stage-2-<name>`` in the port.

    A glob rather than one hard-coded parent, because the same module has to run in both checkouts and
    a wrong path here would mean the migrations silently stop being tested.
    """
    candidates = [
        STAGE.parent / "stage-2" / "sql" / OLIST_DDL_NAME,
        *sorted(STAGE.parent.glob(f"stage-2*/sql/{OLIST_DDL_NAME}")),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def rebuild() -> None:
    """Bring the database to the state a project is in just before ``004`` runs."""
    ddl = olist_ddl()
    if ddl is not None:
        RUNNER.run_sql("DROP SCHEMA IF EXISTS capstone CASCADE; DROP SCHEMA IF EXISTS olist CASCADE;")
        RUNNER.apply(ddl)
    else:
        # no DDL to restore from, so ``olist`` is left exactly as it was found: this module may rebuild
        # the schema it owns, never somebody else's data
        RUNNER.run_sql("DROP SCHEMA IF EXISTS capstone CASCADE;")
        if RUNNER.values("SELECT to_regclass('olist.orders') IS NOT NULL") != ["t"]:
            raise unittest.SkipTest(
                f"no {OLIST_DDL_NAME} next to {STAGE.name} and no olist.orders in the database either, "
                "so 003 has nothing to build the frame from"
            )
    for name in ("001_capstone_schema.sql", "002_time_guard.sql", "003_feature_views.sql"):
        RUNNER.apply(SQL_DIR / name)


def must_apply(sql: str) -> None:
    """Run a statement the way ``psql -v ON_ERROR_STOP=1`` would: a setup step that fails is a
    failed test, not a missing row that makes the next assertion pass by accident."""
    code, out, err = RUNNER.run(sql=sql)
    if code != 0:
        raise AssertionError(f"the setup SQL itself failed ({sql[:60]}...): {err.strip() or out.strip()}")


def apply_004() -> tuple[int, str]:
    code, out, err = RUNNER.run(file=SQL_DIR / "004_as_of_functions.sql")
    return code, (err or out).strip()


def signatures(name: str) -> list[str]:
    """The argument *types* of every overload of ``capstone.<name>``, in apply order.

    Types, not ``pg_get_function_identity_arguments``: that one carries the parameter names, so a
    decoy written with ``p_key text`` would not match ``p_seller_id text`` and the test would compare
    spellings instead of signatures. The catalog's own vector is the thing ``004`` queries too, which
    keeps this assertion and the migration looking at the same column.
    """
    return RUNNER.values(
        "SELECT (SELECT string_agg(t::regtype::text, ', ' ORDER BY i)"
        " FROM unnest(p.proargtypes::oid[]) WITH ORDINALITY AS arg(t, i))"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        f" WHERE n.nspname = 'capstone' AND p.proname = '{name}' ORDER BY 1"
    )


def decoy(name: str, body: str) -> Path:
    """Write a migration that is *not* in ``sql/``, so no tracked-file guard can see it.

    The decoys create the stale shape by hand; leaving them in ``sql/`` would have them picked up by
    the checksum manifest and the "exactly the five planned files" test, which is the kind of noise a
    smoke test should not generate.
    """
    path = WORKDIR / f"{name}.sql"
    path.write_text(body, encoding="utf-8")
    return path


def zoned(name: str, returns: str, value: str) -> None:
    """Create a stale overload in the shape the reader's database had: same name, zoned timestamp."""
    must_apply(
        f"CREATE OR REPLACE FUNCTION capstone.{name} (p_key text, p_at timestamptz)"
        f" RETURNS {returns} AS $$ SELECT {value} $$ LANGUAGE SQL STABLE"
    )


@unittest.skipUnless(CAN_RUN, SKIP_REASON or "no server available")
class ApplyTheRealFilesTests(unittest.TestCase):
    """The migrations have to run on a server, not merely read as SQL.

    Order is re-checked rather than trusted from ``sql_migrations()``: ``003`` reads ``olist``, ``002``'s
    guard reads the table ``001`` creates, and ``004`` now removes what an earlier apply left behind.
    """

    def test_every_file_applies_cleanly_and_the_four_as_of_functions_are_naive(self):
        code, output = apply_004()
        self.assertEqual(code, 0, msg=f"004 must apply: {output}")
        RUNNER.apply(SQL_DIR / "005_external_signal.sql")
        for name in AS_OF_FUNCTIONS:
            with self.subTest(function=name):
                self.assertEqual(signatures(name), [NAIVE_SIGNATURE])

    def test_reapplying_the_directory_changes_nothing(self):
        """The test that errored on the student's database, against a real server.

        The count is the assertion, not the exit code: a second apply that quietly added a second
        overload would still exit 0, and "idempotent" only means something if the catalog is the same
        afterwards.
        """
        code, output = apply_004()
        self.assertEqual(code, 0, msg=output)
        before = {name: signatures(name) for name in AS_OF_FUNCTIONS}
        RUNNER.apply(SQL_DIR / "005_external_signal.sql")
        code, output = apply_004()
        self.assertEqual(code, 0, msg=output)
        RUNNER.apply(SQL_DIR / "005_external_signal.sql")
        self.assertEqual({name: signatures(name) for name in AS_OF_FUNCTIONS}, before)

    def test_the_frame_exposes_ten_features_over_six_tables_with_no_data_at_all(self):
        """5.1's structural half, which a cluster of zero rows can still answer.

        A view over no rows still has columns and dependencies; what this cannot decide is whether the
        frame is *populated*, and how long that takes -- that stays with the project database.
        """
        code, output = apply_004()
        self.assertEqual(code, 0, msg=output)
        technical = {"order_id", "customer_id", "order_approved_at", "order_status"}
        columns = set(RUNNER.values(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'capstone' AND table_name = 'v_feature_frame'"
        ))
        self.assertTrue(columns, msg="the frame has no columns at all")
        self.assertGreaterEqual(
            len(columns - technical),
            10,
            msg=f"only {sorted(columns - technical)} look like features",
        )
        sources = RUNNER.values(FRAME_SOURCES_SQL)
        self.assertGreaterEqual(len(sources), 6, msg=f"the frame resolves through {sources}")


@unittest.skipUnless(CAN_RUN, SKIP_REASON or "no server available")
class StaleOverloadDropTests(unittest.TestCase):
    """The conditional drop in isolation, on a schema that holds nothing else.

    Every test here starts from an empty ``capstone`` namespace: the point of the block is which
    objects it touches, and that question is meaningless if the previous test's decoys are lying
    around. ``004`` is applied on top of a bare schema on purpose — the four ``CREATE OR REPLACE``
    statements and the drop block need no tables, so a failing test can only be about the block.
    """

    def setUp(self) -> None:
        # a full rebuild rather than `DROP SCHEMA capstone CASCADE`: these tests mutate the namespace
        # the *other* classes in this module assert about, and a migration's blast radius is exactly
        # the thing we should not be careless about here either
        rebuild()

    def test_a_stale_overload_is_dropped_when_nothing_depends_on_it(self) -> None:
        zoned("customer_order_volume_before", "float8", "0.0")
        zoned("seller_mean_price_before", "float8", "1.0")
        self.assertEqual(
            [STALE_SIGNATURE, STALE_SIGNATURE],
            sorted(signatures("customer_order_volume_before") + signatures("seller_mean_price_before")),
            msg="the setup did not produce the 8-function database the user described",
        )

        code, output = apply_004()
        self.assertEqual(code, 0, msg=f"a 004 that should drop and replace failed:\n{output}")
        self.assertIn(DROP_NOTE, output, msg="the migration said nothing about what it dropped")
        for name in ("customer_order_volume_before", "seller_mean_price_before"):
            self.assertEqual(
                [NAIVE_SIGNATURE],
                signatures(name),
                msg=f"{name} is still ambiguous, and the frame could keep resolving through the stale one",
            )

        code, output = apply_004()
        self.assertEqual(code, 0, msg=f"re-applying after the convergence failed:\n{output}")
        self.assertNotIn(DROP_NOTE, output, msg="the drop fired a second time on an already-converged database")
        self.assertEqual(
            len(
                RUNNER.values(
                    "SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
                    " WHERE n.nspname = 'capstone' AND p.proname IN ({})".format(
                        ", ".join(f"'{name}'" for name in AS_OF_FUNCTIONS)
                    )
                )
            ),
            4,
            msg="the four functions became something other than four",
        )

    def test_the_drop_refuses_while_a_view_resolves_through_the_stale_one(self) -> None:
        zoned("customer_order_volume_before", "float8", "0.0")
        must_apply(
            "CREATE VIEW capstone.v_using_the_stale_one AS"
            " SELECT capstone.customer_order_volume_before('x', TIMESTAMPTZ '2019-01-01') AS n"
        )

        code, output = apply_004()
        self.assertNotEqual(code, 0, msg="the drop went ahead beside a dependent view")
        self.assertIn(REFUSAL, output)
        # the abort precedes the CREATEs, so the file leaves nothing half-applied: no naive twin is
        # added beside a stale overload the frame still resolves through, which would have made the
        # ambiguity worse rather than merely unfixed
        self.assertEqual(
            [STALE_SIGNATURE],
            signatures("customer_order_volume_before"),
            msg="the refusal did not leave the database exactly as it found it",
        )

        # the recovery the RAISE documents: the dependent object is the project's call, so the
        # migration stops and the operator drops the stale overload once the view is gone
        must_apply("DROP VIEW capstone.v_using_the_stale_one")
        code, output = apply_004()
        self.assertEqual(code, 0, msg=f"the documented recovery did not work:\n{output}")
        self.assertEqual([NAIVE_SIGNATURE], signatures("customer_order_volume_before"))

    def test_no_function_outside_the_four_names_is_ever_dropped(self) -> None:
        """The sweep is narrowed to the owned names, so a zoned neighbour is untouched."""
        zoned("some_other_zoned_thing", "int", "7")
        for name in ("customer_order_volume_before", "seller_mean_price_before"):
            zoned(name, "float8", "0.0")

        code, output = apply_004()
        self.assertEqual(code, 0, msg=f"004 failed on a database with a zoned neighbour:\n{output}")
        self.assertEqual(
            [STALE_SIGNATURE],
            signatures("some_other_zoned_thing"),
            msg="the block dropped a function it does not own",
        )
        self.assertEqual([NAIVE_SIGNATURE], signatures("customer_order_volume_before"))

@unittest.skipUnless(CAN_RUN, SKIP_REASON or "no server available")
class AuditLoaderTests(unittest.TestCase):
    """``audit --load`` against a table that exists, which is where TRUNCATE is a real claim.

    The offline tests already pin the SQL text; what they cannot show is that a real database accepts
    it, that the second run reports a no-op, and -- the part ``ON CONFLICT DO UPDATE`` never gave --
    that a row *removed* from the CSV disappears from the table.
    """

    def setUp(self) -> None:
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest(
                "the loader's own connection path is psycopg 3, and this interpreter has no driver"
            )
        super().setUp()
        # unittest runs these two in alphabetical order and the first leaves 52 rows behind, so the
        # table is emptied here instead of trusting the order: the loader's whole claim is about state
        RUNNER.run_sql("TRUNCATE TABLE capstone.column_audit")
        # the module's scratch dir belongs to the temp-cluster mode, and these tests must also run
        # against a server the reader supplied, so the trimmed CSV gets a directory of its own
        self.tmp = Path(tempfile.mkdtemp(prefix="capstone-smoke-audit-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self, csv_path: Path | None = None) -> dict:
        from capstone.audit import load_from_csv
        # `load_contract`, not `load_config`: the example file carries the student decisions as
        # undecided on purpose, and this test is not the place to invent values for them
        from capstone.config import load_contract

        config = load_contract(STAGE / "config" / "capstone.example.toml")
        return load_from_csv(
            config,
            uri=SMOKE_URI,
            connect=lambda uri: __import__("psycopg").connect(str(uri), autocommit=True),
            csv_path=csv_path,
        )

    def count(self) -> int:
        rows = RUNNER.values("SELECT count(*) FROM capstone.column_audit")
        return int(rows[0])

    def test_the_load_is_idempotent_and_reports_the_no_op_it_claims(self):
        first = self.load()
        self.assertEqual(self.count(), int(first["rows"]), msg="the table does not match the CSV")
        self.assertEqual(int(first["inserted"]), first["rows"])
        again = self.load()
        self.assertEqual(int(again["unchanged"]), again["rows"], msg=f"{again}")
        self.assertEqual(int(again["inserted"]), 0)
        self.assertEqual(int(again["removed"]), 0)
        self.assertEqual(self.count(), first["rows"])

    def test_a_row_deleted_from_the_csv_disappears_from_the_table(self):
        """The reason the load replaces instead of merging, measured on a live table."""
        source = STAGE / "docs" / "evidence" / "column_audit.csv"
        lines = source.read_text(encoding="utf-8").splitlines()
        header, *rows = lines
        self.assertGreater(len(rows), 1, msg="need at least two rows to delete one")
        trimmed = self.tmp / "column_audit_minus_one.csv"
        trimmed.write_text("\n".join([header, *rows[:-1]]) + "\n", encoding="utf-8")
        self.load()
        before = self.count()
        result = self.load(csv_path=trimmed)
        self.assertEqual(self.count(), before - 1, msg=f"{result}")
        self.assertEqual(int(result["removed"]), 1)


@unittest.skipUnless(CAN_RUN, SKIP_REASON or "no server available")
class DriverAgreementTests(unittest.TestCase):
    """The resolver, the guard, and a real connection in the same test.

    Round 105 changed which class a live assertion expects; that was argued from a traceback, and the
    argument only becomes evidence when some driver actually raises. Both drivers are asked if both are
    installed, because the bug was exactly "the interpreter and the URI disagree".
    """

    def raise_error(self, uri: str) -> tuple[type, type]:
        from capstone.features import connect, raise_exception_type

        expected = raise_exception_type(uri)
        connection = connect(uri, autocommit=True, purpose="run the smoke tests")
        try:
            with self.assertRaises(expected) as caught:
                connection.cursor().execute("SELECT capstone.require_allowed(ARRAY['x.y'])")
            return expected, type(caught.exception)
        finally:
            connection.close()

    def test_the_class_the_resolver_names_is_the_class_the_connection_raises(self):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            self.skipTest("psycopg 3 is not installed, so there is no real driver to compare with")
        expected, raised = self.raise_error(SMOKE_URI)
        self.assertIs(
            raised,
            expected,
            msg=f"the connection raised {raised.__module__}.{raised.__qualname__},"
            f" the resolver promised {expected.__module__}.{expected.__qualname__}",
        )

    def test_two_uris_on_one_interpreter_name_two_different_classes(self):
        """The exact shape of the reported failure, asserted rather than reasoned about."""
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            self.skipTest("psycopg2 is not installed; the two-driver comparison cannot be made")
        from capstone.features import raise_exception_type

        new = raise_exception_type(SMOKE_URI)
        old = raise_exception_type(SMOKE_URI.replace("postgresql://", "postgresql+psycopg2://", 1))
        self.assertIsNot(new, old)
        self.assertEqual(old.__module__.split(".")[0], "psycopg2")
        expected, raised = self.raise_error(
            SMOKE_URI.replace("postgresql://", "postgresql+psycopg2://", 1)
        )
        self.assertIs(raised, expected)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
