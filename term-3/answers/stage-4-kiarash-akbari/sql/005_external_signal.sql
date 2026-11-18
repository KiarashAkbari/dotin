-- 005_external_signal.sql
-- The external table, and its join -- with the match rate printed instead of assumed.
--
-- decisions.md item 8 registers `ibge_agregados_municipio` as the source and records, in
-- the same item, that its URL has not been reachable from this environment. That is the
-- exact condition this file is written for: the schema must be ready, and must stay
-- harmless while the fetch is unverified. Two properties make that true:
--
--   * every object here is additive. `capstone.v_feature_frame` (003) does not read this
--     table, so an unverified external source cannot change a single modelled number;
--   * the join is a LEFT JOIN with an explicit `external_matched` flag. Unmatched rows are
--     quarantined by name and reported, never silently turned into NULL and never turned
--     into zero -- the same rule item 7 states for the internal missing values.
--
-- If the lift is not reproducible locally, the whole file is dropped (item 8's exit
-- clause) and nothing else in the stage has to be rewritten.
--
-- Applying this file writes no rows: it creates one table and four views. Re-running the
-- directory can therefore never add a row -- which matters, because the idempotency guard in
-- `tests/test_capstone_sql_guards.py` compares the frame before and after a second apply, and
-- a migration that inserts is indistinguishable from a frame whose values drifted. Any load of
-- external data goes through the two functions at the bottom of this file instead, so the
-- choice between "refresh" and "upsert" is made once, here, and not in a notebook cell.

CREATE TABLE IF NOT EXISTS capstone.external_municipio (
    ibge_code   CHAR(7)     NOT NULL,
    city_state  TEXT        NOT NULL,
    indicator   TEXT        NOT NULL,
    period      TEXT        NOT NULL,
    value       NUMERIC,
    source      TEXT        NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL,
    CONSTRAINT pk_external_municipio
        PRIMARY KEY (ibge_code, indicator, period),
    -- the key is the whole reason this table is joinable at all; a code that is not seven
    -- digits cannot be matched against IBGE's municipality code, so it is refused on entry
    CONSTRAINT ck_external_municipio_code CHECK (ibge_code ~ '^[0-9]{7}$'),
    -- `city_state` is the lowercase, accent-folded "<city>|<uf>" pair, which is the only
    -- join key the audit permits (163 city names recur in more than one state)
    CONSTRAINT ck_external_municipio_key CHECK (city_state ~ '^[^|]+\|[a-z]{2}$'),
    -- a fetched value with no timestamp is a value nobody can date, and a dated feature in
    -- a project whose whole guard is a timestamp must be dated
    CONSTRAINT ck_external_municipio_period CHECK (period ~ '^[0-9]{4}$')
);

-- One row per joinable (city, state) pair, pivoted so the frame joins on a single key
-- instead of multiplying rows by indicator. Aggregating here keeps the "one row per order"
-- invariant true by construction rather than by hope.
CREATE OR REPLACE VIEW capstone.v_external_municipio_pivot AS
SELECT
    e.city_state,
    MAX(e.ibge_code) AS ibge_code,
    JSON_OBJECT_AGG(e.indicator || '@' || e.period, e.value) AS indicators,
    COUNT(*) AS indicator_count,
    MAX(e.fetched_at) AS fetched_at
FROM capstone.external_municipio AS e
GROUP BY e.city_state;

-- The external columns, keyed by order, one row per order. It is deliberately a *separate*
-- view rather than a star-join into the frame: adding three columns to a 34-column view by
-- writing `f.*` would hide which columns the external source brings, and `sql/README.md`
-- bans exactly that kind of "and everything else". The model's input is the frame plus the
-- columns of this view that survive the lift test, and the join between the two is on
-- `order_id`, which this view guarantees is unique by construction (one row per frame row).
CREATE OR REPLACE VIEW capstone.v_external_signal AS
SELECT
    f.order_id,
    lower(c.customer_city) || '|' || lower(c.customer_state) AS join_key,
    (x.city_state IS NOT NULL) AS external_matched,
    x.ibge_code AS external_ibge_code,
    x.indicators AS external_indicators,
    x.indicator_count AS external_indicator_count
