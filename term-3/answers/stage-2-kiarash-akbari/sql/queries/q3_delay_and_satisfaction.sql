-- Candidate: read the materialized Q3 delay/satisfaction snapshot.
-- Trade-off: results are as fresh as the last snapshot refresh.
SELECT bucket,
       delivery_group,
       reviewed_orders,
       mean_review_score,
       low_score_order_ratio
FROM olist.mv_q3_delay_satisfaction
ORDER BY bucket;
