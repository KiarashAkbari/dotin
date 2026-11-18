"""The audit is the allowlist: no row, no column -- and one of those rows is the leak test.

``04`` makes the audit table the thing that decides what the model may read, so the
guard has to be tested as behaviour, not described in a report. The cases below are
the ones the real feature frame will hit: a column nobody audited, a column audited
and forbidden, a row that claims ``allowed`` with no source under it, and a row that
claims it was available at approval while naming a later moment.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import helpers
from capstone.audit import (
    AUDIT_HEADER,
    allowed_columns,
    audit_problems,
    columns_from_ddl,
    coverage,
    load_audit,
    parse_audit,
    require_allowed,
    summarise,
    template_rows,
)
from capstone.errors import AuditError

GOOD = [
    "orders,order_id,purchase,TRUE,pk of the source file,allowed",
    "orders,order_approved_at,approval,TRUE,the decision moment itself,allowed",
    "orders,order_delivered_customer_date,delivery,FALSE,only written when the carrier reports,forbidden",
    "order_reviews,review_score,review_created,FALSE,written days after approval,forbidden",
    "payments,payment_purchase_date,purchase,TRUE,same transaction as the order,allowed",
]


class ParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_file_names_the_phase_that_owes_it(self) -> None:
        with self.assertRaises(AuditError) as caught:
            load_audit(self.root / "column_audit.csv")
        message = str(caught.exception)
        self.assertIn("column_audit.csv", message)
        self.assertIn("phase 2", message)

    def test_header_mismatch_is_refused_with_both_headers(self) -> None:
        text = "table,column,verdict\norders,order_id,allowed\n"
        with self.assertRaises(AuditError) as caught:
            parse_audit(text, origin="x.csv")
        self.assertIn(",".join(AUDIT_HEADER), str(caught.exception))

    def test_empty_file_is_refused(self) -> None:
        with self.assertRaises(AuditError):
            parse_audit("\n", origin="x.csv")

    def test_unknown_verdict_is_refused_by_line_number(self) -> None:
        text = ",".join(AUDIT_HEADER) + "\norders,order_id,purchase,TRUE,s,maybe\n"
        with self.assertRaises(AuditError) as caught:
            parse_audit(text, origin="x.csv")
        self.assertIn("line 2", str(caught.exception))

    def test_a_row_that_loses_a_field_is_refused(self) -> None:
        """A hand-edited CSV drops a comma easily; that must not shift a verdict leftwards."""
        text = ",".join(AUDIT_HEADER) + "\ncustomers,city,purchase,TRUE,allowed\n"
        with self.assertRaises(AuditError) as caught:
            parse_audit(text, origin="short.csv")
        self.assertIn("5 fields", str(caught.exception))

    def test_round_trip_of_a_good_file(self) -> None:
        path = helpers.write_audit(self.root / "column_audit.csv", GOOD)
        audit = load_audit(path)
        self.assertEqual(len(audit), 5)
        self.assertEqual(audit[0].key, "orders.order_id")
        self.assertTrue(audit[1].usable)
        self.assertFalse(audit[2].usable)

    def test_case_is_normalised(self) -> None:
        rows = ["Orders,Order_ID,purchase,true,S,ALLOWED"]
        audit = parse_audit(",".join(AUDIT_HEADER) + "\n" + rows[0], origin="x.csv")
        self.assertEqual(audit[0].key, "orders.order_id")
        self.assertTrue(audit[0].usable)


class ProblemsTest(unittest.TestCase):
    def rows(self, extra: list[str]) -> list:
        return parse_audit("\n".join([",".join(AUDIT_HEADER), *GOOD, *extra]), origin="t")

    def test_a_clean_table_has_no_problems(self) -> None:
        self.assertEqual(audit_problems(self.rows([])), [])

    def test_allowed_without_a_source_is_a_problem(self) -> None:
        audit = self.rows(["customers,city,purchase,TRUE,,allowed"])
        problems = audit_problems(audit)
        self.assertEqual(len(problems), 1)
        self.assertIn("customers.city", problems[0])
        self.assertNotIn("customers.city", allowed_columns(audit))

    def test_allowed_but_not_available_at_approval_is_a_problem(self) -> None:
        audit = self.rows(["sellers,seller_city,purchase,FALSE,source says so,allowed"])
        self.assertTrue(any("at_approval" in problem for problem in audit_problems(audit)))
        self.assertNotIn("sellers.seller_city", allowed_columns(audit))

    def test_contradicting_duplicates_are_a_problem(self) -> None:
        audit = self.rows(
            [
                "orders,order_status,approval,TRUE,status column,allowed",
                "orders,order_status,delivery,FALSE,same name later,forbidden",
            ]
        )
        self.assertTrue(any("contradicting" in p for p in audit_problems(audit)))
        self.assertNotIn("orders.order_status", allowed_columns(audit))

    def test_unverified_is_never_usable(self) -> None:
        audit = self.rows(["products,weight_max,unknown,TRUE,a guess,unverified"])
        self.assertNotIn("products.weight_max", allowed_columns(audit))
        self.assertTrue(summarise(audit)["unverified"])


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.audit = parse_audit("\n".join([",".join(AUDIT_HEADER), *GOOD]), origin="t")

    def test_allowed_columns_is_exactly_the_usable_rows(self) -> None:
        self.assertEqual(
            allowed_columns(self.audit),
            {"orders.order_id", "orders.order_approved_at", "payments.payment_purchase_date"},
        )

    def test_an_unaudited_column_stops_the_frame(self) -> None:
        """This is the test 10.3 asks for: add a column, go red."""
        with self.assertRaises(AuditError) as caught:
            require_allowed(self.audit, ["orders.order_id", "orders.shipping_limit_date"])
        message = str(caught.exception)
        self.assertIn("orders.shipping_limit_date", message)
        self.assertIn("not in the audit at all", message)
        self.assertEqual(
            caught.exception.context["offenders"],
            ["orders.shipping_limit_date: not in the audit at all"],
        )

    def test_a_forbidden_column_is_named_as_forbidden(self) -> None:
        with self.assertRaises(AuditError) as caught:
            require_allowed(self.audit, ["orders.order_delivered_customer_date"])
        self.assertIn("forbidden", str(caught.exception))

    def test_the_allowed_frame_passes(self) -> None:
        used = ["orders.order_id", "payments.payment_purchase_date"]
        self.assertEqual(require_allowed(self.audit, used), None)


class CoverageTest(unittest.TestCase):
    DDL = """
