"""The audit mirror: the CSV in, the table filled, and the frame's guard kept honest about it.

``capstone.audit.load_from_csv`` is what makes ``capstone.require_allowed`` usable on a fresh
database, so it is held to three claims, each of them about the *table* rather than about a log line:

1. loading the same CSV twice changes nothing -- checked by snapshotting the fake table before and
   after, never by trusting the report the loader prints;
2. an edit to the CSV reaches exactly the row it names, all other rows identical;
3. ``features.build`` asks exactly one question about the audit -- "is it empty" -- and never converts
   a real guard violation into a "load the audit first" message.

The fake database is a dictionary that applies the statements it is given. That is deliberate: a stub
that returns whatever a test asks for would also happily return whatever a broken loader writes, and
the value of these tests is that the effect is computed from the SQL.
"""

from __future__ import annotations

import csv
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))

from capstone import audit, features  # noqa: E402
from capstone.audit import (  # noqa: E402
    AUDIT_COLUMNS,
    audit_problems,
    load_audit,
    load_values,
    render_load,
)
from capstone.errors import AuditError, InputError  # noqa: E402
from capstone.pending import is_pending  # noqa: E402

CSV_PATH = STAGE / "docs" / "evidence" / "column_audit.csv"
#: the row these tests edit and delete -- `allowed` today, and read by the frame through
#: the order_items rollups, so changing its verdict has to reach the guard in the same run
VICTIM = ("order_items", "freight_value")
def guard_sql() -> str:
    """The frame's guard statement, spelled by ``capstone.features``.

    Read lazily on purpose: in this tree ``features`` does not own that constant yet -- it arrives
    with the real ``build`` -- and a module-level reference would turn "not ported yet" into an
    ``AttributeError`` for every test in the file.
    """
    return features.AUDIT_GUARD_SQL.format(frame=features.FRAME_VIEW, schema="olist")


class RaiseException(Exception):
    """What the driver raises for a PL/pgSQL ``RAISE EXCEPTION`` -- the error type under test.

    Named after the database side rather than the library side on purpose: psycopg 3 and psycopg2 both
    surface a ``RAISE`` as an exception of this shape, and the claim being tested is that ``build``
    lets it through instead of wrapping it in a friendlier error.
    """


class FakeAuditTable:
    """``capstone.column_audit`` as six columns and a primary key, with TRUNCATE semantics.

    ``allow_ddl`` exists for the tests that walk all of ``build``: the migrations arrive as
    file-sized statements the fake has no opinion about, and ignoring them is the only way to see
    what happens after them. Everywhere else an unknown statement is a failure, because it means the
    loader invented a query nobody specified.
    """

    def __init__(self, *, allow_ddl: bool = False) -> None:
        self.rows: dict[tuple[str, str], tuple] = {}
        self.statements: list[str] = []
        self.allow_ddl = allow_ddl

    def execute(self, sql: str) -> list[tuple]:
        # every statement is recorded, the TRUNCATE included: "the load is one reset and one insert"
        # is only worth asserting if the reset shows up where a reader can look for it
        self.statements.append(sql)
        if sql == audit.READ_STATEMENT:
            return sorted(((*key, *values) for key, values in self.rows.items()))
        if sql == audit.RESET_STATEMENT:
            self.rows.clear()
            return []
        if "count(*) FROM capstone.column_audit" in sql:
            return [(len(self.rows),)]
        if "capstone.require_allowed(array_agg" in sql:
            raise RaiseException(self._violation())
        if self.allow_ddl:
            return []
        raise AssertionError(f"the fake database was asked something it does not implement: {sql[:70]}")

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        self.statements.append(sql)
        assert sql == audit.LOAD_STATEMENT, sql
        for values in seq:
            self.rows[(values[0], values[1])] = tuple(values[2:])

    def _violation(self) -> str:
        # the guard's judgement, recomputed here so the fake cannot be talked into agreeing
        refused = sorted(
            f"{table}.{column}"
            for (table, column), values in self.rows.items()
            if values[3] != "allowed"
        )
        return (
            "completely forbidden columns are referenced by the frame: "
            + ", ".join(refused)
            + "; see capstone.v_audit_rejections"
        )


