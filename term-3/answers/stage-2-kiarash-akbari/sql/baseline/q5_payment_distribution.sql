-- Shares use payment-record grain; split orders can appear in multiple groups.
-- Correlation separately uses one row/order: max installments vs paid total.
-- Order-value average in distribution is explicitly payment-record weighted.
WITH scoped AS (
    SELECT p.* FROM olist.payments AS p
    JOIN olist.orders AS o ON o.order_id = p.order_id
    WHERE o.order_status NOT IN ('canceled', 'unavailable')
), totals AS (
    SELECT order_id, SUM(payment_value) AS paid_total,
           MAX(payment_installments) AS max_installments
    FROM scoped GROUP BY order_id
), relationship AS (
    SELECT CORR(max_installments::double precision, paid_total::double precision)
               AS installment_value_correlation, COUNT(*) AS correlation_orders
    FROM totals
), distribution AS (
    SELECT p.payment_type, p.payment_installments, COUNT(*) AS payment_records,
           COUNT(DISTINCT p.order_id) AS unique_orders,
           SUM(p.payment_value) AS paid_value,
           AVG(p.payment_value) AS mean_payment_value,
           AVG(t.paid_total) AS mean_order_value_payment_weighted
    FROM scoped AS p JOIN totals AS t ON t.order_id = p.order_id
    GROUP BY p.payment_type, p.payment_installments
)
SELECT d.*, d.payment_records::numeric / SUM(d.payment_records) OVER () AS payment_record_share,
       r.installment_value_correlation, r.correlation_orders
FROM distribution AS d CROSS JOIN relationship AS r
ORDER BY d.payment_type, d.payment_installments;
