-- Candidate: join reviews to delivered orders before per-order aggregation.
-- This avoids aggregating reviews for orders that Q3 excludes.
-- AVG over integer already returns numeric, so the extra cast is removed.
WITH scored AS (
    SELECT olist.delivery_delay_days(d.order_delivered_customer_date,
               d.order_estimated_delivery_date) AS delay_days,
           AVG(r.review_score::numeric) AS score
    FROM olist.v_delivered_orders AS d
    JOIN olist.order_reviews AS r
      ON r.order_id = d.order_id
    GROUP BY d.order_id,
             d.order_delivered_customer_date,
             d.order_estimated_delivery_date
), grouped AS (
    SELECT CASE
               WHEN delay_days <= 0 THEN 0
               WHEN delay_days <= 7 THEN 1
               WHEN delay_days <= 14 THEN 2
               ELSE 3
           END AS bucket,
           score
    FROM scored
)
SELECT bucket,
       CASE bucket
           WHEN 0 THEN 'On time or early'
           WHEN 1 THEN '1-7 days late'
           WHEN 2 THEN '8-14 days late'
           ELSE '15+ days late'
       END AS delivery_group,
       COUNT(*) AS reviewed_orders,
       AVG(score) AS mean_review_score,
       AVG((score <= 2)::integer) AS low_score_order_ratio
FROM grouped
GROUP BY bucket
ORDER BY bucket;
