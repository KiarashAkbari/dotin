-- Denominator: observed delivered orders; calendar-day delay; purchase month.
SELECT date_trunc('month', d.order_purchase_timestamp)::date AS purchase_month,
       c.customer_state, COUNT(*) AS delivered_orders,
       COUNT(*) FILTER (WHERE olist.delivery_delay_days(
           d.order_delivered_customer_date, d.order_estimated_delivery_date) > 0) AS late_orders,
       AVG((olist.delivery_delay_days(d.order_delivered_customer_date,
           d.order_estimated_delivery_date) > 0)::integer)::numeric AS late_ratio
FROM olist.v_delivered_orders AS d
JOIN olist.customers AS c ON c.customer_id = d.customer_id
GROUP BY 1, 2 ORDER BY 1, 2;
