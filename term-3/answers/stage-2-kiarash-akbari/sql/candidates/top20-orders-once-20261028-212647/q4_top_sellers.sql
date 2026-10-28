
-- Candidate: rank sellers by revenue before computing ancillary statistics.
-- Product join omitted because the schema's item-to-product FK guarantees
-- an existing product and product attributes do not affect this analysis.
WITH active_orders AS MATERIALIZED (
    SELECT order_id, order_status,
           order_approved_at, order_delivered_carrier_date
    FROM olist.orders
    WHERE order_status NOT IN ('canceled', 'unavailable')
), top_sellers AS MATERIALIZED (
    SELECT i.seller_id,
           SUM(i.price + i.freight_value) AS revenue
    FROM olist.order_items AS i
    JOIN active_orders AS o ON o.order_id = i.order_id
    WHERE o.order_status NOT IN ('canceled', 'unavailable')
    GROUP BY i.seller_id
    ORDER BY revenue DESC, i.seller_id
    LIMIT 20
), seller_orders AS MATERIALIZED (
    SELECT DISTINCT i.seller_id, o.order_id,
           o.order_approved_at AS approved_at,
           o.order_delivered_carrier_date AS carrier_at
    FROM olist.order_items AS i
    JOIN top_sellers AS t ON t.seller_id = i.seller_id
    JOIN active_orders AS o ON o.order_id = i.order_id
    WHERE o.order_status NOT IN ('canceled', 'unavailable')
), reviews AS (
    SELECT r.order_id, AVG(r.review_score::numeric) AS score
    FROM olist.order_reviews AS r
    WHERE EXISTS (
        SELECT 1 FROM seller_orders AS s WHERE s.order_id = r.order_id
    )
    GROUP BY r.order_id
), seller_stats AS (
    SELECT s.seller_id, COUNT(*) AS seller_orders,
           AVG(EXTRACT(EPOCH FROM (s.carrier_at - s.approved_at)) / 86400.0)
               FILTER (WHERE s.carrier_at >= s.approved_at)
               AS mean_preparation_days,
           COUNT(*) FILTER (WHERE s.carrier_at >= s.approved_at)
               AS preparation_sample_orders,
           AVG(r.score) AS mean_review_score,
           COUNT(r.score) AS reviewed_orders
    FROM seller_orders AS s
    LEFT JOIN reviews AS r ON r.order_id = s.order_id
    GROUP BY s.seller_id
)
SELECT t.seller_id, t.revenue, s.seller_orders,
       s.mean_preparation_days, s.preparation_sample_orders,
       s.mean_review_score, s.reviewed_orders
FROM top_sellers AS t
JOIN seller_stats AS s ON s.seller_id = t.seller_id
ORDER BY t.revenue DESC, t.seller_id;
