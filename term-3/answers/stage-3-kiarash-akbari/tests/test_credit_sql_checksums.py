"""The shipped migrations are immutable, and this file is the tripwire.

``apply_sql_migrations`` (stage 1) keys its ledger on the migration *file name* and stores
one checksum per name, so editing a migration in place does not repair anybody's database -
it makes every later load of that database fail with ``SQL migration checksum mismatch``
before a single statement runs. That is not hypothetical: ``010_credit_schema.sql`` was
edited after it had been applied (``ac34967``, the FILTER clause), and the first reviewer run
of ``python -m credit.load --migrations-only`` stopped exactly there.

So the two rules this stage lives by are pinned here:

* a correction arrives as a **new** file (``012_…``, ``013_…``), never as a new revision of
  a number that some database has already applied; and
* a database whose ledger row disagrees is repaired forward with ``python -m credit.load
  --reset``, which drops only ``credit`` and deletes only the rows these files name.

A failing assertion below means a shipped migration's bytes changed. Revert the edit and put
the change in a new file instead; do not "fix" the pin unless the intent is to break every
database that applied the old bytes.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from data_factory.database import _migration_checksum

HERE = Path(__file__).resolve().parent
SQL_DIR = HERE.parent / "sql"

# the checksum the ledger will store for each file, computed the way the library computes it
APPLIED_CHECKSUMS = {
    "010_credit_schema.sql": (
        "28926a70aca3db19b6b9cfe17b7f51b0602c4d16fdcd25e31598d1f2aa91f33f"
    ),
    "011_credit_staging.sql": (
        "410003ea3aac1186c835cb03e6a127ff3b401608c2601112a942c635e3d2a407"
    ),
}


def file_checksum(path: Path) -> str:
    return _migration_checksum(path.read_text(encoding="utf-8"))


class MigrationImmutabilityTests(unittest.TestCase):
    def test_the_pinned_set_is_the_shipped_set(self):
        """No migration may be added or removed without saying so here.

        The point is the *name*: the ledger is keyed on it, so reusing a number for a
        different purpose collides with every database that applied the old file.
        """
        shipped = sorted(path.name for path in SQL_DIR.glob("*.sql"))
        self.assertEqual(shipped, sorted(APPLIED_CHECKSUMS))

    def test_no_applied_migration_has_been_edited_in_place(self):
        drifted = {
            path.name: file_checksum(path)
            for path in sorted(SQL_DIR.glob("*.sql"))
            if file_checksum(path) != APPLIED_CHECKSUMS[path.name]
        }
        self.assertEqual(
            drifted,
            {},
            msg=(
                "these files have already been applied somewhere, and the checksum is what "
                f"those databases will refuse: {drifted}"
            ),
        )

    def test_measurement_sql_is_never_a_migration(self):
        """``sql/queries/`` is read by the notebook, so it must not be in the ledger."""
        queries = sorted(path.name for path in (SQL_DIR / "queries").glob("*.sql"))
        self.assertEqual(
            queries, ["credit_missingness_parity.sql", "credit_sentinel_spikes.sql"]
        )
        for name in queries:
            self.assertNotIn(name, APPLIED_CHECKSUMS)

    def test_the_guard_below_bites_on_a_one_character_edit(self):
        """A negative control: the tripwire is live, not decorative.

        ``sha256`` of the same text with one byte changed has to differ, otherwise the pin
        above is comparing nothing and this whole file is theatre.
        """
        path = SQL_DIR / "010_credit_schema.sql"
        text = path.read_text(encoding="utf-8")
        edited = text.replace(
            "CREATE SCHEMA IF NOT EXISTS credit;",
            "CREATE SCHEMA IF NOT EXISTS credit ;",
            1,
        )
        self.assertNotEqual(edited, text)
        self.assertNotEqual(_migration_checksum(edited), file_checksum(path))


if __name__ == "__main__":
    unittest.main()
