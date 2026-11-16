-- 003_feature_views.sql
-- The feature frame: one row per order, at the moment of decision.
--
-- Three rules decide the shape of this file, and all three are visible in SQL rather than
-- in a notebook paragraph:
--
--   1. Only columns the audit allows, and only those available at `order_approved_at`.
--      `tests/test_capstone_sql_guards.py` reads this view's real catalog dependencies
--      (pg_depend) and runs them through `capstone.require_allowed`, so the claim is
--      checked against what the view actually reads, not against this comment.
--   2. Every history aggregate is as-of. `prior_orders_by_customer` and
--      `days_since_prior_order` see strictly earlier approvals and nothing else, because
--      the window frame is a RANGE on the timestamp and excludes same-instant peers. 004
--      keeps a slow, plainly-written version of the same two numbers as the
--      specification, and a test asserts the two agree row by row -- an optimisation that
--      drifted from its definition is exactly the bug that would otherwise reach the model.
--   3. Missing is not zero. A product with no category, an order whose seller ZIP has no
--      geolocation row, an order with no payments: each keeps an indicator column instead
--      of being folded into 0 (decisions.md item 7).
--
-- The label is deliberately absent. `order_reviews` appears nowhere in this file: it is
-- the answer sheet, and a frame that can see it does not need a leak in order to cheat.

-- The as-of history, in one pass over approved orders. D1 (`drop_before_training`) is
-- applied here, once, so no aggregate downstream can silently meet an unanchored row.
CREATE OR REPLACE VIEW capstone.v_customer_order_history AS
SELECT
    o.order_id,
    o.customer_id,
    o.order_approved_at,
    -- A RANGE, not ROWS: with `ROWS ... 1 PRECEDING` two orders approved in the same
    -- second would see each other, which is a same-instant leak. RANGE compares on the
    -- value, so same-instant peers are excluded and the count is exactly "strictly
    -- earlier approvals by this customer".
    COUNT(*) OVER (
        PARTITION BY o.customer_id
        ORDER BY o.order_approved_at
        RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL '1 microsecond' PRECEDING
    ) AS prior_orders_by_customer,
    EXTRACT(
        EPOCH FROM (
            o.order_approved_at
            - MAX(o.order_approved_at) OVER (
                  PARTITION BY o.customer_id
                  ORDER BY o.order_approved_at
                  RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL '1 microsecond' PRECEDING
              )
        )
    ) / 86400.0 AS days_since_prior_order
FROM olist.orders AS o
WHERE o.order_approved_at IS NOT NULL;

-- Centroid per ZIP prefix. The source carries many points per prefix (738,332 rows over
-- 19,015 prefixes), so "the" coordinate of a prefix is an aggregate; averaging is written
-- here so it is a stated choice and not an accident of a join fanning out.
CREATE OR REPLACE VIEW capstone.v_zip_centroid AS
SELECT
    g.geolocation_zip_code_prefix,
    AVG(g.geolocation_lat) AS lat,
    AVG(g.geolocation_lng) AS lng,
    COUNT(*) AS rows_behind_centroid
FROM olist.geolocation AS g
GROUP BY g.geolocation_zip_code_prefix;

-- Item aggregates at order grain. `primary_*` is the single most expensive item row of the
-- order, ties broken by order_item_id: one deterministic row, so the seller and product
-- features of an order cannot change between two runs of the same data.
CREATE OR REPLACE VIEW capstone.v_order_item_rollup AS
WITH agg AS (
    SELECT
        i.order_id,
        COUNT(*) AS n_items,
        SUM(i.price) AS price_sum,
        MAX(i.price) - MIN(i.price) AS price_spread,
        SUM(i.freight_value) AS freight_sum,
        CASE
            WHEN SUM(i.price + i.freight_value) > 0
            THEN SUM(i.freight_value) / SUM(i.price + i.freight_value)
        END AS freight_share,
        COUNT(DISTINCT i.seller_id) AS n_sellers,
        COUNT(DISTINCT i.product_id) AS n_products,
        MIN(i.shipping_limit_date) AS first_shipping_limit_date
    FROM olist.order_items AS i
    GROUP BY i.order_id
), primary_row AS (
    SELECT DISTINCT ON (i.order_id)
        i.order_id,
        i.seller_id AS primary_seller_id,
        i.product_id AS primary_product_id
    FROM olist.order_items AS i
    ORDER BY i.order_id, (i.price + i.freight_value) DESC, i.order_item_id
)
SELECT
    agg.order_id,
    agg.n_items,
    agg.price_sum,
    agg.price_spread,
    agg.freight_sum,
    agg.freight_share,
    agg.n_sellers,
    agg.n_products,
    EXTRACT(EPOCH FROM (agg.first_shipping_limit_date - a.order_approved_at)) / 86400.0
        AS shipping_limit_days,
    primary_row.primary_seller_id,
    primary_row.primary_product_id
FROM agg
JOIN primary_row USING (order_id)
JOIN capstone.v_decision_moment AS a ON a.order_id = agg.order_id;

-- Payment aggregates at order grain. `payment_sequential` is the position within a split,
-- so MAX of it is "how many parts this order was cut into", which is what the capacity
-- question actually asks.
CREATE OR REPLACE VIEW capstone.v_order_payment_rollup AS
SELECT
    pay.order_id,
    COUNT(*) AS n_payments,
    SUM(pay.payment_value) AS payment_sum,
    MAX(pay.payment_installments) AS installments_max,
    MAX(pay.payment_sequential) AS payment_parts,
    BOOL_OR(pay.payment_type = 'credit_card') AS pays_credit_card,
    BOOL_OR(pay.payment_type = 'voucher') AS pays_voucher
