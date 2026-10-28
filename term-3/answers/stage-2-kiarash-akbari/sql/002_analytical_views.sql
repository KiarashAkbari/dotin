-- Apply after 001_create_olist_schema.sql.
-- These are live views, not cached or materialized results.
-- Delivery uses completed orders with both timestamps available.
CREATE OR REPLACE VIEW olist.v_delivered_orders AS
SELECT order_id, customer_id, order_purchase_timestamp,
       order_approved_at, order_delivered_carrier_date,
       order_delivered_customer_date, order_estimated_delivery_date
FROM olist.orders
WHERE order_status = 'delivered'
  AND order_delivered_customer_date IS NOT NULL
  AND order_estimated_delivery_date IS NOT NULL;

-- Grain: one row per order line. Review and payment tables are deliberately
-- absent: joining their one-to-many rows here would multiply item revenue.
-- Unknown category remains NULL; the monthly snapshot retains that group.
-- Sales exclude canceled and unavailable orders, but include other statuses.
CREATE OR REPLACE VIEW olist.v_order_item_sales AS
SELECT o.order_id, o.order_purchase_timestamp,
       o.order_approved_at, o.order_delivered_carrier_date,
       i.order_item_id, i.seller_id, i.product_id,
       p.product_category_name,
       t.product_category_name_english,
       i.price, i.freight_value,
       i.price + i.freight_value AS item_value
FROM olist.orders AS o
JOIN olist.order_items AS i ON i.order_id = o.order_id
JOIN olist.products AS p ON p.product_id = i.product_id
LEFT JOIN olist.product_category_name_translation AS t
  ON t.product_category_name = p.product_category_name
WHERE o.order_status NOT IN ('canceled', 'unavailable');