CREATE TABLE olist.orders (
    order_id           VARCHAR(32) NOT NULL,
    customer_id        VARCHAR(32) NOT NULL,
    order_status       TEXT        NOT NULL,
    order_approved_at  TIMESTAMP,
    CONSTRAINT pk_orders PRIMARY KEY (order_id)
);

CREATE TABLE olist.order_reviews (
    review_id    VARCHAR(32) NOT NULL,
    order_id     VARCHAR(32) NOT NULL,
    review_score SMALLINT    NOT NULL,
    CONSTRAINT pk_order_reviews PRIMARY KEY (review_id, order_id)
);
"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.ddl = self.root / "001_ddl.sql"
        self.ddl.write_text(self.DDL, encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ddl_columns_are_read_without_a_database(self) -> None:
        tables = columns_from_ddl(self.ddl)
        self.assertEqual(
            tables["orders"],
            ["order_id", "customer_id", "order_status", "order_approved_at"],
        )
        self.assertEqual(tables["order_reviews"], ["review_id", "order_id", "review_score"])

    def test_coverage_counts_the_gap_not_the_boilerplate(self) -> None:
        audit = parse_audit("\n".join([",".join(AUDIT_HEADER), *GOOD]), origin="t")
        gap = coverage(audit, columns_from_ddl(self.ddl))
        self.assertEqual(gap["orders"], 2)  # customer_id, order_status
        self.assertEqual(gap["order_reviews"], 2)  # review_id and order_id are not audited

    def test_a_generated_template_cannot_make_anything_usable(self) -> None:
        rows = columns_from_ddl(self.ddl)
        text = template_rows(rows)
        audit = parse_audit(text, origin="template")
        self.assertEqual(allowed_columns(audit), frozenset())
        self.assertEqual(summarise(audit)["unverified"], 7)
        self.assertEqual(coverage(audit, rows), {})

    def test_the_real_stage2_ddl_has_all_nine_tables(self) -> None:
        tables = columns_from_ddl(helpers.STAGE2_DDL)
        self.assertEqual(len(tables), 9)
        self.assertIn("order_approved_at", tables["orders"])
        self.assertNotIn("shipping_limit_date", tables["orders"])

    def test_a_multiline_check_does_not_become_a_column(self) -> None:
        """The parser was wrong here once, in the one table with multi-line CHECKs.

        ``pgserver`` reports 53 columns for the ``olist`` schema, and the generated
        witness list is the audit's denominator, so the two counts have to agree. The
        log in ``docs/evidence/scaffold-run.txt`` records the run that compared them.
        """
        tables = columns_from_ddl(helpers.STAGE2_DDL)
        self.assertEqual(sum(len(columns) for columns in tables.values()), 53)
        self.assertEqual(
            tables["geolocation"],
            [
                "geolocation_id",
                "geolocation_zip_code_prefix",
                "geolocation_lat",
                "geolocation_lng",
                "geolocation_city",
                "geolocation_state",
            ],
        )
        for columns in tables.values():
            self.assertNotIn("between", columns)
            self.assertNotIn("and", columns)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
