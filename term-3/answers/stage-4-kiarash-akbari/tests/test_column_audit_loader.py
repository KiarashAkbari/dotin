"""The audit loader: the CSV in, SQL out, and nothing executed here.

``scripts/load_column_audit.py`` exists because ``capstone.column_audit`` is created by ``sql/001``
and read by the guards in ``sql/002``, while the rows themselves live in ``docs/evidence/
column_audit.csv``. With the table empty, ``capstone.require_allowed`` refuses every column --
fail-closed, which is the right instinct and the wrong error -- and a live run reports two errors
that look like feature bugs. These tests hold the emitter to the CSV: same rows, same keys, no
way to smuggle a row in that the audit would not defend.

Offline by construction: the script prints SQL and never connects, so nothing here needs a
database, and the load itself is verified on the student's PostgreSQL.
"""

from __future__ import annotations

import importlib.util
import io
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))

SCRIPT = STAGE / "scripts" / "load_column_audit.py"
CSV_PATH = STAGE / "docs" / "evidence" / "column_audit.csv"


def load_script():
    """Import the script by path: it is a tool, not a module of the package."""
    spec = importlib.util.spec_from_file_location("load_column_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HEADER = "table,column,available_at,at_approval,source,verdict"


def write_audit(path: Path, rows: list[str]) -> Path:
    """An audit file with the fixed header and the given body rows, for the refusal tests.

    Local rather than imported from ``tests/helpers.py`` for the same reason the other guard
    modules avoid it: this suite is also run as ``python -m unittest tests.test_x``, where
    ``helpers`` is not on the path.
    """
    path.write_text("\n".join([HEADER, *rows]) + "\n", encoding="utf-8")
    return path


class LoaderTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_script()
        cls.rows = cls.module.load_audit(CSV_PATH)
        cls.sql = cls.module.render(cls.rows)

    def test_one_statement_per_row_and_the_keys_match_the_csv(self):
        # `,;` on the tail: the last VALUES line closes the statement, so it ends in `);` and a
        # pattern that only accepts `),` quietly loses one row
        values = re.findall(r"^    \((.*)\)[,;]$", self.sql, re.MULTILINE)
        self.assertEqual(len(self.rows), len(values), msg="a CSV row vanished into the SQL")
        self.assertEqual(len(self.rows), 53, msg="the shipped audit is 53 rows; the CSV changed")
        keys = [f"'{row.table}', '{row.column}'" for row in sorted(self.rows, key=lambda r: (r.table, r.column))]
        for expected, actual in zip(keys, values):
            self.assertTrue(actual.startswith(expected), msg=f"{actual[:60]!r} vs {expected!r}")

    def test_the_target_and_its_identifiers_are_quoted(self):
        # `table` and `column` are reserved close enough that an unquoted column list is a
        # syntax error, so the statement has to quote them every time
        insert = next(line for line in self.sql.splitlines() if line.startswith("INSERT INTO"))
        self.assertEqual(
            insert,
            'INSERT INTO capstone.column_audit ("table", "column", "available_at", '
            '"at_approval", "source", "verdict")',
            msg=insert,
        )

    def test_a_quote_in_a_witness_survives_the_round_trip(self):
        apostrophes = [row for row in self.rows if "'" in row.source]
        self.assertTrue(apostrophes, msg="no test data, no test: the CSV has an apostrophe today")
        row = apostrophes[0]
        self.assertIn(f"'{row.source.replace(chr(39), chr(39) * 2)}'", self.sql)

    def test_the_load_replaces_and_is_one_transaction(self):
        """Truncate, insert, commit -- so a re-run cannot grow the table or keep a stale verdict."""
        statements = [self.sql.index(word) for word in ("BEGIN;", "TRUNCATE TABLE", "INSERT INTO", "COMMIT;")]
        self.assertEqual(statements, sorted(statements), msg=self.sql[:200])
        self.assertEqual(
            self.sql.count("TRUNCATE TABLE capstone.column_audit;"),
            1,
            msg="exactly one reset, before the inserts",
        )

    def test_the_null_at_approval_is_a_null_and_not_a_false(self):
        """An `unverified` row claims nothing about approval; the table must not invent a claim.

        ``false`` would read as "this column does not exist at approval", which is a verdict
        somebody could act on. The CSV's own bools round-trip through the same path.
        """
        unverified = [row for row in self.rows if row.at_approval is None]
        for row in unverified:
            self.assertIn(
                f"'{row.available_at}', NULL, '{row.source.replace(chr(39), chr(39) * 2)}'",
                self.sql,
                msg=row.key,
            )
        self.assertIn(", true, ", self.sql)
        self.assertIn(", false, ", self.sql)

    def test_the_header_comment_names_the_generator(self):
        self.assertTrue(self.sql.startswith("-- generated by scripts/load_column_audit.py"))


class LoaderRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_script()
        self.tmp = Path(tempfile.mkdtemp(prefix="capstone-audit-"))

    def run_main(self, csv_path: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.module.main(["--csv", str(csv_path)])
        return code, out.getvalue(), err.getvalue()

    def test_a_contradictory_audit_produces_no_sql(self):
        """An `allowed` row with no source is the mistake the guard exists to catch -- so the
        loader refuses to be the thing that puts it in the database."""
        bad = write_audit(
            self.tmp / "column_audit.csv",
            ["orders,order_approved_at,at approval,true,,allowed"],
        )
        code, out, err = self.run_main(bad)
        self.assertEqual(out, "", msg="refusing must not still print SQL")
        self.assertIn("refusing to emit SQL", err)
        self.assertNotEqual(code, 0)

    def test_an_empty_audit_produces_no_sql(self):
        empty = write_audit(self.tmp / "column_audit.csv", [])
        code, out, err = self.run_main(empty)
        self.assertEqual(out, "")
        self.assertIn("no rows", err)
        self.assertNotEqual(code, 0)

    def test_a_missing_csv_says_who_writes_it(self):
        code, out, err = self.run_main(self.tmp / "column_audit.csv")
        self.assertEqual(out, "")
        self.assertIn("ROADMAP phase 2 writes it", err)
        self.assertNotEqual(code, 0)

    def test_the_shipped_csv_is_accepted(self):
        code, out, err = self.run_main(CSV_PATH)
        self.assertEqual(code, 0, msg=err)
        self.assertIn("INSERT INTO capstone.column_audit", out)


if __name__ == "__main__":
    unittest.main()
