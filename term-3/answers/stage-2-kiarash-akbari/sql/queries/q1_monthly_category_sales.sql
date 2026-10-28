-- Candidate: read the materialized Q1 monthly category sales snapshot.
-- Trade-off: results are as fresh as the last snapshot refresh.
SELECT purchase_month,
       product_category_name,
       sales_value,
       unique_orders,
       average_order_value
FROM olist.mv_q1_monthly_category_sales
ORDER BY purchase_month, product_category_name NULLS LAST;
