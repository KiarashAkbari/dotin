-- Missingness, view parity, and cohort sizes - all read from the database.
--   psql "$DATABASE_URL" -f sql/queries/credit_missingness_parity.sql
-- The second statement is the one that earns the "inspectable" claim: if the SQL
-- view and the Python mask ever disagree, this returns a non-zero count.

SELECT
    column_name,
    rows_total,
    missing_rows,
    missing_pct,
    default_rate_when_missing,
    default_rate_otherwise,
    round(
        (coalesce(default_rate_when_missing, 0) - coalesce(default_rate_otherwise, 0))::numeric,
        5
    ) AS difference
FROM credit.v_column_missingness
ORDER BY missing_rows DESC, column_name;

-- Rows the masked view changes, per column: the view must null exactly the code
-- rows and nothing else.
SELECT
    'num_late_90_codes' AS check_name,
    count(*) FILTER (WHERE a.num_late_90 IN (96, 98)) AS raw_code_rows,
    count(*) FILTER (WHERE v.num_late_90 IS NULL AND a.num_late_90 IN (96, 98)) AS nulled_by_view,
    count(*) FILTER (WHERE v.num_late_90 IS NULL AND a.num_late_90 NOT IN (96, 98))
        AS wrongly_nulled
FROM credit.application AS a
JOIN credit.v_model_input AS v USING (row_id)
UNION ALL
SELECT
    'income_missing_indicator',
    count(*) FILTER (WHERE a.monthly_income IS NULL),
    count(*) FILTER (WHERE v.income_missing = 1),
    count(*) FILTER (WHERE v.income_missing = 1 AND a.monthly_income IS NOT NULL)
FROM credit.application AS a
JOIN credit.v_model_input AS v USING (row_id)
UNION ALL
SELECT
    'utilisation_cap_moves_only_the_tails',
    count(*) FILTER (WHERE a.revolving_utilization_unsecured > 1.0),
    count(*) FILTER (WHERE v.utilization_capped = 1.0),
    count(*) FILTER (WHERE v.utilization_capped > 1.0)
FROM credit.application AS a
JOIN credit.v_model_input AS v USING (row_id);

-- Who is in each cohort, before any metric is computed per cohort: a subgroup
-- fairness claim on 40 rows is not a claim.
SELECT
    CASE
        WHEN v.age IS NULL THEN 'missing'
        WHEN v.age < 30 THEN 'under_30'
        WHEN v.age < 45 THEN '30_to_44'
        WHEN v.age < 60 THEN '45_to_59'
        ELSE '60_and_over'
    END AS age_band,
    count(*) AS rows_total,
    sum(v.serious_dlqin_2yrs) AS defaults,
    round(avg(v.serious_dlqin_2yrs)::numeric, 5) AS default_rate,
    round(avg(v.income_missing)::numeric, 5) AS income_missing_rate
FROM credit.v_model_input AS v
GROUP BY 1
ORDER BY rows_total DESC;
