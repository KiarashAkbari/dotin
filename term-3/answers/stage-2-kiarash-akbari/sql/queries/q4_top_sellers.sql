-- Preparation is approval-to-carrier, an order-level proxy for seller time.
-- Negative/missing durations excluded from duration only; freight is revenue.
WITH seller_orders AS (
    SELECT seller_id, order_id, SUM(item_value) AS revenue,
           MAX(order_approved_at) AS approved_at,
           MAX(order_delivered_carrier_date) AS carrier_at
    FROM olist.v_order_item_sales GROUP BY seller_id, order_id
), reviews AS (
    SELECT order_id, AVG(review_score::numeric) AS score
    FROM olist.order_reviews GROUP BY order_id
)
SELECT s.seller_id, SUM(s.revenue) AS revenue, COUNT(*) AS seller_orders,
       AVG(EXTRACT(EPOCH FROM (s.carrier_at - s.approved_at)) / 86400.0)
           FILTER (WHERE s.carrier_at >= s.approved_at) AS mean_preparation_days,
       COUNT(*) FILTER (WHERE s.carrier_at >= s.approved_at) AS preparation_sample_orders,
       AVG(r.score) AS mean_review_score, COUNT(r.score) AS reviewed_orders
FROM seller_orders AS s LEFT JOIN reviews AS r ON r.order_id = s.order_id
GROUP BY s.seller_id ORDER BY revenue DESC, s.seller_id LIMIT 20;
