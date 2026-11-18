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
--
-- `p_at` is `timestamp`, not `timestamp with time zone`, and that is a measured choice rather
-- than a preference. `olist.orders.order_approved_at` is `TIMESTAMP` -- naive, no offset -- as
-- `stage-2/sql/001_create_olist_schema.sql` defines it, and 003's window compares naive values
-- to each other. Ask the same question through a `timestamptz` parameter and PostgreSQL resolves
-- the *column* side through the session zone first, so a session sitting in a zone with
-- daylight time shifts the answer by an hour for part of the year. The live run in
-- live run on the student's own PostgreSQL is what found it, on order
-- `003a7f59d7e08a9c61d9e2881fe6459c` the frame's window said 175.902280 days and this file's
-- function said 175.860613: exactly 0.041667 of a day, one hour, which is the size of the shift
-- between the two offsets those two instants carry in that zone. The window cannot produce that number, because a
-- window over a naive column never leaves the naive domain: it is the parameter's type, not the
-- arithmetic, that decides.
--
-- The narrow reading is that the parameter is only a cutoff instant, so the safe rule is: nothing
-- in this directory converts. `001`'s bookkeeping keeps `TIMESTAMPTZ` (`build_run.built_at`,
-- `external_municipio.fetched_at`), because those columns record when *we* wrote a row and there
-- is no naive counterpart to be inconsistent with. What must stay naive is every comparison
-- against an `olist` timestamp.

-- How many orders this buyer had already had, strictly before the decision instant.
--
-- `p_customer_id` is an `olist.customers.customer_id` -- the one the frame carries -- but the
-- match is made through `customer_unique_id`, because `customer_id` is per order. Written this
-- way, the function is the specification of `003`'s window rather than a second opinion: same
-- key, same exclusion, one of them in a WHERE clause where a reader can check it.
CREATE OR REPLACE FUNCTION capstone.customer_order_volume_before(
    p_customer_id text,
    p_at timestamp
)
RETURNS bigint
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT COUNT(*)
    FROM olist.orders AS o
    JOIN olist.customers AS prior_buyer ON prior_buyer.customer_id = o.customer_id
    JOIN olist.customers AS this_buyer ON this_buyer.customer_id = p_customer_id
    WHERE this_buyer.customer_unique_id = prior_buyer.customer_unique_id
      -- the as-of condition, in the WHERE clause rather than in a window frame
      AND o.order_approved_at IS NOT NULL
      AND o.order_approved_at < p_at;
$$;

-- Days since that customer's previous approval, at the same instant. NULL when there is no
-- earlier order, which is reported as NULL and never as 0 (decisions.md item 7).
CREATE OR REPLACE FUNCTION capstone.customer_days_since_prior_order(
    p_customer_id text,
    p_at timestamp
)
RETURNS double precision
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- the cast is written out rather than left to RETURNS, so the two paths agree where the
    -- value is produced and not on the way out: EXTRACT gives numeric, and the frame's window
    -- column is `::float8` too (see 003). A declared return type is easy to keep while editing
    -- the body away from it; a cast in the expression is not.
    SELECT (EXTRACT(EPOCH FROM (p_at - MAX(o.order_approved_at))) / 86400.0)::float8
    FROM olist.orders AS o
    JOIN olist.customers AS prior_buyer ON prior_buyer.customer_id = o.customer_id
    JOIN olist.customers AS this_buyer ON this_buyer.customer_id = p_customer_id
    WHERE this_buyer.customer_unique_id = prior_buyer.customer_unique_id
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
    p_at timestamp
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
    p_at timestamp
)
RETURNS double precision
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- ::numeric inside and ::float8 outside: the cast on the way in is what makes the sum
    -- reproducible, the cast on the way out is what makes the type match the frame.
    SELECT AVG(i.price::numeric)::float8
    FROM olist.order_items AS i
    JOIN olist.orders AS o ON o.order_id = i.order_id
    WHERE i.seller_id = p_seller_id
      AND o.order_approved_at IS NOT NULL
      AND o.order_approved_at < p_at;
$$;

COMMENT ON FUNCTION capstone.customer_order_volume_before(text, timestamp) IS
    'the as-of specification behind v_feature_frame.prior_orders_by_customer, on '
    'customer_unique_id; slow on purpose, and compared to the window implementation by '
    'tests/test_capstone_sql_guards.py';
