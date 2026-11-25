-- Q3 delivery delay vs review score. Same buckets, weights and NULL rules as
-- sql/baseline/q3: one equal-weight row per reviewed delivered order.
-- Change: the per-order review average reads ix_order_reviews_order_score in
-- order_id order (Index Only Scan, ORDER BY in the subquery), so the
-- GroupAggregate needs no Sort and no heap access.
WITH reviews AS (
    SELECT order_id, AVG(review_score::numeric) AS score
    FROM (SELECT order_id, review_score FROM olist.order_reviews ORDER BY order_id) AS ordered
    GROUP BY order_id
), scored AS (
    SELECT olist.delivery_delay_days(d.order_delivered_customer_date,
               d.order_estimated_delivery_date) AS delay_days, r.score
    FROM olist.v_delivered_orders AS d
    JOIN reviews AS r ON r.order_id = d.order_id
), grouped AS (
    SELECT CASE WHEN delay_days <= 0 THEN 0 WHEN delay_days <= 7 THEN 1
                WHEN delay_days <= 14 THEN 2 ELSE 3 END AS bucket, score
    FROM scored
)
SELECT bucket,
       CASE bucket WHEN 0 THEN 'On time or early' WHEN 1 THEN '1-7 days late'
                   WHEN 2 THEN '8-14 days late' ELSE '15+ days late' END AS delivery_group,
       COUNT(*) AS reviewed_orders, AVG(score) AS mean_review_score,
       AVG((score <= 2)::integer)::numeric AS low_score_order_ratio
FROM grouped GROUP BY bucket ORDER BY bucket
;
