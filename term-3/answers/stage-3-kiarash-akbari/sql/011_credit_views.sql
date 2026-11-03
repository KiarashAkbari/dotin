-- Stage 3, step 2: the read-facing views over the credit schema.
--
-- Split from 010 because psycopg 3 fails to execute the combined multi-statement
-- file through its prepared-statement path; the FILTER clause inside the
-- missingness view is where it trips. The migration runner applies files in
-- lexical order, so 010 (tables) runs before 011 (views).

-- Database-side preparation. The boundary this file draws: any transform that is
-- true for every row, needs no fitted statistic, and would otherwise be repeated
-- by each consumer, belongs here. Anything whose parameters come from data -
-- imputed values, scaling, encodings - must stay in Python, because a view cannot
-- be fitted on one split and reused on another.
CREATE OR REPLACE VIEW credit.v_model_input AS
SELECT
    a.row_id,
    a.serious_dlqin_2yrs,
    a.revolving_utilization_unsecured,
    LEAST(a.revolving_utilization_unsecured, 1.0) AS utilization_capped,
    a.age,
    CASE WHEN a.num_late_30_59 IN (96, 98) THEN NULL ELSE a.num_late_30_59 END
        AS num_late_30_59,
    CASE WHEN a.num_late_90 IN (96, 98) THEN NULL ELSE a.num_late_90 END
        AS num_late_90,
    CASE WHEN a.num_late_60_89 IN (96, 98) THEN NULL ELSE a.num_late_60_89 END
        AS num_late_60_89,
    a.debt_ratio,
    a.monthly_income,
    CASE WHEN a.monthly_income IS NULL THEN 1 ELSE 0 END AS income_missing,
    a.num_open_credit_lines,
    a.num_real_estate_loans,
    a.num_dependents
FROM credit.application AS a;

COMMENT ON VIEW credit.v_model_input IS
    'The only relation the notebook reads. Codes become NULL here so no sum can '
    'mistake them for counts; utilisation is capped for models that read the value '
    'as a ratio, while the raw column stays selectable.';

-- Missingness, measured in the database. The Python profile and this view must
-- agree row for row, which is what makes both of them checkable.
CREATE OR REPLACE VIEW credit.v_column_missingness AS
WITH flagged AS (
    SELECT 'age' AS column_name, a.age AS value, a.serious_dlqin_2yrs AS target
        FROM credit.application AS a
    UNION ALL
    SELECT 'debt_ratio', a.debt_ratio, a.serious_dlqin_2yrs FROM credit.application AS a
    UNION ALL
    SELECT 'monthly_income', a.monthly_income, a.serious_dlqin_2yrs FROM credit.application AS a
    UNION ALL
    SELECT 'num_dependents', a.num_dependents, a.serious_dlqin_2yrs FROM credit.application AS a
    UNION ALL
    SELECT 'num_late_30_59', a.num_late_30_59, a.serious_dlqin_2yrs FROM credit.application AS a
    UNION ALL
    SELECT 'num_late_60_89', a.num_late_60_89, a.serious_dlqin_2yrs FROM credit.application AS a
    UNION ALL
    SELECT 'num_late_90', a.num_late_90, a.serious_dlqin_2yrs FROM credit.application AS a
    UNION ALL
    SELECT 'num_open_credit_lines', a.num_open_credit_lines, a.serious_dlqin_2yrs
        FROM credit.application AS a
    UNION ALL
    SELECT 'num_real_estate_loans', a.num_real_estate_loans, a.serious_dlqin_2yrs
        FROM credit.application AS a
    UNION ALL
    SELECT 'revolving_utilization_unsecured', a.revolving_utilization_unsecured,
        a.serious_dlqin_2yrs
        FROM credit.application AS a
)
SELECT
    flagged.column_name,
    count(*) AS rows_total,
    count(*) - count(flagged.value) AS missing_rows,
    round((count(*) - count(flagged.value))::numeric / NULLIF(count(*), 0) * 100, 4)
        AS missing_pct,
    round((avg(flagged.target) FILTER (WHERE flagged.value IS NULL))::numeric, 5)
        AS default_rate_when_missing,
    round((avg(flagged.target) FILTER (WHERE flagged.value IS NOT NULL))::numeric, 5)
        AS default_rate_otherwise
FROM flagged
GROUP BY flagged.column_name;

COMMENT ON VIEW credit.v_column_missingness IS
    'Per-column absence counts computed from the raw table, codes included: a row '
    'that carries a code is a row that was reported, and conflating the two would '
    'hide the very artifact the analysis is about.';