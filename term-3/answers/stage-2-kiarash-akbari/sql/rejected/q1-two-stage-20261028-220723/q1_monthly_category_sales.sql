-- Candidate: aggregate to order grain first, removing the 112k-row sort
-- previously required by COUNT(DISTINCT order_id).
WITH item_sales AS (
    SELECT date_trunc('month', o.order_purchase_timestamp)::date
               AS purchase_month,
           p.product_category_name,
           o.order_id,
           SUM(i.price + i.freight_value) AS sales_value
    FROM olist.order_items AS i
    JOIN olist.orders AS o
      ON o.order_id = i.order_id
    JOIN olist.products AS p
      ON p.product_id = i.product_id
    WHERE o.order_status NOT IN ('canceled', 'unavailable')
    GROUP BY 1, 2, 3
)
SELECT purchase_month,
       product_category_name,
       SUM(sales_value) AS sales_value,
       COUNT(*) AS unique_orders,
       SUM(sales_value) / COUNT(*) AS average_order_value
FROM item_sales
GROUP BY purchase_month, product_category_name
ORDER BY purchase_month, product_category_name NULLS LAST;
