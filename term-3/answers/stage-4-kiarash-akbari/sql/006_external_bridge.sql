-- 006_external_bridge.sql
-- The one column the external signal has to become before anyone can ablate it.
--
-- `005` created `capstone.v_external_signal`: one row per frame row, with `external_matched`,
-- `external_ibge_code`, `external_indicators` (a JSON object keyed `"<indicator>@<period>"`) and
-- `external_indicator_count`. It deliberately stopped there, and its own header says why: the frame
-- must not read a source whose fetch was unverified, so "no external data" could not change a
-- modelled number. That design is still true -- this file does not touch `v_feature_frame` either.
-- But phase 5's ablation needs the opposite half: for the comparison to be an ablation rather than a
-- second model, the *same fitted rows* have to be scored once with a signal column and once without,
-- and a Python-side scorer cannot reach a JSON object through a frame that never carried it.
--
-- So this file adds exactly one thing: a typed, per-order numeric column, in a view the model step
-- joins by `order_id`. `features.build`, the audit guard and `v_feature_frame` stay as they are --
-- the frame's 29 columns and their as-of story are not this file's business, and a reader who
-- re-applies the directory gets the same frame.
--
-- Two properties are worth reading before changing anything:
--
--   * the indicator *name* is not written here. It is a datum of the response `capstone.external.py`
--     parsed, and spelling it in SQL would put the same claim in two files with only one of them
--     checked. The rule is positional instead: a municipality may carry exactly one value, and the
--     guard below refuses the apply when it carries more than one. A `min()` without that guard would
--     be a choice pretending to be a cast.
--   * the year in the column's name is checked, not assumed. `external_density_2022` is what
--     `src/capstone/model.py` asks the frame for, so the migration refuses a database whose stored
--     period is some other year rather than quietly rebinding the name to new data. A static test in
--     `tests/test_capstone_sql_guards.py` pins that the name and the guard name the same year, which
--     is the only way this pair cannot rot: nothing outside this file claims 2022 in a form the SQL
--     could be compared against -- `external.py` takes the period from the response, and the URL that
--     carried it lives in the student's ignored local config.
--
-- What the column is, in one sentence for the report: an annual IBGE aggregate for 2022, joined
-- through the customer's municipality. It is a covariate of *place*, not a measurement taken at
-- `order_approved_at` -- the orders in this corpus are from 2016 to 2018, so the value postdates the
-- decision moment it would be used to predict. That is why the only legal use of it here is as a
-- difference between two runs of the same rows (`model.ablation`), and why the paragraph in
-- `decisions.md` section 13 says the quiet part: no lift may be claimed from it, at home or in
-- production.
--
-- Applying this file writes no rows, and re-applying it changes nothing: one view replaced, one DO
-- block that reads and prints. That keeps `005`'s re-apply invariant alive -- the idempotency guard
-- in `tests/test_capstone_sql_guards.py` compares the frame before and after a second apply, and a
-- migration that wrote rows would be indistinguishable from a frame whose values drifted.

-- The per-order feature view: one row per frame row, keyed by order_id, no fan-out. `005`'s pivot is
-- grouped by `city_state`, so a municipality carrying two indicators for the period would multiply
-- the join -- which is the failure the DO block below refuses rather than sorts away.
CREATE OR REPLACE VIEW capstone.v_external_feature AS
SELECT
    s.order_id,
    s.external_matched,
    s.external_ibge_code,
    s.external_indicator_count,
    -- the cast is float8 and not numeric: `model.numeric()` refuses a text cell, and the JSON
    -- extraction hands back text, so an explicit cast is the one place the shape changes
    (s.external_indicators ->> (
        SELECT min(k.key)
        FROM json_object_keys(s.external_indicators) AS k(key)
    ))::float8 AS external_density_2022
FROM capstone.v_external_signal AS s;

-- The guard, and the only place this directory admits what it resolved.
--
-- Both halves matter. The refusal is what stops "one value per municipality" from being an
-- assumption: with two indicators stored for 2022, the `min(key)` above would silently pick the
-- alphabetically first one, and the ablation would then be comparing a model to a coin flip. The
-- notice is what makes the column honest: `external_density_2022` names the indicator the contract
-- registers, and the only way to know that the table agrees is to print what is in it. On a database
-- with no external rows -- every fresh clone, and every run of the smoke tests -- neither branch has
-- anything to say, and the view above stands with NULLs.
DO $bridge$
DECLARE
    crowded  bigint;
    resolved text;
BEGIN
    -- one municipality, one value: the pick above is a cast only while this holds
    SELECT count(*)
      INTO crowded
      FROM (
        SELECT e.city_state
          FROM capstone.external_municipio AS e
         GROUP BY e.city_state
        HAVING count(DISTINCT e.indicator || '@' || e.period) > 1
      ) AS both_ways;
    IF crowded > 0 THEN
        RAISE EXCEPTION
            'capstone.external_municipio carries % municipality key(s) with more than one indicator-period pair, '
            'and capstone.v_external_feature would have to choose one of them. One value per joinable place is '
            'the contract: either fetch the second indicator into its own column with its own view, or narrow '
            'the table to the registered source; do not let the view decide by sorting.',
            crowded;
    END IF;

    -- the year the column name claims, asked of the data
    SELECT count(*)
      INTO crowded
      FROM (
        SELECT e.city_state
          FROM capstone.external_municipio AS e
         GROUP BY e.city_state
        HAVING bool_or(e.period <> '2022')
      ) AS wrong_year;
    IF crowded > 0 THEN
        RAISE EXCEPTION
            'capstone.external_municipio holds a period other than 2022 for % municipality key(s), while '
            'capstone.v_external_feature publishes the value under the name external_density_2022. The name is '
            'the claim being checked: add a column per period, and say in decisions.md which one the model reads, '
            'rather than letting one name follow whichever year arrived.',
            crowded;
    END IF;

    SELECT string_agg(DISTINCT e.indicator || '@' || e.period, ', ' ORDER BY e.indicator || e.period)
      INTO resolved
      FROM capstone.external_municipio AS e;

    IF resolved IS NULL THEN
        RAISE NOTICE
            'capstone.v_external_feature is created with no rows to read: external_density_2022 is NULL for '
            'every order, so the model step reports the signal as absent rather than scoring it as zero';
    ELSE
        RAISE NOTICE 'capstone.v_external_feature resolves indicator ''%''', resolved;
    END IF;
END
$bridge$;

-- The one count the report has to print beside any number built on this view: how many orders can
-- actually see a value. `005`'s `v_external_match_rate` counts *keys*; this counts *orders*, which is
-- the denominator the ablation is restricted to, and the two are different numbers on purpose (one
-- city with ten thousand orders moves this one and not that one).
CREATE OR REPLACE VIEW capstone.v_external_feature_coverage AS
SELECT
    count(*) AS frame_rows,
    count(*) FILTER (WHERE f.external_matched) AS matched_rows,
    count(f.external_density_2022) AS valued_rows,
    CASE
        WHEN count(*) > 0 THEN count(f.external_density_2022)::numeric / count(*)
    END AS value_share
FROM capstone.v_external_feature AS f;
