-- 002_time_guard.sql
-- The decision moment, defined once, and the two guards that read the audit.
--
-- `04` reduces the whole leak question to one sentence: only what existed at
-- `order_approved_at` may be read. Two things in SQL make that sentence checkable
-- instead of advisory: one place where the moment is defined, and one function that
-- refuses a column the audit does not allow. The frame view in 003 is built on both.

-- The anchor. Every feature and every label in this stage is defined relative to this
-- view, so a change to the decision moment is a change in one file.
--
-- `order_approved_at IS NULL` is D1 (`drop_before_training`, decisions.md item 1): the
-- row is kept here so the exclusion is countable, and the feature frame drops it.
--
-- The last two columns are the order's own promise, both fixed when the order is placed and so
-- known at the anchor: the frame's `promise_days` is their difference. They are carried here so
-- the frame reads `olist.orders` once, through this view, instead of joining the table a second
-- time by `order_id` (decisions.md section 38). Appended after `has_anchor` on purpose:
-- `CREATE OR REPLACE VIEW` may add columns at the end and nothing else, so a database that
-- already holds the four-column anchor takes this file without a drop.
CREATE OR REPLACE VIEW capstone.v_decision_moment AS
SELECT
    o.order_id,
    o.customer_id,
    o.order_approved_at,
    (o.order_approved_at IS NOT NULL) AS has_anchor,
    o.order_purchase_timestamp,
    o.order_estimated_delivery_date
FROM olist.orders AS o;

-- The excluded population, as a number the report quotes rather than a comment.
--
-- This view is a diagnostic and not a feature input, which is why it is the one place the
-- final ``order_status`` may be read at all: the audit marks it forbidden
-- (``at_approval = false``, its value space contains ``delivered``), and D1's cost is not
-- uniform -- 141 of the 160 are canceled or unavailable -- so the number has to be
-- computable while remaining unavailable to the model. ``v_feature_frame`` reads
-- ``v_decision_moment``, never this view, and the test in
-- ``tests/test_capstone_sql_guards.py`` is what keeps that sentence true.
CREATE OR REPLACE VIEW capstone.v_d1_excluded_orders AS
SELECT o.order_id, o.order_status, o.customer_id
FROM olist.orders AS o
WHERE o.order_approved_at IS NULL;

-- What the audit forbids, in SQL. `available_at` is the reason a reader needs; the
-- verdict alone would be an instruction without an argument.
CREATE OR REPLACE VIEW capstone.v_audit_rejections AS
SELECT "table", "column", verdict, available_at
FROM capstone.column_audit
WHERE verdict <> 'allowed'
ORDER BY "table", "column";

-- Is one column usable at the decision moment? Stable, so the frame can call it per
-- column; the `at_approval` test is repeated on purpose, because a row that says
-- `allowed` while `at_approval` is false must not pass through the second door either.
CREATE OR REPLACE FUNCTION capstone.audit_is_allowed(p_table text, p_column text)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM capstone.column_audit AS a
        WHERE a."table" = p_table
          AND a."column" = p_column
          AND a.verdict = 'allowed'
          AND a.at_approval
    );
$$;

-- The same question for a list, refusing by name.
--
-- `capstone.audit.require_allowed` is the Python twin, and it asks about the columns a
-- builder *intends* to use -- that half is phase 4's pending loader, so today the intent is
-- written down but not executed. This function asks about what a view actually reads, taken
-- out of `pg_depend`, which nobody can forget to update; the live test in
-- tests/test_capstone_sql_guards.py feeds those catalog rows through it, and the static test
-- in the same file does the equivalent by parsing this directory.
CREATE OR REPLACE FUNCTION capstone.require_allowed(p_columns text[])
RETURNS void
LANGUAGE plpgsql
STABLE
AS $$
DECLARE
    offenders text;
BEGIN
    IF p_columns IS NULL OR cardinality(p_columns) = 0 THEN
        RAISE EXCEPTION 'require_allowed was given no columns; an empty list is not an audit';
    END IF;

    SELECT string_agg(offence, '; ' ORDER BY offence)
      INTO offenders
      FROM (
        -- the audit is keyed on (table, column); a caller may hand us 'olist.orders.x' as
        -- easily as 'orders.x', and the version that silently finds no audit row is the
        -- dangerous one -- so the schema prefix is dropped here rather than missed there
        SELECT ref.raw AS offence
          FROM (
            SELECT c AS raw,
                   (string_to_array(c, '.'))[
                       array_length(string_to_array(c, '.'), 1) - 1] AS tbl,
                   (string_to_array(c, '.'))[array_length(string_to_array(c, '.'), 1)] AS col
              FROM unnest(p_columns) AS c
          ) AS ref
          LEFT JOIN capstone.column_audit AS a
            ON a."table" = ref.tbl AND a."column" = ref.col
         WHERE a."table" IS NULL
            OR a.verdict <> 'allowed'
            OR NOT a.at_approval
      ) AS refused;

    IF offenders IS NOT NULL THEN
        RAISE EXCEPTION 'the column audit rejects this feature frame: %', offenders
            USING HINT = 'capstone.v_audit_rejections lists every forbidden column and why';
    END IF;
END;
$$;

-- A convenience for psql and for the notebooks: the guard as a one-line query.
CREATE OR REPLACE FUNCTION capstone.audit_is_allowed(p_column text)
RETURNS boolean
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
    -- last two dotted parts win, so 'olist.order_reviews.review_score' and
    -- 'order_reviews.review_score' ask the same question; a one-part name has no column to
    -- look up and is refused, which is the safe direction
    SELECT capstone.audit_is_allowed(
        (string_to_array(p_column, '.'))[
            array_length(string_to_array(p_column, '.'), 1) - 1],
        (string_to_array(p_column, '.'))[array_length(string_to_array(p_column, '.'), 1)]
    );
$$;
