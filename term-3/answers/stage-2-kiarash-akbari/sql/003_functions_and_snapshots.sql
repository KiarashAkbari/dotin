-- Apply after 002_analytical_views.sql.
-- A date-only promise is evaluated by calendar day, not midnight time.
-- Positive = late; negative = early; unavailable dates produce NULL.
CREATE OR REPLACE FUNCTION olist.delivery_delay_days(
    actual_delivery TIMESTAMP WITHOUT TIME ZONE,
    promised_delivery TIMESTAMP WITHOUT TIME ZONE
)
RETURNS INTEGER
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
    SELECT actual_delivery::date - promised_delivery::date;
$$;

-- One row per purchase month and Portuguese product category.
-- NULL category is retained. No performance indexes are introduced here.
-- This is an explicit freshness/space comparison artifact, not a silent
-- replacement for the five live dashboard queries.
CREATE TABLE IF NOT EXISTS olist.monthly_category_snapshot (
    month_start DATE NOT NULL,
    product_category_name TEXT,
    sales_value NUMERIC NOT NULL CHECK (sales_value >= 0),
    order_count BIGINT NOT NULL CHECK (order_count > 0),
    average_order_value NUMERIC NOT NULL CHECK (average_order_value >= 0),
    refreshed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT ck_snapshot_month_start CHECK (
        month_start = date_trunc('month', month_start::timestamp)::date
    )
);

-- Caller must use a transaction (e.g. SQLAlchemy engine.begin()).
-- No COMMIT or exception swallowing: an error rolls back the DELETE and INSERT.
-- The transaction-level advisory lock serializes rebuilds of the same month.
-- NULL/any day of a month: NULL is rejected; other days normalize to day one.
CREATE OR REPLACE PROCEDURE olist.refresh_monthly_category_snapshot(
    requested_month DATE
)
LANGUAGE plpgsql
AS $$
DECLARE
    target_month DATE;
    next_month DATE;
BEGIN
    IF requested_month IS NULL THEN
        RAISE EXCEPTION 'requested_month must not be NULL'
            USING ERRCODE = '22004';
    END IF;
    target_month := date_trunc('month', requested_month::timestamp)::date;
    next_month := (target_month + INTERVAL '1 month')::date;
    PERFORM pg_advisory_xact_lock(
        47202,
        EXTRACT(YEAR FROM target_month)::integer * 12
            + EXTRACT(MONTH FROM target_month)::integer
    );
    DELETE FROM olist.monthly_category_snapshot
    WHERE month_start = target_month;

    INSERT INTO olist.monthly_category_snapshot (
        month_start, product_category_name, sales_value,
        order_count, average_order_value, refreshed_at
    )
    SELECT target_month, product_category_name,
           SUM(item_value), COUNT(DISTINCT order_id),
           SUM(item_value) / COUNT(DISTINCT order_id),
           transaction_timestamp()
    FROM olist.v_order_item_sales
    WHERE order_purchase_timestamp >= target_month::timestamp
      AND order_purchase_timestamp < next_month::timestamp
    GROUP BY product_category_name;
END;
$$;
