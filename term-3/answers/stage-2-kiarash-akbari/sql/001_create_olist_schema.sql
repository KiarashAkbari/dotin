-- 001_create_olist_schema.sql
-- Olist core schema: tables and constraints only.
-- No performance indexes are created here; they must be justified later
-- with execution plans in separate versioned SQL files.

CREATE SCHEMA IF NOT EXISTS olist;

-- Customer identifiers are 32-character hexadecimal strings in the source.
-- CHAR(32) makes the width explicit; the CHECK rejects malformed IDs.
CREATE TABLE olist.customers (
    customer_id              VARCHAR(32) NOT NULL,
    customer_unique_id       VARCHAR(32) NOT NULL,
    customer_zip_code_prefix VARCHAR(8)  NOT NULL,
    customer_city            TEXT        NOT NULL,
    customer_state           CHAR(2)     NOT NULL,
    CONSTRAINT pk_customers PRIMARY KEY (customer_id),
    CONSTRAINT ck_customers_id CHECK (customer_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT ck_customers_unique_id CHECK (customer_unique_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT ck_customers_zip CHECK (customer_zip_code_prefix ~ '^[0-9]{5,8}$')
);

CREATE TABLE olist.sellers (
    seller_id              VARCHAR(32) NOT NULL,
    seller_zip_code_prefix VARCHAR(8)  NOT NULL,
    seller_city            TEXT        NOT NULL,
    seller_state           CHAR(2)     NOT NULL,
    CONSTRAINT pk_sellers PRIMARY KEY (seller_id),
    CONSTRAINT ck_sellers_id CHECK (seller_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT ck_sellers_zip CHECK (seller_zip_code_prefix ~ '^[0-9]{5,8}$')
);

CREATE TABLE olist.product_category_name_translation (
    product_category_name         TEXT NOT NULL,
    product_category_name_english TEXT NOT NULL,
    CONSTRAINT pk_category_translation PRIMARY KEY (product_category_name)
);

-- No foreign key from products.product_category_name to the translation
-- table: 13 products use 2 category names that have no translation row.
-- The loader must map the source typos *_lenght to *_length.
CREATE TABLE olist.products (
    product_id                 VARCHAR(32) NOT NULL,
    product_category_name      TEXT,
    product_name_length        INTEGER,
    product_description_length INTEGER,
    product_photos_qty         INTEGER,
    product_weight_g           INTEGER,
    product_length_cm          INTEGER,
    product_height_cm          INTEGER,
    product_width_cm           INTEGER,
    CONSTRAINT pk_products PRIMARY KEY (product_id),
    CONSTRAINT ck_products_id CHECK (product_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT ck_products_nonneg CHECK (
        COALESCE(product_name_length, 0)        >= 0 AND
        COALESCE(product_description_length, 0) >= 0 AND
        COALESCE(product_photos_qty, 0)         >= 0 AND
        COALESCE(product_weight_g, 0)           >= 0 AND
        COALESCE(product_length_cm, 0)          >= 0 AND
        COALESCE(product_height_cm, 0)          >= 0 AND
        COALESCE(product_width_cm, 0)           >= 0
    )
);

-- TIMESTAMP WITHOUT TIME ZONE is used because the source provides no
-- timezone information; assigning one would be an unsupported assumption.
CREATE TABLE olist.orders (
    order_id                      VARCHAR(32) NOT NULL,
    customer_id                   VARCHAR(32) NOT NULL,
    order_status                  TEXT        NOT NULL,
    order_purchase_timestamp      TIMESTAMP   NOT NULL,
    order_approved_at             TIMESTAMP,
    order_delivered_carrier_date  TIMESTAMP,
    order_delivered_customer_date TIMESTAMP,
    order_estimated_delivery_date TIMESTAMP   NOT NULL,
    CONSTRAINT pk_orders PRIMARY KEY (order_id),
    CONSTRAINT ck_orders_id CHECK (order_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id)
        REFERENCES olist.customers (customer_id)
        ON DELETE RESTRICT
);

-- NUMERIC(10,2) is used for monetary values to avoid binary floating-point
-- drift in SUM and AVG calculations.
CREATE TABLE olist.order_items (
    order_id            VARCHAR(32)   NOT NULL,
    order_item_id       INTEGER       NOT NULL,
    product_id          VARCHAR(32)   NOT NULL,
    seller_id           VARCHAR(32)   NOT NULL,
    shipping_limit_date TIMESTAMP     NOT NULL,
    price               NUMERIC(10,2) NOT NULL,
    freight_value       NUMERIC(10,2) NOT NULL,
    CONSTRAINT pk_order_items PRIMARY KEY (order_id, order_item_id),
    CONSTRAINT ck_order_items_order_id CHECK (order_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT ck_order_items_product_id CHECK (product_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT ck_order_items_seller_id CHECK (seller_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT fk_items_order FOREIGN KEY (order_id)
        REFERENCES olist.orders (order_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_items_product FOREIGN KEY (product_id)
        REFERENCES olist.products (product_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_items_seller FOREIGN KEY (seller_id)
        REFERENCES olist.sellers (seller_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_items_price CHECK (price >= 0),
    CONSTRAINT ck_items_freight CHECK (freight_value >= 0),
    CONSTRAINT ck_items_sequence CHECK (order_item_id >= 1)
);

-- Zero-value and zero-installment payment rows exist in the source and are
-- retained; therefore the constraints use >= 0 rather than > 0.
CREATE TABLE olist.payments (
    order_id             VARCHAR(32)   NOT NULL,
    payment_sequential   INTEGER       NOT NULL,
    payment_type         TEXT          NOT NULL,
    payment_installments INTEGER       NOT NULL,
    payment_value        NUMERIC(10,2) NOT NULL,
    CONSTRAINT pk_payments PRIMARY KEY (order_id, payment_sequential),
    CONSTRAINT ck_payments_order_id CHECK (order_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT fk_payments_order FOREIGN KEY (order_id)
        REFERENCES olist.orders (order_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_payments_sequence CHECK (payment_sequential >= 1),
    CONSTRAINT ck_payments_installments CHECK (payment_installments >= 0),
    CONSTRAINT ck_payments_value CHECK (payment_value >= 0)
);

-- review_id alone is not unique: 789 review IDs are reused across different
-- orders. The pair (review_id, order_id) was verified unique on 99,224 rows.
CREATE TABLE olist.order_reviews (
    review_id               VARCHAR(32) NOT NULL,
    order_id                VARCHAR(32) NOT NULL,
    review_score            SMALLINT    NOT NULL,
    review_comment_title    TEXT,
    review_comment_message  TEXT,
    review_creation_date    TIMESTAMP   NOT NULL,
    review_answer_timestamp TIMESTAMP,
    CONSTRAINT pk_order_reviews PRIMARY KEY (review_id, order_id),
    CONSTRAINT ck_reviews_id CHECK (review_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT ck_reviews_order_id CHECK (order_id ~ '^[0-9a-f]{32}$'),
    CONSTRAINT fk_reviews_order FOREIGN KEY (order_id)
        REFERENCES olist.orders (order_id)
        ON DELETE RESTRICT,
    CONSTRAINT ck_reviews_score CHECK (review_score BETWEEN 1 AND 5)
);

-- A surrogate key is required because ZIP prefix is not unique.
-- Exact duplicate rows must be removed by the loader before insertion.
CREATE TABLE olist.geolocation (
    geolocation_id              BIGINT GENERATED ALWAYS AS IDENTITY,
    geolocation_zip_code_prefix VARCHAR(8)       NOT NULL,
    geolocation_lat             DOUBLE PRECISION NOT NULL,
    geolocation_lng             DOUBLE PRECISION NOT NULL,
    geolocation_city            TEXT             NOT NULL,
    geolocation_state           CHAR(2)          NOT NULL,
    CONSTRAINT pk_geolocation PRIMARY KEY (geolocation_id),
    CONSTRAINT ck_geolocation_zip CHECK (
        geolocation_zip_code_prefix ~ '^[0-9]{5,8}$'
    ),
    CONSTRAINT ck_geolocation_lat CHECK (
        geolocation_lat BETWEEN -90 AND 90
    ),
    CONSTRAINT ck_geolocation_lng CHECK (
        geolocation_lng BETWEEN -180 AND 180
    )
);