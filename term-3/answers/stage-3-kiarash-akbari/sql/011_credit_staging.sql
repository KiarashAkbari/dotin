-- 011: a staging table for files that arrive after the model exists.
--
-- Why a second table and not a re-load: the deliverable is a script that takes a
-- fresh file with the same schema and produces predictions, and it must apply the
-- preparation unchanged. The cheapest way to guarantee "unchanged" is to make the
-- fresh file go through the same view definition over a table shaped like the first
-- one - so the file is loaded into credit.staging_application and read back through
-- credit.v_staging_model_input, and the only difference between the two paths is the
-- name of the table the view selects from.
--
-- It is also the additive half of the schema story: the migration ledger checksums
-- applied files, so 010 can never be edited after a database has applied it. Any
-- change to the credit schema from now on is a new file, exactly like this one.
--
-- No BEGIN/COMMIT: apply_sql_migrations owns the transaction.

CREATE TABLE IF NOT EXISTS credit.staging_application (
    row_id                            bigint        NOT NULL,
    serious_dlqin_2yrs                smallint,
    revolving_utilization_unsecured   double precision,
    age                               double precision,
    num_late_30_59                    double precision,
    debt_ratio                        double precision,
    monthly_income                    double precision,
    num_open_credit_lines             double precision,
    num_late_90                       double precision,
    num_real_estate_loans             double precision,
    num_late_60_89                    double precision,
    num_dependents                    double precision,
    CONSTRAINT pk_credit_staging PRIMARY KEY (row_id),
    CONSTRAINT ck_credit_staging_target CHECK (serious_dlqin_2yrs IN (0, 1))
);

COMMENT ON TABLE credit.staging_application IS
    'A fresh file, same columns as credit.application, except the target: rows here '
    'may not have an outcome yet, so serious_dlqin_2yrs is nullable and the view over '
    'it is otherwise identical to credit.v_model_input.';

CREATE OR REPLACE VIEW credit.v_staging_model_input AS
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
FROM credit.staging_application AS a;

COMMENT ON VIEW credit.v_staging_model_input IS
    'The model-input view applied to staging rows. Kept as a copy of the view text '
    'rather than a macro so it can be read on its own; tests/credit_sql.py asserts '
    'the two SELECT lists are the same expression per expression.';
