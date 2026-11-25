-- Q5 payment type x installments distribution and its relation to order value.
-- Same grains as sql/baseline/q5: shares per payment record; correlation over
-- one row per order (max installments vs paid total).
-- Change: one WindowAgg over ix_payments_order_type_inst (already sorted by
-- order, type, installments) yields order totals, the first row of each order
-- and of each (order, type, installments) group; one GROUPING SETS pass then
-- computes the 27 groups and the total row. No Sort, no second scan.
-- CORR is double precision: its last digits depend on summation order, so
-- results are compared after rounding to 12 significant digits.
WITH scoped AS (
    SELECT p.order_id, p.payment_type, p.payment_installments, p.payment_value,
           SUM(p.payment_value) OVER w_order AS paid_total,
           MAX(p.payment_installments) OVER w_order AS max_installments,
           ROW_NUMBER() OVER w_order AS order_row,
           (LAG(p.payment_type) OVER w_order, LAG(p.payment_installments) OVER w_order)
               IS DISTINCT FROM (p.payment_type, p.payment_installments) AS first_in_group
    FROM olist.payments AS p
    WHERE NOT EXISTS (
        SELECT 1 FROM olist.orders AS x
        WHERE x.order_id = p.order_id
          AND x.order_status IN ('canceled', 'unavailable'))
    WINDOW w_order AS (
        PARTITION BY p.order_id ORDER BY p.payment_type, p.payment_installments
        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
), rolled AS (
    SELECT payment_type, payment_installments,
           GROUPING(payment_type) AS is_total,
           COUNT(*) AS payment_records,
           COUNT(*) FILTER (WHERE first_in_group) AS unique_orders,
           SUM(payment_value) AS paid_value,
           AVG(payment_value) AS mean_payment_value,
           AVG(paid_total) AS mean_order_value_payment_weighted,
           CORR(max_installments::double precision, paid_total::double precision)
               FILTER (WHERE order_row = 1) AS installment_value_correlation,
           COUNT(*) FILTER (WHERE order_row = 1) AS correlation_orders
    FROM scoped
    GROUP BY GROUPING SETS ((payment_type, payment_installments), ())
)
SELECT d.payment_type, d.payment_installments, d.payment_records, d.unique_orders,
       d.paid_value, d.mean_payment_value, d.mean_order_value_payment_weighted,
       d.payment_records::numeric / t.payment_records AS payment_record_share,
       t.installment_value_correlation, t.correlation_orders
FROM rolled AS d
JOIN rolled AS t ON t.is_total = 1
WHERE d.is_total = 0
ORDER BY d.payment_type, d.payment_installments
;
