-- Materialized snapshot for Q4 seller dashboard.
-- Order metrics are computed once per seller/order; revenue is computed
-- separately from item rows so multi-item orders are not overcounted.
DROP MATERIALIZED VIEW IF EXISTS olist.mv_q4_seller_dashboard;

CREATE MATERIALIZED VIEW olist.mv_q4_seller_dashboard AS
WITH seller_orders AS (
    SELECT DISTINCT
        i.seller_id,
        o.order_id,
        o.order_approved_at AS approved_at,
        o.order_delivered_carrier_date AS carrier_at
    FROM olist.order_items AS i
    JOIN olist.orders AS o ON o.order_id = i.order_id
    WHERE o.order_status NOT IN ('canceled', 'unavailable')
), reviews AS (
    SELECT r.order_id,
           AVG(r.review_score::numeric) AS score
    FROM olist.order_reviews AS r
    WHERE EXISTS (
        SELECT 1
        FROM seller_orders AS s
        WHERE s.order_id = r.order_id
    )
    GROUP BY r.order_id
), order_metrics AS (
    SELECT s.seller_id,
           s.order_id,
           s.approved_at,
           s.carrier_at,
           AVG(r.score) AS score
    FROM seller_orders AS s
    LEFT JOIN reviews AS r
      ON r.order_id = s.order_id
    GROUP BY s.seller_id, s.order_id, s.approved_at, s.carrier_at
), seller_revenue AS (
    SELECT s.seller_id,
           SUM(i.price + i.freight_value) AS revenue
    FROM olist.order_items AS i
    JOIN seller_orders AS s
      ON s.seller_id = i.seller_id
     AND s.order_id = i.order_id
    GROUP BY s.seller_id
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
SELECT r.seller_id,
       r.revenue,
       s.seller_orders,
       s.mean_preparation_days,
       s.preparation_sample_orders,
       s.mean_review_score,
       s.reviewed_orders
FROM seller_revenue AS r
JOIN seller_stats AS s
  ON s.seller_id = r.seller_id;

CREATE UNIQUE INDEX ux_mv_q4_seller_dashboard_seller
ON olist.mv_q4_seller_dashboard (seller_id);