class FakeCursor:
    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self._rows: list[tuple] = []

    def execute(self, sql: str, params=None) -> None:
        self._rows = self.connection.table.execute(sql)

    def executemany(self, sql: str, seq) -> None:
        self.connection.table.executemany(sql, list(seq))

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def fetchone(self) -> tuple:
        return self._rows[0] if self._rows else (None,)

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeConnection:
    """Enough DBAPI for the loader: one cursor, one commit, a close that has to happen."""

    def __init__(self, table: FakeAuditTable) -> None:
        self.table = table
        self.commits = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


def example_config():
    from capstone.config import load_contract

    return load_contract(STAGE / "config" / "capstone.example.toml")


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = load_audit(CSV_PATH)
        self.assertEqual(len(self.rows), 53, msg="the shipped audit is 53 rows; the CSV changed")
        self.tmp = Path(tempfile.mkdtemp(prefix="capstone-load-"))
        self.csv = self.tmp / "column_audit.csv"
        self.csv.write_text(CSV_PATH.read_text(encoding="utf-8"), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the CSV, read and written through the csv module: the witnesses contain commas, and a
    # -- split(",") would move a verdict into the previous field and pass by accident
    def read_csv(self) -> tuple[list[str], list[list[str]]]:
        rows = list(csv.reader(io.StringIO(self.csv.read_text(encoding="utf-8"))))
        return rows[0], [row for row in rows[1:] if row]

    def write_csv(self, header: list[str], body: list[list[str]]) -> None:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(body)
        self.csv.write_text(out.getvalue(), encoding="utf-8")

    def find(self, body: list[list[str]]) -> int:
        return next(i for i, row in enumerate(body) if tuple(row[:2]) == VICTIM)

    def load(self, table: FakeAuditTable, *, csv_path: Path | None = None) -> tuple[dict, FakeConnection]:
        connection = FakeConnection(table)
        report = audit.load_from_csv(
            example_config(),
            uri="postgresql://fake/olist",
            connect=lambda _uri: connection,
            csv_path=csv_path or self.csv,
        )
        return report, connection

    def test_a_second_load_of_the_same_csv_changes_nothing_at_all(self):
        """The no-op claim, measured on the table rather than reported by the loader."""
        table = FakeAuditTable()
        first, _conn = self.load(table)
        self.assertEqual(first["rows"], 53)
        self.assertEqual(
            [first[key] for key in ("inserted", "unchanged", "rewritten", "removed")], [53, 0, 0, 0]
        )
        after_first = dict(table.rows)

        second, _conn = self.load(table)
        self.assertEqual(
            [second[key] for key in ("inserted", "unchanged", "rewritten", "removed")],
            [0, 53, 0, 0],
            msg=f"a re-run reported {second}",
        )
        self.assertEqual(dict(table.rows), after_first, msg="the table moved on a re-run")
        self.assertEqual(second["changed_keys"], [])
        self.assertEqual(second["verdicts"], first["verdicts"])

    def test_a_legitimate_csv_edit_reaches_exactly_the_row_it_names(self):
        """One verdict changed in the CSV: one row rewritten, the other 52 identical.

        The witness changes too, and every row carries all four fields, so a load that transferred
        only ``verdict`` and left the rest of the mirror behind fails here rather than passing quietly.
        """
        table = FakeAuditTable()
        self.load(table)
        before = dict(table.rows)

        header, body = self.read_csv()
        index = self.find(body)
        self.assertEqual(body[index][5], "allowed", msg="the fixture picked a row already forbidden")
        body[index][5] = "forbidden"
        body[index][4] = "re-audited after the report was written"
        self.write_csv(header, body)

        report, _conn = self.load(table)
        self.assertEqual(
            [report[key] for key in ("inserted", "unchanged", "rewritten", "removed")],
            [0, 52, 1, 0],
            msg=f"expected exactly one rewritten row, got {report}",
        )
        self.assertEqual(report["changed_keys"], ["order_items.freight_value"])
        after = dict(table.rows)
        self.assertEqual(after[VICTIM][3], "forbidden", msg="the verdict did not propagate")
        self.assertEqual(after[VICTIM][2], "re-audited after the report was written")
        self.assertEqual(
            after[VICTIM][0],
            before[VICTIM][0],
            msg="available_at was rewritten by a load that had no reason to touch it",
        )
        self.assertEqual(
            {key: values for key, values in after.items() if key != VICTIM},
            {key: values for key, values in before.items() if key != VICTIM},
        )

    def test_a_row_deleted_from_the_csv_leaves_the_table(self):
        """Why the load replaces rather than upserts, and what ``DO UPDATE`` would have cost.

        An upsert reports a clean run and keeps the row nobody audits any more -- and the guard reads
        a lingering ``allowed`` as permission. That is the one failure this table exists to prevent,
        so it is tested rather than trusted.
        """
        table = FakeAuditTable()
        self.load(table)
        header, body = self.read_csv()
        before = len(body)
        body = [row for row in body if tuple(row[:2]) != VICTIM]
        self.assertEqual(len(body), before - 1, msg="the row this test deletes is not in the CSV")
        self.write_csv(header, body)

        report, _conn = self.load(table)
        self.assertEqual(report["removed"], 1, msg=f"the stale row survived: {report}")
        self.assertNotIn(VICTIM, table.rows)

    def test_the_connection_path_and_the_psql_path_carry_the_same_rows(self):
        """Two frontends, one row set -- the test that keeps ``render_load`` from drifting."""
        bound = load_values(self.rows)
        rendered = render_load(self.rows)
        self.assertEqual(len(bound), 53)
        self.assertEqual(
            [tuple(values[:2]) for values in bound],
            [(row.table, row.column) for row in sorted(self.rows, key=lambda r: (r.table, r.column))],
        )
        for values in bound:
            literal = ", ".join(
                "NULL"
                if value is None
                else ("true" if value else "false")
                if isinstance(value, bool)
                else audit.sql_quote(str(value))
                for value in values
            )
            self.assertIn(
                f"({literal})", rendered, msg=f"a row the psql path does not carry: {values[:2]}"
            )

    def test_the_load_validates_before_it_writes(self):
        """A row that contradicts its own verdict must not reach the table the guard trusts."""
        header, body = self.read_csv()
        index = self.find(body)
        body[index][4] = ""  # `allowed` with no witness
        self.write_csv(header, body)
        self.assertTrue(audit_problems(load_audit(self.csv)), msg="the fixture stopped being a problem")

        table = FakeAuditTable()
        with self.assertRaises(AuditError):
            self.load(table)
        self.assertEqual(table.rows, {}, msg="a refused CSV still wrote rows")
        self.assertEqual(table.statements, [], msg="a refused CSV touched the database at all")

    def test_the_load_is_one_transaction_and_closes_the_connection(self):
        table = FakeAuditTable()
        _report, connection = self.load(table)
        self.assertEqual(connection.commits, 1, msg="one transaction, not one per statement")
        self.assertTrue(connection.closed)
        self.assertEqual(table.statements[0], audit.READ_STATEMENT)
        self.assertEqual(table.statements[1], audit.RESET_STATEMENT)
        self.assertEqual(table.statements.count(audit.LOAD_STATEMENT), 1)
        self.assertEqual(table.statements[-1], audit.READ_STATEMENT)

    def test_a_missing_audit_file_names_its_writer(self):
        table = FakeAuditTable()
        with self.assertRaises(InputError) as caught:
            self.load(table, csv_path=self.tmp / "nowhere.csv")
        self.assertIn("scripts/audit_columns.py", str(caught.exception))

    def test_the_cli_refusal_names_the_load_not_the_frame(self):
        """A refusal that points at the wrong layer is a second bug on top of the first.

        ``audit --load`` and ``features`` resolve their connection through one function, and that
        function used to answer with "no database to build the frame against" even when nobody was
        building a frame. The wording is a parameter now, so this asserts the parameter is passed.
        """
        import contextlib
        import os
        from capstone.cli import main

        saved = os.environ.pop("CAPSTONE_DATABASE_URI", None)
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = main(["audit", "--load", "--config", str(STAGE / "config" / "capstone.example.toml")])
        finally:
            if saved is not None:
                os.environ["CAPSTONE_DATABASE_URI"] = saved
        message = err.getvalue()
        self.assertEqual(code, 78, msg=message)
        self.assertIn("CAPSTONE_DATABASE_URI", message)
        self.assertIn("mirror the audit into capstone.column_audit", message)
        self.assertNotIn("build the frame", message)

    def test_the_step_is_not_a_placeholder_and_the_chain_sees_it(self):
        from capstone.chain import CHAIN, statuses

        self.assertFalse(is_pending(audit.load_from_csv))
        rows = {row["name"]: row for row in statuses()}
        self.assertTrue(rows["load-audit"]["ready"])
        self.assertEqual(rows["load-audit"]["reason"], "")
        names = [step.name for step in CHAIN]
        self.assertLess(names.index("load-audit"), names.index("features"))


@unittest.skipIf(
    is_pending(features.build),
    "these tests compare the loader with features.build, and build is a placeholder here: "
    "a guard that runs against a stub proves nothing",
)
class FramePreCheckTests(unittest.TestCase):
    """``features.build`` may ask one question about the audit table, and it is "is it empty"."""

    def build_with(self, table: FakeAuditTable):
        return features.build(
            example_config(),
            [],
            uri="postgresql://fake/olist",
            connect=lambda _uri: FakeConnection(table),
            clock=lambda: 0.0,
            repeat=1,
        )

    def test_an_empty_table_is_refused_before_the_guard_runs(self):
        table = FakeAuditTable(allow_ddl=True)
        with self.assertRaises(InputError) as caught:
            self.build_with(table)
        message = str(caught.exception)
        self.assertIn("capstone audit --load", message)
        self.assertIn("load_column_audit.py", message, msg="the no-Python fallback belongs there too")
        self.assertIn(audit.AUDIT_COUNT_STATEMENT, table.statements)
        # the DDL that *defines* require_allowed is applied, of course; what must not happen is the
        # guard query running against a table the loader knows is empty
        self.assertNotIn(guard_sql(), table.statements, msg="the guard ran anyway")

    def test_a_populated_table_that_rejects_a_column_is_not_relabelled(self):
        """The mirror is full and the frame still leaks: that is a finding, and it stays one."""
        table = FakeAuditTable(allow_ddl=True)
        for values in load_values(load_audit(CSV_PATH)):
            table.rows[(values[0], values[1])] = tuple(values[2:])
        table.rows[VICTIM] = ("at delivery", True, "audited", "forbidden")
        with self.assertRaises(RaiseException) as caught:
            self.build_with(table)
        self.assertIn("order_items.freight_value", str(caught.exception))
        self.assertIn("v_audit_rejections", str(caught.exception))
        self.assertNotIsInstance(caught.exception, InputError)

    def test_the_emptiness_check_runs_first_and_the_guard_once(self):
        table = FakeAuditTable(allow_ddl=True)
        table.rows[VICTIM] = ("at delivery", True, "audited", "allowed")
        try:
            self.build_with(table)
        except Exception:  # noqa: BLE001 - the frame is fake past this point; the ordering is the claim
            pass
        self.assertEqual(
            table.statements.count(guard_sql()), 1, msg="the guard ran a different number of times"
        )
        self.assertLess(
            table.statements.index(audit.AUDIT_COUNT_STATEMENT),
            table.statements.index(guard_sql()),
            msg="the pre-check ran after the guard, which is the ordering it exists to avoid",
        )

    def test_the_loader_and_the_pre_check_agree_on_the_table_name(self):
        """One constant, two users: a renamed table must not split into 'load X, count Y'."""
        self.assertIn(audit.COLUMN_AUDIT_TABLE, audit.AUDIT_COUNT_STATEMENT)
        self.assertIn(audit.COLUMN_AUDIT_TABLE, audit.READ_STATEMENT)
        self.assertIn(audit.COLUMN_AUDIT_TABLE, audit.RESET_STATEMENT)
        self.assertEqual(AUDIT_COLUMNS, audit.AUDIT_HEADER)
        self.assertEqual(len(AUDIT_COLUMNS), len(audit.QUOTED_AUDIT_COLUMNS.split(", ")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
