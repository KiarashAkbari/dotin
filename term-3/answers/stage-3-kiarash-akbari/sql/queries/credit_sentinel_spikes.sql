-- The measurement that turns "I think these are codes" into a number.
-- Run against the database that holds credit.application:
--   psql "$DATABASE_URL" -f sql/queries/credit_sentinel_spikes.sql
-- Output belongs in the notebook, and the notebook's numbers belong in report.md.

WITH probed AS (
    SELECT 'num_late_30_59' AS column_name, num_late_30_59 AS value,
           serious_dlqin_2yrs AS target, code
        FROM credit.application CROSS JOIN (VALUES (96), (98)) AS codes (code)
    UNION ALL
    SELECT 'num_late_90', num_late_90, serious_dlqin_2yrs, code
        FROM credit.application CROSS JOIN (VALUES (96), (98)) AS codes (code)
    UNION ALL
    SELECT 'num_late_60_89', num_late_60_89, serious_dlqin_2yrs, code
        FROM credit.application CROSS JOIN (VALUES (96), (98)) AS codes (code)
),
counted AS (
    SELECT
        column_name,
        code,
        count(*) FILTER (WHERE value = code) AS rows_with_code,
        avg(target) FILTER (WHERE value = code) AS default_rate_with_code,
        avg(target) FILTER (WHERE value IS DISTINCT FROM code) AS default_rate_otherwise
    FROM probed
    GROUP BY column_name, code
),
overall AS (
    SELECT count(*) AS rows_total, avg(serious_dlqin_2yrs) AS default_rate_overall
        FROM credit.application
)
SELECT
    counted.column_name,
    counted.code,
    counted.rows_with_code,
    overall.rows_total,
    round(counted.rows_with_code::numeric / NULLIF(overall.rows_total, 0) * 100, 4)
        AS pct_of_file,
    round(counted.default_rate_with_code::numeric, 5) AS default_rate_with_code,
    round(counted.default_rate_otherwise::numeric, 5) AS default_rate_otherwise,
    round(overall.default_rate_overall::numeric, 5) AS default_rate_overall
FROM counted CROSS JOIN overall
WHERE counted.rows_with_code > 0
ORDER BY counted.column_name, counted.code;

-- The same shape for the values that are merely extreme rather than coded: the
-- file contains utilisation ratios above 1 and at least one impossible age, and
-- the difference between "outlier" and "corrupt" decides the policy, so both
-- counts are taken from the table and not from memory.
SELECT
    'revolving_utilization_above_1' AS measure,
    count(*) FILTER (WHERE revolving_utilization_unsecured > 1.0) AS rows_hit,
    count(*) AS rows_total,
    round(count(*) FILTER (WHERE revolving_utilization_unsecured > 1.0)::numeric
          / NULLIF(count(*), 0) * 100, 4) AS pct,
    round(avg(serious_dlqin_2yrs) FILTER (WHERE revolving_utilization_unsecured > 1.0)::numeric, 5)
        AS default_rate_hit
FROM credit.application
UNION ALL
SELECT
    'age_below_18',
    count(*) FILTER (WHERE age < 18),
    count(*),
    round(count(*) FILTER (WHERE age < 18)::numeric / NULLIF(count(*), 0) * 100, 4),
    round(avg(serious_dlqin_2yrs) FILTER (WHERE age < 18)::numeric, 5)
FROM credit.application
UNION ALL
SELECT
    'age_above_100',
    count(*) FILTER (WHERE age > 100),
    count(*),
    round(count(*) FILTER (WHERE age > 100)::numeric / NULLIF(count(*), 0) * 100, 4),
    round(avg(serious_dlqin_2yrs) FILTER (WHERE age > 100)::numeric, 5)
FROM credit.application
UNION ALL
SELECT
    'negative_count_column',
    count(*) FILTER (
        WHERE num_dependents < 0 OR num_late_90 < 0 OR num_open_credit_lines < 0
    ),
    count(*),
    round(count(*) FILTER (
        WHERE num_dependents < 0 OR num_late_90 < 0 OR num_open_credit_lines < 0
    )::numeric / NULLIF(count(*), 0) * 100, 4),
    round(avg(serious_dlqin_2yrs) FILTER (
        WHERE num_dependents < 0 OR num_late_90 < 0 OR num_open_credit_lines < 0
    )::numeric, 5)
FROM credit.application;
