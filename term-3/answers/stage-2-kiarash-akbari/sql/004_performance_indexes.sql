-- 004_performance_indexes.sql
-- Indexes of the dashboard queries, the snapshot grain guard and the range
-- refresh procedure. Applied by `data_factory load-database` with 001-003,
-- before the CSV load and inside the load's transaction, so this file must
-- not use CREATE INDEX CONCURRENTLY, VACUUM or COMMIT.
--
-- Every index below has plan evidence (with vs without the index, same query,
-- same session settings) in data/curated/evidence/index-evidence.json, built
-- by olist_perf.evidence.index_evidence(). Sizes and the write cost are in
-- index-cost.json. The two indexes that were built, measured and dropped are
-- documented in sql/rejected/004_rejected_*.sql.

-- Schema for DataFrames the notebook writes back with pandas.to_sql and an
-- explicit dtype map (olist_perf.bridge.write_typed). Kept out of olist so a
-- --reset of the Olist schema and a notebook export never touch each other.
CREATE SCHEMA IF NOT EXISTS reporting;

-- Q1 (hybrid): the live part reads only the months after the snapshot. Without
-- this index the range predicate is a Seq Scan over all 99k orders; with it
-- PostgreSQL reads the tail months through a Bitmap Index Scan.
CREATE INDEX IF NOT EXISTS ix_orders_purchase_ts
    ON olist.orders (order_purchase_timestamp);

-- Q4 and Q5 exclude canceled/unavailable orders with NOT EXISTS against this
-- partial index (about 1.2k entries, under 0.1 MB) instead of joining all 99k
-- orders to read their status.
CREATE INDEX IF NOT EXISTS ix_orders_inactive
    ON olist.orders (order_id)
    WHERE order_status IN ('canceled', 'unavailable');

-- Q4: the top-20 sellers' orders need status, approval and carrier dates.
-- INCLUDE makes the ~21k lookups Index Only Scans instead of heap fetches.
CREATE INDEX IF NOT EXISTS ix_orders_dispatch_cover
    ON olist.orders (order_id)
    INCLUDE (order_status, order_approved_at, order_delivered_carrier_date);

-- Q3 and Q4: per-order review averages. Q3 reads the whole index in order_id
-- order (Index Only Scan, no Sort before the GroupAggregate); Q4 probes it
-- once per top-seller order.
CREATE INDEX IF NOT EXISTS ix_order_reviews_order_score
    ON olist.order_reviews (order_id)
    INCLUDE (review_score);

-- Q5: one window per order over payments already sorted by
-- (order_id, payment_type, payment_installments); payment_value is INCLUDEd
-- so the scan is Index Only and the WindowAgg needs no Sort.
CREATE INDEX IF NOT EXISTS ix_payments_order_type_inst
    ON olist.payments (order_id, payment_type, payment_installments)
    INCLUDE (payment_value);

-- Snapshot grain guard: one row per (month, category), NULL category included.
-- The procedure is idempotent by DELETE + INSERT; this constraint makes a
-- second writer that bypasses it fail instead of duplicating a month.
ALTER TABLE olist.monthly_category_snapshot
    ADD CONSTRAINT uq_monthly_category_snapshot_grain
    UNIQUE NULLS NOT DISTINCT (month_start, product_category_name);

-- Rebuild a range of closed months by calling the one-month procedure for
-- each month. Runs inside the caller's transaction: an error in any month
-- rolls back every month of the call, so the snapshot is never half-built.
CREATE OR REPLACE PROCEDURE olist.refresh_monthly_category_snapshot_range(
    first_month DATE,
    last_month DATE
)
LANGUAGE plpgsql
AS $$
DECLARE
    month_value DATE;
BEGIN
    IF first_month IS NULL OR last_month IS NULL THEN
        RAISE EXCEPTION 'first_month and last_month must not be NULL'
            USING ERRCODE = '22004';
    END IF;
    IF date_trunc('month', first_month::timestamp)
       > date_trunc('month', last_month::timestamp) THEN
        RAISE EXCEPTION 'first_month % is after last_month %',
            first_month, last_month
            USING ERRCODE = '22023';
    END IF;
    FOR month_value IN
        SELECT generate_series(
                   date_trunc('month', first_month::timestamp),
                   date_trunc('month', last_month::timestamp),
                   INTERVAL '1 month'
               )::date
    LOOP
        CALL olist.refresh_monthly_category_snapshot(month_value);
    END LOOP;
END;
$$;
