-- Q1 monthly category sales: hybrid of a snapshot and a live tail.
-- Grain and rules (unchanged from sql/baseline/q1): purchase month x Portuguese
-- category; revenue = price + freight; canceled/unavailable excluded; NULL
-- category kept; a multi-category order counts once in each category.
--
-- Why not an index: the baseline aggregates all 112k order lines on every
-- read (Sort + GroupAggregate for COUNT(DISTINCT)); every index leaves that
-- work in the plan. Closed months come from olist.monthly_category_snapshot
-- (built by olist.refresh_monthly_category_snapshot[_range]); only months after
-- the newest snapshot month are aggregated live, through ix_orders_purchase_ts.
-- With an empty snapshot the query degrades to the full live baseline, never
-- to a wrong answer. Price: closed months are as fresh as their last refresh.
WITH coverage AS (
    SELECT (MAX(month_start) + INTERVAL '1 month')::timestamp AS live_from
    FROM olist.monthly_category_snapshot
), combined AS (
    SELECT s.month_start AS purchase_month, s.product_category_name,
           s.sales_value, s.order_count AS unique_orders, s.average_order_value
    FROM olist.monthly_category_snapshot AS s
    UNION ALL
    SELECT date_trunc('month', v.order_purchase_timestamp)::date,
           v.product_category_name, SUM(v.item_value),
           COUNT(DISTINCT v.order_id),
           SUM(v.item_value) / COUNT(DISTINCT v.order_id)
    FROM olist.v_order_item_sales AS v
    WHERE v.order_purchase_timestamp >= COALESCE(
        (SELECT live_from FROM coverage), '-infinity'::timestamp)
    GROUP BY 1, 2
)
SELECT purchase_month, product_category_name, sales_value, unique_orders, average_order_value
FROM combined
ORDER BY 1, 2 NULLS LAST
;
