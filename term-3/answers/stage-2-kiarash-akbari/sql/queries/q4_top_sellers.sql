-- Q4 top 20 sellers by revenue (price + freight), with mean approval-to-carrier
-- days and mean per-order review score. Same rules as sql/baseline/q4.
-- Change: rank sellers first (revenue only, NOT EXISTS against the tiny
-- ix_orders_inactive), then read only the top 20 sellers' orders through
-- ix_order_items_seller_order + ix_orders_dispatch_cover and their reviews
-- through ix_order_reviews_order_score, instead of computing every seller.
-- Assumption kept from the baseline: an order's review counts for each seller
-- in that order; freight is part of seller revenue.
WITH top_sellers AS MATERIALIZED (
    SELECT i.seller_id, SUM(i.price + i.freight_value) AS revenue
    FROM olist.order_items AS i
    WHERE NOT EXISTS (
        SELECT 1 FROM olist.orders AS x
        WHERE x.order_id = i.order_id
          AND x.order_status IN ('canceled', 'unavailable'))
    GROUP BY i.seller_id
    ORDER BY revenue DESC, i.seller_id
    LIMIT 20
), seller_orders AS (
    SELECT i.seller_id, i.order_id,
           MAX(o.order_approved_at) AS approved_at,
           MAX(o.order_delivered_carrier_date) AS carrier_at
    FROM top_sellers AS t
    JOIN olist.order_items AS i ON i.seller_id = t.seller_id
    JOIN olist.orders AS o ON o.order_id = i.order_id
    WHERE o.order_status NOT IN ('canceled', 'unavailable')
    GROUP BY i.seller_id, i.order_id
)
SELECT t.seller_id, t.revenue, COUNT(*) AS seller_orders,
       AVG(EXTRACT(EPOCH FROM (s.carrier_at - s.approved_at)) / 86400.0)
           FILTER (WHERE s.carrier_at >= s.approved_at) AS mean_preparation_days,
       COUNT(*) FILTER (WHERE s.carrier_at >= s.approved_at) AS preparation_sample_orders,
       AVG(r.score) AS mean_review_score, COUNT(r.score) AS reviewed_orders
FROM top_sellers AS t
JOIN seller_orders AS s ON s.seller_id = t.seller_id
CROSS JOIN LATERAL (
    SELECT AVG(v.review_score::numeric) AS score
    FROM olist.order_reviews AS v
    WHERE v.order_id = s.order_id
) AS r
GROUP BY t.seller_id, t.revenue
ORDER BY t.revenue DESC, t.seller_id
;
