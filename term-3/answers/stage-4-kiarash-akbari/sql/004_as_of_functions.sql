-- 004_as_of_functions.sql
-- The as-of rule, written out longhand.
--
-- These functions are the *specification* of the two history features in 003. They are
-- deliberately slow and obvious: a `WHERE` clause that says, in plain words, "rows
-- approved before this instant and nothing else". 003 implements the same two numbers with
-- one window pass so the frame can meet its time budget, and
-- `tests/test_capstone_sql_guards.py` compares the two row by row over a sample of orders.
-- If someone later "simplifies" the window and changes the semantics, that test is what
-- notices -- not the comment.
--
-- ROADMAP phase 4 item 2 asks for the as-of condition to be *in* the SQL. A function body
-- is the smallest possible place for such a condition to live, and the largest possible
-- audience: it can be run, grepped and read in psql without touching Python.

-- How many orders this customer had already had, strictly before the decision instant.
CREATE OR REPLACE FUNCTION capstone.customer_order_volume_before(
    p_customer_id text,
    p_at timestamp with time zone
)
RETURNS bigint
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT COUNT(*)
    FROM olist.orders AS o
    JOIN olist.customers AS previous_customer
      ON previous_customer.customer_id = o.customer_id
    JOIN olist.customers AS current_customer
      ON current_customer.customer_id = p_customer_id
     AND current_customer.customer_unique_id = previous_customer.customer_unique_id
    WHERE TRUE
      -- the as-of condition, in the WHERE clause rather than in a window frame
      AND o.order_approved_at IS NOT NULL
      AND o.order_approved_at < p_at;
$$;

-- Days since that customer's previous approval, at the same instant. NULL when there is no
-- earlier order, which is reported as NULL and never as 0 (decisions.md item 7).
CREATE OR REPLACE FUNCTION capstone.customer_days_since_prior_order(
    p_customer_id text,
    p_at timestamp with time zone
)
RETURNS double precision
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT EXTRACT(EPOCH FROM (p_at - MAX(o.order_approved_at))) / 86400.0
    FROM olist.orders AS o
    JOIN olist.customers AS previous_customer
      ON previous_customer.customer_id = o.customer_id
    JOIN olist.customers AS current_customer
      ON current_customer.customer_id = p_customer_id
     AND current_customer.customer_unique_id = previous_customer.customer_unique_id
    WHERE TRUE
      AND o.order_approved_at IS NOT NULL
      AND o.order_approved_at < p_at;
$$;

-- Seller history, which is the aggregate a reader most expects to find in a feature
-- frame -- and the one that most easily leaks, because "this seller's late-delivery rate"
-- is computed from rows that only exist after the order under review was placed. So the
-- seller function counts orders, not outcomes: every outcome column in the schema is
-- forbidden by the audit, and this function cannot see one even if edited to try.
CREATE OR REPLACE FUNCTION capstone.seller_order_volume_before(
    p_seller_id text,
    p_at timestamp with time zone
)
RETURNS bigint
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT COUNT(DISTINCT i.order_id)
    FROM olist.order_items AS i
    JOIN olist.orders AS o ON o.order_id = i.order_id
    WHERE i.seller_id = p_seller_id
      AND o.order_approved_at IS NOT NULL
      AND o.order_approved_at < p_at;
$$;

-- A mean price is safe for the same reason the volume is: both sides of it are set before
-- the anchor. The 21-item order in the data is also why the aggregate is over items rather
-- than orders: at most 21 rows can enter one order's item group, so the number is bounded
-- by construction and needs no cap.
CREATE OR REPLACE FUNCTION capstone.seller_mean_price_before(
    p_seller_id text,
    p_at timestamp with time zone
)
RETURNS double precision
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT AVG(i.price)
    FROM olist.order_items AS i
    JOIN olist.orders AS o ON o.order_id = i.order_id
    WHERE i.seller_id = p_seller_id
      AND o.order_approved_at IS NOT NULL
      AND o.order_approved_at < p_at;
$$;

COMMENT ON FUNCTION capstone.customer_order_volume_before(text, timestamptz) IS
    'the as-of specification behind v_feature_frame.prior_orders_by_customer; slow on '
    'purpose, and compared to the window implementation by tests/test_capstone_sql_guards.py';
