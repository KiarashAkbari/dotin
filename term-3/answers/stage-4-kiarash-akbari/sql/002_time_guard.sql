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
CREATE OR REPLACE VIEW capstone.v_decision_moment AS
SELECT
    o.order_id,
    o.customer_id,
    o.order_approved_at,
    (o.order_approved_at IS NOT NULL) AS has_anchor
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
        SELECT c AS offence
          FROM unnest(p_columns) AS c
          LEFT JOIN capstone.column_audit AS a
            ON a."table" = split_part(c, '.', 1)
           AND a."column" = split_part(c, '.', 2)
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
    SELECT capstone.audit_is_allowed(split_part(p_column, '.', 1), split_part(p_column, '.', 2));
$$;