FROM olist.payments AS pay
GROUP BY pay.order_id;

-- The frame itself: one row per approved order, 29 feature columns beside the three
-- technical keys (order_id, customer_id, order_approved_at), and no column the audit does
-- not allow. It names five ``olist`` tables directly -- orders, customers, sellers, products,
-- product_category_name_translation -- and reaches eight in total, because the item, payment
-- and geography aggregates arrive through this directory's own views. The live test counts the
-- dependency closure in ``pg_depend`` rather than trusting this sentence.
CREATE OR REPLACE VIEW capstone.v_feature_frame AS
SELECT
    a.order_id,
    a.customer_id,
    a.order_approved_at,

    -- what this customer had already done, as of this instant
    COALESCE(h.prior_orders_by_customer, 0) AS prior_orders_by_customer,
    h.days_since_prior_order,

    -- what was bought, and what was promised for it
    COALESCE(i.n_items, 0) AS n_items,
    i.price_sum,
    i.price_spread,
    COALESCE(i.freight_sum, 0) AS freight_sum,
    i.freight_share,
    i.shipping_limit_days,
    COALESCE(i.n_sellers, 0) AS n_sellers,
    COALESCE(i.n_products, 0) AS n_products,
    EXTRACT(EPOCH FROM (o.order_estimated_delivery_date - o.order_purchase_timestamp))
        / 86400.0 AS promise_days,
    EXTRACT(DAY FROM o.order_approved_at)::int AS approval_day_of_month,
    EXTRACT(HOUR FROM o.order_approved_at)::int AS approval_hour,

    -- how it was paid for
    COALESCE(p.n_payments, 0) AS n_payments,
    p.payment_sum,
    p.installments_max,
    p.payment_parts,
    COALESCE(p.pays_credit_card, FALSE) AS pays_credit_card,
    COALESCE(p.pays_voucher, FALSE) AS pays_voucher,

    -- where it goes, and who sends it
    c.customer_state,
    (c.customer_zip_code_prefix::int / 1000) AS customer_zip_region,
    ((cust_geo.lat - seller_geo.lat) * (cust_geo.lat - seller_geo.lat)
        + (cust_geo.lng - seller_geo.lng) * (cust_geo.lng - seller_geo.lng))
        AS customer_seller_span_sq,
    s.seller_state,

    -- what the catalogue says about that item -- including whether it says nothing
    t.product_category_name_english,
    pr.product_weight_g,
    pr.product_photos_qty,

    -- the three "not available" flags of decisions.md item 7: a missing row is a fact
    -- about the data, and it is reported as one
    (p.order_id IS NULL) AS payments_unavailable,
    (cust_geo.lat IS NULL OR seller_geo.lat IS NULL) AS geography_unavailable,
    (t.product_category_name_english IS NULL) AS category_unavailable
FROM capstone.v_decision_moment AS a
JOIN olist.orders AS o ON o.order_id = a.order_id
LEFT JOIN capstone.v_customer_order_history AS h ON h.order_id = a.order_id
LEFT JOIN capstone.v_order_item_rollup AS i ON i.order_id = a.order_id
LEFT JOIN capstone.v_order_payment_rollup AS p ON p.order_id = a.order_id
LEFT JOIN olist.customers AS c ON c.customer_id = a.customer_id
LEFT JOIN capstone.v_zip_centroid AS cust_geo
       ON cust_geo.geolocation_zip_code_prefix = c.customer_zip_code_prefix
LEFT JOIN olist.sellers AS s ON s.seller_id = i.primary_seller_id
LEFT JOIN capstone.v_zip_centroid AS seller_geo
       ON seller_geo.geolocation_zip_code_prefix = s.seller_zip_code_prefix
LEFT JOIN olist.products AS pr ON pr.product_id = i.primary_product_id
LEFT JOIN olist.product_category_name_translation AS t
       ON t.product_category_name = pr.product_category_name
WHERE a.has_anchor;

COMMENT ON VIEW capstone.v_feature_frame IS
    'stage-4 feature frame: one row per approved order, as of order_approved_at; audited '
    'columns only; the label lives in capstone.v_order_label and never here';

-- The label, in its own view, so no query that wants features can inherit it by accident.
-- D3 (`worst_score_wins`) is the MIN; D2 (`separate_population`) is `has_review`, kept as
-- a column rather than as a filter so the excluded share stays countable.
CREATE OR REPLACE VIEW capstone.v_order_label AS
WITH per_order AS (
    SELECT
        rev.order_id,
        COUNT(*) AS review_count,
        MIN(rev.review_score) AS worst_score
    FROM olist.order_reviews AS rev
    GROUP BY rev.order_id
)
SELECT
    f.order_id,
    (r.order_id IS NOT NULL) AS has_review,
    COALESCE(r.review_count, 0) AS review_count,
    r.worst_score,
    CASE WHEN r.order_id IS NULL THEN NULL ELSE r.worst_score <= 2 END AS is_negative
FROM capstone.v_feature_frame AS f
LEFT JOIN per_order AS r ON r.order_id = f.order_id;

-- D2's population, named, so the report quotes a count and not a sentence. It spans every
-- order (768), and `also_excluded_by_d1` marks the 5 that D1 already removes -- which is
-- why "D1 first, then D2" leaves 763 rather than 768 (decisions.md item 1).
CREATE OR REPLACE VIEW capstone.v_d2_unlabelled_orders AS
SELECT
    o.order_id,
    o.order_status,
    (o.order_approved_at IS NULL) AS also_excluded_by_d1
FROM olist.orders AS o
LEFT JOIN (SELECT DISTINCT order_id FROM olist.order_reviews) AS r USING (order_id)
WHERE r.order_id IS NULL;
