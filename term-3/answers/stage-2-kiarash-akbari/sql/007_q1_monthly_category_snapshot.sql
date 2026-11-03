-- Materialized snapshot for Q1 monthly category sales.
-- Stores one row per purchase month and product category, including NULL
-- categories. Revenue includes freight and excludes canceled/unavailable.
DROP MATERIALIZED VIEW IF EXISTS olist.mv_q1_monthly_category_sales;

CREATE MATERIALIZED VIEW olist.mv_q1_monthly_category_sales AS
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
GROUP BY purchase_month, product_category_name;

CREATE UNIQUE INDEX ux_mv_q1_monthly_category_sales
ON olist.mv_q1_monthly_category_sales (
    purchase_month,
    COALESCE(product_category_name, '')
);
