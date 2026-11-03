-- Candidate: read the materialized Q4 seller snapshot and rank the top 20.
-- Trade-off: results are as fresh as the last snapshot refresh.
SELECT seller_id,
       revenue,
       seller_orders,
       mean_preparation_days,
       preparation_sample_orders,
       mean_review_score,
       reviewed_orders
FROM olist.mv_q4_seller_dashboard
ORDER BY revenue DESC, seller_id
LIMIT 20;
