-- Candidate: compute order-level review averages in the same seller-orders pass.
-- This removes the separate seller_orders scan previously used by the reviews
-- EXISTS filter, while retaining per-order review averaging.
WITH top_sellers AS MATERIALIZED (
    SELECT i.seller_id,
           SUM(i.price + i.freight_value) AS revenue
    FROM olist.order_items AS i
    JOIN olist.orders AS o ON o.order_id = i.order_id
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
    JOIN olist.orders AS o ON o.order_id = i.order_id
    WHERE o.order_status NOT IN ('canceled', 'unavailable')
), order_metrics AS (
    SELECT s.seller_id,
           s.order_id,
           s.approved_at,
           s.carrier_at,
           AVG(r.review_score::numeric) AS score
    FROM seller_orders AS s
    LEFT JOIN olist.order_reviews AS r
      ON r.order_id = s.order_id
    GROUP BY s.seller_id, s.order_id, s.approved_at, s.carrier_at
), seller_stats AS (
    SELECT seller_id,
           COUNT(*) AS seller_orders,
           AVG(EXTRACT(EPOCH FROM (carrier_at - approved_at)) / 86400.0)
               FILTER (WHERE carrier_at >= approved_at)
               AS mean_preparation_days,
           COUNT(*) FILTER (WHERE carrier_at >= approved_at)
               AS preparation_sample_orders,
           AVG(score) AS mean_review_score,
           COUNT(score) AS reviewed_orders
    FROM order_metrics
    GROUP BY seller_id
)
SELECT t.seller_id, t.revenue, s.seller_orders,
       s.mean_preparation_days, s.preparation_sample_orders,
       s.mean_review_score, s.reviewed_orders
FROM top_sellers AS t
JOIN seller_stats AS s ON s.seller_id = t.seller_id
ORDER BY t.revenue DESC, t.seller_id;