FROM capstone.v_feature_frame AS f
JOIN olist.customers AS c ON c.customer_id = f.customer_id
LEFT JOIN capstone.v_external_municipio_pivot AS x
       ON x.city_state = lower(c.customer_city) || '|' || lower(c.customer_state);

-- The match rate, as one number the report must print before any feature built on it is
-- believed. `source_keys` is how many distinct (city, state) pairs the orders carry; a
-- small `matched_keys` means the external table is a decoration.
CREATE OR REPLACE VIEW capstone.v_external_match_rate AS
SELECT
    COUNT(DISTINCT k.join_key) AS source_keys,
    COUNT(DISTINCT x.city_state) AS external_keys,
    COUNT(DISTINCT CASE WHEN x.city_state IS NOT NULL THEN k.join_key END) AS matched_keys,
    CASE
        WHEN COUNT(DISTINCT k.join_key) > 0
        THEN COUNT(DISTINCT CASE WHEN x.city_state IS NOT NULL THEN k.join_key END)::numeric
             / COUNT(DISTINCT k.join_key)
    END AS key_match_rate
FROM (
    SELECT DISTINCT lower(c.customer_city) || '|' || lower(c.customer_state) AS join_key
    FROM olist.customers AS c
) AS k
LEFT JOIN capstone.v_external_municipio_pivot AS x ON x.city_state = k.join_key;

-- The quarantine list: cities that did not match, ordered by how many orders they carry.
-- This is the "say which ones" half of item 8, and it is the output of the accent-folding
-- decision (50 city names survive it unmatched), so a reader can tell a folding bug from a
-- genuinely absent municipality.
CREATE OR REPLACE VIEW capstone.v_external_unmatched_cities AS
SELECT
    lower(c.customer_city) AS city,
    lower(c.customer_state) AS uf,
    COUNT(*) AS orders
FROM olist.customers AS c
LEFT JOIN capstone.v_external_municipio_pivot AS x
       ON x.city_state = lower(c.customer_city) || '|' || lower(c.customer_state)
WHERE x.city_state IS NULL
GROUP BY 1, 2
ORDER BY orders DESC, city;

-- The only two legal ways to change `capstone.external_municipio`, both idempotent.
--
-- `upsert` is keyed on the primary key the table already has, so running the same fetch twice
-- rewrites the same row instead of adding a second one. `refresh` is for a full reload: it
-- deletes exactly the rows of one source and nothing else, which keeps a rejected hypothesis
-- (item 8's exit clause) from wiping a table a later experiment may have filled with something
-- else. A loader that invents its own `INSERT` bypasses both, and the re-apply test is what
-- catches it.
CREATE OR REPLACE FUNCTION capstone.upsert_external_municipio(
    p_ibge_code text,
    p_city_state text,
    p_indicator text,
    p_period text,
    p_value numeric,
    p_source text
)
RETURNS void
LANGUAGE sql
AS $$
    INSERT INTO capstone.external_municipio AS e
        (ibge_code, city_state, indicator, period, value, source, fetched_at)
    VALUES
        (p_ibge_code, p_city_state, p_indicator, p_period, p_value, p_source, now())
    ON CONFLICT (ibge_code, indicator, period) DO UPDATE
        SET city_state = EXCLUDED.city_state,
            value      = EXCLUDED.value,
            source     = EXCLUDED.source,
            fetched_at = EXCLUDED.fetched_at;
$$;

CREATE OR REPLACE FUNCTION capstone.refresh_external_municipio(p_source text)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    removed bigint;
BEGIN
    IF p_source IS NULL OR length(btrim(p_source)) = 0 THEN
        RAISE EXCEPTION 'refresh_external_municipio needs the source it is replacing; an empty name would delete every row';
    END IF;
    DELETE FROM capstone.external_municipio AS e WHERE e.source = p_source;
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END;
$$;
