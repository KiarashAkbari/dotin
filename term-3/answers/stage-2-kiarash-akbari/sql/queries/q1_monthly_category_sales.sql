-- Purchase month; revenue includes freight, excludes canceled/unavailable.
-- Each multi-category order counts once in each category; NULL is retained.
SELECT date_trunc('month', order_purchase_timestamp)::date AS purchase_month,
       product_category_name, SUM(item_value) AS sales_value,
       COUNT(DISTINCT order_id) AS unique_orders,
       SUM(item_value) / COUNT(DISTINCT order_id) AS average_order_value
FROM olist.v_order_item_sales
GROUP BY 1, 2 ORDER BY 1, 2 NULLS LAST;
