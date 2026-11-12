"""Text-level contract tests for 010_credit_schema.sql.

No database is touched here. These tests hold the DDL to the promises the loader
and the notebook depend on, so a schema edit that would break a load fails in the
offline suite instead of during a delivery run - the same habit
``stage-2/tests/test_factory_sink_sql.py`` established, including the rule that
the caller owns the transaction and that nothing constrains an unmeasured range.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGE1_SRC = HERE.parents[1] / "stage-1" / "src"
for candidate in (HERE, HERE.parent / "src", STAGE1_SRC):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from data_factory.cli import (
    _is_base_migration,
    _is_sink_migration,
    _is_snapshot_migration,
)

from credit.columns import (
    COLUMNS,
    DB_INPUT_VIEW,
    DB_MISSINGNESS_VIEW,
    DB_RUN_TABLE,
    DB_SCHEMA,
    DB_TABLE,
    DERIVED_COLUMNS,
    LATE_PAYMENT_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    VIEW_SELECT_COLUMNS,
)
from credit.load import digest_query

MIGRATION = HERE.parent / "sql" / "010_credit_schema.sql"
DDL = MIGRATION.read_text(encoding="utf-8")

TABLE_BODIES = {
    name: body
    for name, body in re.findall(
        r"CREATE TABLE IF NOT EXISTS credit\.(\w+) \((.*?)\n\);", DDL, re.DOTALL
    )
}
VIEW_BODIES = {
    name: body
    for name, body in re.findall(
        r"CREATE OR REPLACE VIEW credit\.(\w+) AS(.*?)\n\n", DDL, re.DOTALL
    )
}


def _body(table: str) -> str:
    assert table in TABLE_BODIES, f"{table} is not created by this migration"
    return TABLE_BODIES[table]


AGGREGATES = ("count", "sum", "avg", "min", "max", "bool_or", "bool_and", "every")

#: every SQL text this stage sends to the server, including the one built in Python
SQL_TEXTS: dict[str, str] = {
    MIGRATION.name: DDL,
    "011_credit_staging.sql": (MIGRATION.parent / "011_credit_staging.sql").read_text(
        encoding="utf-8"
    ),
    "credit_sentinel_spikes.sql": (
        MIGRATION.parent / "queries" / "credit_sentinel_spikes.sql"
    ).read_text(encoding="utf-8"),
    "credit_missingness_parity.sql": (
        MIGRATION.parent / "queries" / "credit_missingness_parity.sql"
    ).read_text(encoding="utf-8"),
    "digest_query": digest_query(DB_SCHEMA),
}


def _function_before(text: str, index: int) -> str:
    """The name of the call whose closing paren a FILTER clause attaches to.

    Grouping parens are walked through, because the mistake this guard exists for -
    ``round((avg(x)::numeric) FILTER (...))`` - has a bare group in front of FILTER
    rather than a call, and the real function behind it is the outer ``round``.
    """
    j = index - 1
    while j >= 0:
        while j >= 0 and text[j].isspace():
            j -= 1
        if j < 0 or text[j] != ")":
            return ""
        depth = 0
        while j >= 0:
            if text[j] == ")":
                depth += 1
            elif text[j] == "(":
                depth -= 1
                if depth == 0:
                    break
            j -= 1
        opened_at = j
        k = opened_at - 1
        while k >= 0 and (text[k].isalnum() or text[k] == "_"):
            k -= 1
        if k != opened_at - 1:
            return text[k + 1 : opened_at].lower()
        # a group rather than a call: the name belongs to the pair that wraps it
        k = opened_at - 1
        while k >= 0 and text[k].isspace():
            k -= 1
        if k >= 0 and text[k] == "(":
            opened_at = k
            k = opened_at - 1
            while k >= 0 and (text[k].isalnum() or text[k] == "_"):
                k -= 1
            if k != opened_at - 1:
                return text[k + 1 : opened_at].lower()
        return ""
    return ""


class FilterClauseTests(unittest.TestCase):
    """`FILTER` binds to an aggregate call, and to nothing else.

    PostgreSQL rejects round(avg(x)::numeric) FILTER (...) with a syntax error, and the
    first time that was found it was by running the migration on a real server - which is
    not available to this suite. Reading the parenthesis nesting is the closest offline
    equivalent, and it is here so the next cast wrapped around an aggregate fails a test
    instead of a delivery.
    """

    def test_every_filter_clause_follows_an_aggregate_call(self):
        for name, text in SQL_TEXTS.items():
            for match in re.finditer(r"\bFILTER\s*\(", text):
                with self.subTest(
                    file=name, line=text[: match.start()].count("\n") + 1
                ):
                    self.assertIn(_function_before(text, match.start()), AGGREGATES)

    def _attached_function(self, text: str) -> str:
        match = re.search(r"\bFILTER\s*\(", text)
        assert match is not None, "the sample has no FILTER clause to read"
        return _function_before(text, match.start())

    def test_the_helper_reads_the_real_shape_of_the_bug(self):
        # a guard that cannot fail is not a guard: the exact mistake it exists for
        self.assertEqual(
            self._attached_function("round((avg(t)::numeric) FILTER (WHERE x))"),
            "round",
        )
        self.assertEqual(
            self._attached_function("round(avg(t) FILTER (WHERE x)::numeric, 5)"), "avg"
        )
        self.assertEqual(self._attached_function("count(*) FILTER ("), "count")


class FileHygieneTests(unittest.TestCase):
    def test_the_file_exists_and_is_numbered_next(self):
        self.assertTrue(MIGRATION.is_file())
        self.assertEqual(MIGRATION.name, "010_credit_schema.sql")
        # the next free prefix after stage 2's chain, so ordering is never a race
        self.assertEqual(MIGRATION.name[:3], "010")

    def test_stage_2_migration_groups_do_not_claim_this_file(self):
        # an Olist rebuild must never have to know that the credit schema exists
        self.assertFalse(_is_base_migration(MIGRATION))
        self.assertFalse(_is_snapshot_migration(MIGRATION))
        self.assertFalse(_is_sink_migration(MIGRATION))

    def test_the_caller_owns_the_transaction(self):
        self.assertNotIn("BEGIN;", DDL)
        self.assertNotIn("COMMIT;", DDL)

    def test_every_check_constraint_is_named(self):
        named = re.findall(r"CONSTRAINT (\w+) CHECK", DDL)
        self.assertEqual(len(named), DDL.count("CHECK ("))
        self.assertTrue(all(name.startswith("ck_credit_") for name in named))
        self.assertEqual(len(named), len(set(named)))

    def test_no_secondary_index_is_created(self):
        self.assertIsNone(re.search(r"CREATE (UNIQUE )?INDEX", DDL))


class ApplicationTableTests(unittest.TestCase):
    def test_the_schema_and_the_two_tables_are_created(self):
        self.assertIn("CREATE SCHEMA IF NOT EXISTS credit;", DDL)
        self.assertEqual(set(TABLE_BODIES), {DB_TABLE, DB_RUN_TABLE})

    def test_every_contract_column_is_declared_with_its_type(self):
        body = _body(DB_TABLE)
        for spec in COLUMNS:
            pattern = rf"(?m)^\s*{spec.db_name}\s+{spec.sql_type}\b"
            self.assertRegex(body, pattern, f"{spec.db_name} should be {spec.sql_type}")

    def test_only_the_id_and_the_label_are_not_null(self):
        body = _body(DB_TABLE)
        for spec in COLUMNS:
            declared = re.search(
                rf"^\s*{spec.db_name}[^,\n]*", body, re.MULTILINE
            ).group(0)
            self.assertEqual("NOT NULL" in declared, not spec.nullable, declared)

    def test_features_are_never_numeric(self):
        # PostgreSQL rejects NaN in numeric; missing values arrive as NULL instead
        body = _body(DB_TABLE)
        for line in body.splitlines():
            declared = line.strip()
            if declared.startswith(tuple(spec.db_name for spec in COLUMNS)):
                self.assertNotIn("numeric", declared, declared)

    def test_the_only_domain_check_on_the_data_is_the_label(self):
        body = _body(DB_TABLE)
        self.assertIn("CHECK (serious_dlqin_2yrs IN (0, 1))", body)
        for column in (
            "age",
            "revolving_utilization_unsecured",
            "num_dependents",
            "debt_ratio",
        ):
            self.assertNotIn(f"CHECK ({column}", body)
            self.assertNotIn(f"CHECK ( {column}", body)

    def test_the_primary_key_is_the_row_id(self):
        self.assertIn(
            "CONSTRAINT pk_credit_application PRIMARY KEY (row_id)", _body(DB_TABLE)
        )

    def test_raw_values_are_preserved_in_the_table(self):
        # codes are masked in the view, never destroyed here
        self.assertIn("sentinel codes stay here", DDL)


class ProvenanceTableTests(unittest.TestCase):
    def test_the_columns_a_loader_can_write(self):
        body = _body(DB_RUN_TABLE)
        for name in (
            "source_kind",
            "file_name",
            "sha256",
            "rows_loaded",
            "rejected_count",
            "fingerprint",
        ):
            self.assertIn(name, body)

    def test_the_hash_and_fingerprint_shapes_are_enforced(self):
        body = _body(DB_RUN_TABLE)
        self.assertIn("sha256        char(64)", body)
        self.assertIn("fingerprint   char(12)", body)
        self.assertIn("CHECK (sha256 ~ '^[0-9a-f]{64}$')", body)
        self.assertIn("CHECK (fingerprint ~ '^[0-9a-f]{12}$')", body)

    def test_the_provenance_vocabulary_matches_the_factory_schema(self):
        body = _body(DB_RUN_TABLE)
        self.assertIn("rows_loaded", body)
        self.assertNotIn("row_count", body)

    def test_this_stage_only_loads_files(self):
        self.assertIn("CHECK (source_kind = 'file')", _body(DB_RUN_TABLE))


class InputViewTests(unittest.TestCase):
    def test_the_view_the_notebook_reads_exists(self):
        self.assertIn(DB_INPUT_VIEW, VIEW_BODIES)

    def test_every_selected_name_comes_out_of_the_view(self):
        body = VIEW_BODIES[DB_INPUT_VIEW]
        for name in VIEW_SELECT_COLUMNS:
            self.assertIn(name, body)
        # the notebook names its columns: a column added to the view must not be
        # able to change a model matrix on its own
        self.assertNotIn("SELECT *", DDL)

    def test_the_codes_stop_being_numbers_for_every_late_column(self):
        body = VIEW_BODIES[DB_INPUT_VIEW]
        for column in LATE_PAYMENT_COLUMNS:
            self.assertIn(
                f"CASE WHEN a.{column} IN (96, 98) THEN NULL ELSE a.{column} END", body
            )

    def test_utilisation_is_capped_without_losing_the_cap_as_a_fact(self):
        body = VIEW_BODIES[DB_INPUT_VIEW]
        self.assertIn("LEAST(a.revolving_utilization_unsecured, 1.0)", body)
        self.assertIn("utilization_capped", body)

    def test_the_missing_income_indicator_is_row_wise(self):
        body = VIEW_BODIES[DB_INPUT_VIEW]
        self.assertIn(
            "CASE WHEN a.monthly_income IS NULL THEN 1 ELSE 0 END AS income_missing",
            body,
        )

    def test_the_view_derives_no_fitted_statistic(self):
        # no average, no percentile, no window: a view cannot be fitted on one split
        body = VIEW_BODIES[DB_INPUT_VIEW]
        for forbidden in (
            "avg(",
            "percentile_cont",
            "stddev",
            " OVER (",
            "max(",
            "min(",
        ):
            self.assertNotIn(forbidden, body.lower())

    def test_each_derived_column_is_produced_exactly_once(self):
        body = VIEW_BODIES[DB_INPUT_VIEW]
        for name in DERIVED_COLUMNS:
            self.assertEqual(len(re.findall(rf"AS {name}\b", body)), 1, name)


class MissingnessViewTests(unittest.TestCase):
    def test_one_branch_per_model_feature(self):
        body = VIEW_BODIES[DB_MISSINGNESS_VIEW]
        self.assertEqual(body.count("UNION ALL"), len(MODEL_FEATURE_COLUMNS) - 1)
        for column in MODEL_FEATURE_COLUMNS:
            self.assertIn(f"'{column}'", body)

    def test_absence_is_counted_by_count_ignoring_nulls(self):
        body = VIEW_BODIES[DB_MISSINGNESS_VIEW]
        self.assertIn("count(*) - count(flagged.value)", body)

    def test_the_default_rate_is_split_by_absence(self):
        body = VIEW_BODIES[DB_MISSINGNESS_VIEW]
        self.assertIn("FILTER (WHERE flagged.value IS NULL)", body)
        self.assertIn("FILTER (WHERE flagged.value IS NOT NULL)", body)

    def test_the_view_reads_the_raw_table_not_the_masked_one(self):
        # a row that carries a code is a row that was reported
        body = VIEW_BODIES[DB_MISSINGNESS_VIEW]
        self.assertIn("FROM credit.application", body)
        self.assertNotIn(DB_INPUT_VIEW, body)


class MigrationOrderingTests(unittest.TestCase):
    def test_the_schema_prefix_is_reserved_for_the_file(self):
        self.assertTrue(DDL.startswith("-- Stage 3, step 1"))

    def test_all_referenced_tables_are_created_in_the_same_file(self):
        referenced = set(re.findall(r"(?:FROM|INTO|UPDATE)\s+credit\.(\w+)", DDL))
        created = set(TABLE_BODIES) | set(VIEW_BODIES)
        self.assertTrue(referenced <= created, referenced - created)

    def test_the_constraint_names_are_scoped_to_this_schema(self):
        for name in re.findall(r"CONSTRAINT (\w+)", DDL):
            self.assertTrue(name.startswith(("pk_credit_", "ck_credit_")), name)


if __name__ == "__main__":
    unittest.main()
