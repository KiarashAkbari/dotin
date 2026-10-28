-- Candidate: replace the 102k-row sort used for COUNT(DISTINCT order_id)
-- with two hash aggregations: order grain, then payment-group grain.
WITH scoped AS (
    SELECT p.*
    FROM olist.payments AS p
    JOIN olist.orders AS o ON o.order_id = p.order_id
    WHERE o.order_status NOT IN ('canceled', 'unavailable')
), totals AS (
    SELECT order_id,
           SUM(payment_value) AS paid_total,
           MAX(payment_installments) AS max_installments
    FROM scoped
    GROUP BY order_id
), order_groups AS (
    SELECT p.payment_type,
           p.payment_installments,
           p.order_id,
           COUNT(*) AS payment_records,
           SUM(p.payment_value) AS paid_value,
           t.paid_total
    FROM scoped AS p
    JOIN totals AS t ON t.order_id = p.order_id
    GROUP BY p.payment_type, p.payment_installments, p.order_id, t.paid_total
), distribution AS (
    SELECT payment_type,
           payment_installments,
           SUM(payment_records)::bigint AS payment_records,
           COUNT(*) AS unique_orders,
           SUM(paid_value) AS paid_value,
           SUM(paid_value) / SUM(payment_records)
               AS mean_payment_value,
           SUM(paid_total * payment_records) / SUM(payment_records)
               AS mean_order_value_payment_weighted
    FROM order_groups
    GROUP BY payment_type, payment_installments
), relationship AS (
    SELECT CORR(max_installments::double precision,
                paid_total::double precision)
               AS installment_value_correlation,
           COUNT(*) AS correlation_orders
    FROM totals
)
SELECT d.*,
       d.payment_records::numeric
           / SUM(d.payment_records) OVER () AS payment_record_share,
       r.installment_value_correlation,
       r.correlation_orders
FROM distribution AS d
CROSS JOIN relationship AS r
ORDER BY d.payment_type, d.payment_installments;
