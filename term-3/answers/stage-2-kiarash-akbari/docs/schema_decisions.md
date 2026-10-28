# Schema decisions

## Scope

- Source: Olist Brazilian E-commerce CSV datasets.
- Target: PostgreSQL.
- Raw records are preserved; records that violate load contracts are classified before loading.
- Every decision must remain traceable to profiling outputs in `data/curated/`.
- This document is a draft schema contract; it does not yet define indexes.

## Global decisions

### Value basis

- Options: item-based value or payment-based value.
- Decision: item-based value.
- Formula: `item_value = price + freight_value`.
- Evidence: 98,665 orders have both items and payments; 98,287 match within 0.01; 378 differ.
- Trade-off: payment adjustments are not reflected in item-based sales value.

### Reviews granularity

- Options: preserve all review rows or reduce to one row per order.
- Decision: preserve all review rows.
- Evidence: 99,224 review rows; 98,410 distinct review IDs; 547 orders have multiple reviews.
- Trade-off: order-level review metrics require an explicit aggregation rule.

### Geolocation granularity

- Options: treat ZIP prefix as unique or preserve multiple geolocation rows.
- Decision: preserve multiple geolocation rows.
- Evidence: 17,972 ZIP prefixes have multiple rows; 8,556 have multiple city labels; 8 have multiple state labels.
- Trade-off: customer and seller geolocation resolution requires an explicit selection policy.

### Record disposition

- Records are not silently deleted.
- A record either passes its table contract, is loaded with an allowed nullable field, or is quarantined with a reason code.
- Foreign-key violations are counted and reported before loading.

## Table contracts

### customers

- Grain: one row per customer account record.
- Primary key: `customer_id`.
- Nullable columns: none.
- Foreign keys: none.
- Constraints:
  - `customer_id` is unique and not null.
  - `customer_unique_id` is not null.
  - `customer_zip_code_prefix`, `customer_city`, and `customer_state` are not null.
- Special cases:
  - `customer_unique_id` is not unique; it identifies a person or recurring customer, not a customer-account row.
  - It must not be used as the primary key.

### orders

- Grain: one row per order.
- Primary key: `order_id`.
- Nullable columns:
  - `order_approved_at`
  - `order_delivered_carrier_date`
  - `order_delivered_customer_date`
- Foreign keys:
  - `customer_id` references `customers.customer_id`.
- Constraints:
  - `order_id` is unique and not null.
  - `customer_id` is not null.
  - `order_status` is not null.
  - `order_purchase_timestamp` is not null.
  - `order_estimated_delivery_date` is not null.
- Special cases:
  - Missing delivery timestamps are valid for orders that have not been delivered.
  - Eight orders with status `delivered` have no customer delivery timestamp; they are retained and reported as data-quality exceptions.
  - Six non-delivered orders have a customer delivery timestamp; they are retained and reported as data-quality exceptions.

### order_items

- Grain: one row per order line item.
- Primary key: `(order_id, order_item_id)`.
- Nullable columns: none.
- Foreign keys:
  - `order_id` references `orders.order_id`.
  - `product_id` references `products.product_id`.
  - `seller_id` references `sellers.seller_id`.
- Constraints:
  - `order_id`, `order_item_id`, `product_id`, and `seller_id` are not null.
  - `price` is not null and greater than or equal to zero.
  - `freight_value` is not null and greater than or equal to zero.
- Derived value:
  - `item_value = price + freight_value`.
- Special cases:
  - No null, negative, or zero-value item rows were observed in the current source.

### payments

- Grain: one row per payment record.
- Primary key: `(order_id, payment_sequential)`.
- Nullable columns: none.
- Foreign keys:
  - `order_id` references `orders.order_id`.
- Constraints:
  - `order_id` and `payment_sequential` are not null.
  - `payment_type` is not null.
  - `payment_installments` is not null and greater than or equal to zero.
  - `payment_value` is not null and greater than or equal to zero.
- Special cases:
  - Nine payment rows have zero value.
  - Two payment rows have zero installments.
  - These rows are retained because they do not violate referential integrity or non-negativity.
  - Payment total per order is the sum of all payment rows for that order.

### order_reviews

- Grain: one review-to-order assignment.
- Primary key: `(review_id, order_id)`.
- Foreign keys:
  - `order_id` references `orders.order_id`.
- Constraints:
  - `review_id` and `order_id` are not null.
  - The pair `(review_id, order_id)` is unique.
  - `review_score` is not null and between 1 and 5.
- Evidence:
  - 99,224 review rows.
  - 99,224 distinct `(review_id, order_id)` pairs.
  - 0 duplicate `(review_id, order_id)` rows.
  - 789 `review_id` values are reused across different orders.
- Decision:
  - `review_id` alone is not a primary key.
  - The composite key preserves every review-to-order assignment.



### products

- Grain: one row per product.
- Primary key: `product_id`.
- Nullable columns:
  - `product_category_name`
  - `product_name_length`
  - `product_description_length`
  - `product_photos_qty`
  - `product_weight_g`
  - `product_length_cm`
  - `product_height_cm`
  - `product_width_cm`
- Foreign keys:
  - `product_category_name` may reference `product_category_name_translation.product_category_name` only when non-null.
- Constraints:
  - `product_id` is unique and not null.
- Special cases:
  - 610 products have a blank category.
  - 13 products have a non-blank category without a translation.
  - These products are retained; the category translation relationship is optional for blank or untranslated categories.

### sellers

- Grain: one row per seller.
- Primary key: `seller_id`.
- Nullable columns: none.
- Foreign keys: none.
- Constraints:
  - `seller_id` is unique and not null.
  - `seller_zip_code_prefix`, `seller_city`, and `seller_state` are not null.


### geolocation

- Grain: one distinct raw geolocation observation.
- Primary key: `geolocation_id`, a database-generated surrogate key.
- Foreign keys: none.
- Constraints:
  - `geolocation_id` is unique and not null.
  - `geolocation_zip_code_prefix`, `geolocation_city`, and `geolocation_state` are not null.
- Loading policy:
  - Exact duplicate rows are removed before loading.
  - 261,831 fully duplicate rows are removed.
  - 738,332 distinct rows remain.
- Evidence:
  - ZIP prefix is not unique after deduplication.
  - 17,972 ZIP prefixes have multiple rows.
  - 8,556 ZIP prefixes have multiple city labels.
  - 8 ZIP prefixes have multiple state labels.
- Decision:
  - ZIP prefix is a lookup key, not a primary key.
  - A surrogate key is required to preserve distinct geolocation observations.
  

### product_category_name_translation

- Grain: one row per Portuguese product category name.
- Primary key: `product_category_name`.
- Nullable columns: none.
- Foreign keys: none.
- Constraints:
  - `product_category_name` is unique and not null.
  - `product_category_name_english` is not null.
- Special cases:
  - Two non-blank product category names used by products have no translation row.
  - The product-to-translation relationship is therefore optional.

