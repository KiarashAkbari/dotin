"""Requires TEST_DATABASE_URL pointing to an empty disposable PostgreSQL DB."""
import os
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

SQL = Path(__file__).resolve().parents[1] / "sql"
FIXTURE = """
INSERT INTO olist.customers VALUES
(repeat('a',32), repeat('b',32), '00123', 'city', 'SP');
INSERT INTO olist.sellers VALUES (repeat('c',32), '00123', 'city', 'SP');
INSERT INTO olist.product_category_name_translation VALUES ('books', 'books');
INSERT INTO olist.products (product_id, product_category_name)
VALUES (repeat('d',32), 'books');
INSERT INTO olist.orders (order_id, customer_id, order_status,
order_purchase_timestamp, order_approved_at, order_delivered_carrier_date,
order_delivered_customer_date, order_estimated_delivery_date)
VALUES (repeat('e',32), repeat('a',32), 'delivered', '2018-01-01',
'2018-01-02', '2018-01-04', '2018-01-11 12:00', '2018-01-10');
INSERT INTO olist.order_items VALUES
(repeat('e',32), 1, repeat('d',32), repeat('c',32), '2018-01-05', 10, 1),
(repeat('e',32), 2, repeat('d',32), repeat('c',32), '2018-01-05', 20, 2);
INSERT INTO olist.payments VALUES
(repeat('e',32), 1, 'credit_card', 2, 10),
(repeat('e',32), 2, 'voucher', 1, 23);
INSERT INTO olist.order_reviews
(review_id, order_id, review_score, review_creation_date) VALUES
(repeat('f',32), repeat('e',32), 1, '2018-01-12'),
(repeat('0',32), repeat('e',32), 5, '2018-01-13');
"""


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class PostgresIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = sa.create_engine(os.environ["TEST_DATABASE_URL"])
        self.addCleanup(self.engine.dispose)
        if self.engine.dialect.name != "postgresql":
            self.fail("Integration tests require PostgreSQL")
        self.conn = self.engine.connect()
        self.addCleanup(self.conn.close)
        self.tx = self.conn.begin()
        self.addCleanup(self.tx.rollback)
        if self.conn.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = :name)"), {"name": "olist"}):
            self.fail("Refusing to run: olist schema already exists; use an empty disposable database")
        for filename in ("001_create_olist_schema.sql", "002_analytical_views.sql", "003_functions_and_snapshots.sql"):
            self.conn.execute(sa.text((SQL / filename).read_text(encoding="utf-8")))
        self.conn.execute(sa.text(FIXTURE))
        for filename in (
            "005_q4_seller_snapshot.sql",
            "006_q5_payment_snapshot.sql",
            "007_q1_monthly_category_snapshot.sql",
            "008_q3_delay_satisfaction_snapshot.sql",
        ):
            self.conn.execute(
                sa.text((SQL / filename).read_text(encoding="utf-8"))
            )

    def query(self, filename):
        statement = sa.text((SQL / "queries" / filename).read_text(encoding="utf-8"))
        return self.conn.execute(statement).mappings().all()

    def refresh(self):
        self.conn.execute(sa.text("CALL olist.refresh_monthly_category_snapshot(CAST(:month AS date))"), {"month": "2018-01-15"})

    def snapshot(self):
        return self.conn.execute(sa.text("SELECT month_start, product_category_name, sales_value, order_count, average_order_value FROM olist.monthly_category_snapshot ORDER BY month_start, product_category_name NULLS LAST")).all()

    def test_function_calendar_day_signs(self):
        statement = sa.text("SELECT olist.delivery_delay_days(:actual, :promised)")
        for day, expected in ((9, -1), (10, 0), (11, 1)):
            with self.subTest(day=day):
                actual = datetime(2018, 1, day, 23, 59)
                value = self.conn.scalar(statement, {"actual": actual, "promised": datetime(2018, 1, 10)})
                self.assertEqual(value, expected)

    def test_function_null(self):
        self.assertIsNone(self.conn.scalar(sa.text("SELECT olist.delivery_delay_days(NULL::timestamp, '2018-01-10'::timestamp)")))

    def test_q1_no_item_or_review_multiplication(self):
        rows = self.query("q1_monthly_category_sales.sql")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sales_value"], Decimal("33"))
        self.assertEqual(rows[0]["unique_orders"], 1)
        self.assertEqual(rows[0]["average_order_value"], Decimal("33"))

    def test_q2_late_ratio(self):
        rows = self.query("q2_delivery_by_state_month.sql")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["customer_state"], "SP")
        self.assertEqual(rows[0]["delivered_orders"], 1)
        self.assertEqual(rows[0]["late_orders"], 1)
        self.assertEqual(rows[0]["late_ratio"], Decimal("1"))

    def test_q3_review_grain(self):
        rows = self.query("q3_delay_and_satisfaction.sql")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bucket"], 1)
        self.assertEqual(rows[0]["reviewed_orders"], 1)
        self.assertEqual(rows[0]["mean_review_score"], Decimal("3"))

    def test_q4_seller_grain(self):
        rows = self.query("q4_top_sellers.sql")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["revenue"], Decimal("33"))
        self.assertEqual(rows[0]["seller_orders"], 1)
        self.assertEqual(rows[0]["mean_preparation_days"], Decimal("2"))
        self.assertEqual(rows[0]["mean_review_score"], Decimal("3"))

    def test_q5_payment_shares(self):
        rows = self.query("q5_payment_distribution.sql")
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(r["payment_record_share"] for r in rows), Decimal("1"))
        self.assertEqual(sum(r["paid_value"] for r in rows), Decimal("33"))
        self.assertTrue(all(r["correlation_orders"] == 1 for r in rows))
        self.assertTrue(all(r["installment_value_correlation"] is None for r in rows))

    def test_procedure_idempotency(self):
        self.refresh()
        before = self.snapshot()
        self.assertEqual(len(before), 1)
        self.refresh()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(before[0][2], Decimal("33"))

    def test_procedure_mid_insert_failure_restores_previous_snapshot(self):
        self.refresh()
        before = self.snapshot()
        self.conn.execute(sa.text("""
CREATE FUNCTION olist.reject_snapshot_insert() RETURNS trigger
LANGUAGE plpgsql AS $$ BEGIN
RAISE EXCEPTION 'intentional test failure' USING ERRCODE = 'P0001';
END; $$;
CREATE TRIGGER reject_snapshot_insert BEFORE INSERT
ON olist.monthly_category_snapshot FOR EACH ROW
EXECUTE FUNCTION olist.reject_snapshot_insert();
"""))
        with self.assertRaises(sa.exc.DBAPIError):
            with self.conn.begin_nested():
                self.refresh()
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
