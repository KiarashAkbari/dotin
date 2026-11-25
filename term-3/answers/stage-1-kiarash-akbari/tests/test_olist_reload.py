"""A reload empties the Olist tables with one set-based TRUNCATE, not a DELETE per table.

None of the foreign-key columns of the olist schema is indexed, so a DELETE of a
parent table runs one referential check per deleted row, each a scan of the child
table: quadratic on a reload. TRUNCATE checks the foreign keys between the listed
tables once, as a set. The real-PostgreSQL side (a reload under lock_timeout, rows
not doubled) is in test_load_database_snapshots.
"""

import unittest

from data_factory.database import OLIST_SCHEMA, OLIST_TABLES, clear_olist_tables


class RecordingConnection:
    def __init__(self):
        self.statements = []

    def execute(self, statement, parameters=None):
        self.statements.append(str(statement))


class ClearOlistTablesTests(unittest.TestCase):
    def setUp(self):
        self.connection = RecordingConnection()
        clear_olist_tables(self.connection)

    def test_one_statement_for_the_whole_set(self):
        self.assertEqual(len(self.connection.statements), 1)
        statement = self.connection.statements[0]
        self.assertTrue(statement.startswith("TRUNCATE TABLE "), statement)
        self.assertNotIn("DELETE", statement.upper())

    def test_every_olist_table_is_in_the_set(self):
        statement = self.connection.statements[0]
        listed = statement.removeprefix("TRUNCATE TABLE ").split(" RESTART")[0].split(", ")
        self.assertEqual(sorted(listed), sorted(f"{OLIST_SCHEMA}.{t}" for t in OLIST_TABLES))

    def test_identities_restart_and_nothing_cascades(self):
        statement = self.connection.statements[0]
        self.assertTrue(statement.endswith(" RESTART IDENTITY"), statement)
        # A table outside olist that references these must stop the reload, not
        # be emptied with it.
        self.assertNotIn("CASCADE", statement.upper())


if __name__ == "__main__":
    unittest.main()
