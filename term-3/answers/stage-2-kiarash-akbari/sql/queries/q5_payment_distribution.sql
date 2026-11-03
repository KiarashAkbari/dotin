-- Candidate: read the materialized Q5 payment distribution snapshot.
-- Trade-off: results are as fresh as the last snapshot refresh.
SELECT payment_type,
       payment_installments,
       payment_records,
       unique_orders,
       paid_value,
       mean_payment_value,
       mean_order_value_payment_weighted,
       payment_record_share,
       installment_value_correlation,
       correlation_orders
FROM olist.mv_q5_payment_distribution
ORDER BY payment_type, payment_installments;
