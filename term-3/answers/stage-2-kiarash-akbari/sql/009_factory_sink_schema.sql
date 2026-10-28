-- 009_factory_sink_schema.sql
-- Stage 1 destination schema: the factory's own curated outputs, loaded
-- through the same data_factory package that produced them.
--
-- Why 009 and not 004: the applied chain is discovered by filename prefix
-- (cli._is_base_migration takes 001-003, cli._is_snapshot_migration takes
-- 005-008) and 004 belongs to sql/rejected/, where sql_migration_files never
-- looks because it only globs direct children of sql/. Using the next free
-- prefix keeps the rejected experiment's number intact and puts this file
-- last in lexical order, which is where a destination schema belongs: it must
-- be applied after the data it mirrors has been loaded, not before.
--
-- The sink owns nothing in olist and olist owns nothing here; a full reset
-- therefore drops both schemas, each with its own function
-- (database.reset_olist_schema and database.reset_factory_schema) so the scope
-- of --reset is explicit in code, not implied by a CASCADE chain.
--
-- Repeated load: data_factory.database.load_stage1_sink truncates every table
-- in this file inside the same transaction it inserts into, so re-running the
-- sink converges instead of accumulating. A failure mid-load rolls the
-- transaction back and the previously loaded rows stay exactly as they were.
--
-- No performance indexes are created here. The five measured dashboard
-- queries read the olist schema; indexes in this schema would be cost without
-- a reader to justify it, which is the same rule 001 states for olist.

CREATE SCHEMA IF NOT EXISTS factory;

-- Grain: one row per complaint case, exactly the columns of
-- data/curated/cases.csv (clean.CLEAN_COLUMNS is the contract; the loader
-- reflects this table and fails loudly if a column is missing).
-- Timestamps stay naive because Stage 1 emits New York wall-clock text
-- (OUTPUT_DATETIME_FORMAT in clean.py) for a window defined in that zone;
-- converting here would silently reinterpret the audited artifact.
CREATE TABLE IF NOT EXISTS factory.case_file (
    unique_key         BIGINT       NOT NULL,
    created_at         TIMESTAMP    NOT NULL,
    closed_at          TIMESTAMP,
    duration_seconds   BIGINT,
    is_ambiguous       BOOLEAN      NOT NULL,
    agency             TEXT         NOT NULL,
    complaint_type     TEXT         NOT NULL,
    descriptor         TEXT,
    status             TEXT         NOT NULL,
    borough            TEXT         NOT NULL,
    community_board    TEXT,
    incident_zip       VARCHAR(5),
    modzcta            VARCHAR(5),
    population         BIGINT,
    CONSTRAINT pk_factory_case_file PRIMARY KEY (unique_key),
    -- Stage 1 rejects a close before a create (ReasonCode.CLOSED_BEFORE_CREATED)
    -- and drops nothing: equality is legal, so the same-second cohort is
    -- visible here as duration_seconds = 0 rather than hidden.
    CONSTRAINT ck_factory_case_file_order CHECK (closed_at IS NULL OR closed_at >= created_at),
    CONSTRAINT ck_factory_case_file_duration CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    CONSTRAINT ck_factory_case_file_zip CHECK (
        incident_zip IS NULL OR incident_zip ~ '^[0-9]{5}$'
    ),
    CONSTRAINT ck_factory_case_file_modzcta CHECK (
        modzcta IS NULL OR modzcta ~ '^[0-9]{5}$'
    ),
    CONSTRAINT ck_factory_case_file_population CHECK (
        population IS NULL OR population > 0
    )
);

-- The delivered wide table has one column pair per month, so its shape follows
-- the configured window. One row per (area, complaint type, month) keeps the
-- table stable across windows and lets SQL do the pivoting a report needs.
-- modzcta is TEXT, not VARCHAR(5): UNKNOWN_AREA from outputs.py is the sentinel
-- Stage 1 writes when a case has no ZIP match, and the pair counts must stay
-- complete for the area totals to reconcile with cases.csv.
CREATE TABLE IF NOT EXISTS factory.complaint_area_month (
    modzcta         TEXT           NOT NULL,
    complaint_type  TEXT           NOT NULL,
    month_start     CHAR(7)        NOT NULL,
    complaint_count INTEGER        NOT NULL CHECK (complaint_count >= 0),
    rate_per_10k    NUMERIC(12, 4),
    population      BIGINT,
    CONSTRAINT pk_factory_complaint_area_month
        PRIMARY KEY (modzcta, complaint_type, month_start),
    CONSTRAINT ck_factory_complaint_area_month_area CHECK (
        modzcta ~ '^([0-9]{5}|UNKNOWN)$'
    ),
    CONSTRAINT ck_factory_complaint_area_month_month CHECK (
        month_start ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'
    ),
    -- The rate is stored as Stage 1 delivered it (outputs.RATE_BASE is 10,000)
    -- so the table is a faithful copy; recomputing it here would hide a
    -- population join that had already gone wrong.
    CONSTRAINT ck_factory_complaint_area_month_rate CHECK (
        rate_per_10k IS NULL OR rate_per_10k >= 0
    )
);

-- profile.REPORT_FILENAME is JSON. It is parsed in Python and inserted with
-- bound parameters; it never goes through the CSV reader, which would either
-- fail on structure it cannot flatten or quietly stringify the numbers.
CREATE TABLE IF NOT EXISTS factory.column_quality (
    column_name     TEXT           NOT NULL,
    rows_total      BIGINT         NOT NULL CHECK (rows_total >= 0),
    null_count      BIGINT         NOT NULL CHECK (null_count >= 0),
    null_percent    NUMERIC(8, 4)  NOT NULL,
    distinct_values INTEGER,
    distinct_capped BOOLEAN        NOT NULL,
    min_value       TEXT,
    max_value       TEXT,
    CONSTRAINT pk_factory_column_quality PRIMARY KEY (column_name),
    CONSTRAINT ck_factory_column_quality_share CHECK (
        null_percent >= 0 AND null_percent <= 100
    ),
    CONSTRAINT ck_factory_column_quality_nulls CHECK (null_count <= rows_total)
);

-- One row: the report's scalars need a home that is not a column of every
-- per-column row, and a CHECK on the key keeps the table to exactly one row.
CREATE TABLE IF NOT EXISTS factory.quality_meta (
    id             INTEGER NOT NULL,
    distinct_cap   INTEGER NOT NULL,
    conflict_count INTEGER NOT NULL,
    CONSTRAINT pk_factory_quality_meta PRIMARY KEY (id),
    CONSTRAINT ck_factory_quality_meta_single CHECK (id = 1),
    CONSTRAINT ck_factory_quality_meta_cap CHECK (distinct_cap > 0),
    CONSTRAINT ck_factory_quality_meta_conflicts CHECK (conflict_count >= 0)
);

-- borough_modzcta_conflicts is a list of {modzcta, boroughs[]} in the JSON
-- report; flattened to one row per pair so it can be joined to case_file.
CREATE TABLE IF NOT EXISTS factory.borough_conflict (
    modzcta TEXT NOT NULL,
    borough TEXT NOT NULL,
    CONSTRAINT pk_factory_borough_conflict PRIMARY KEY (modzcta, borough)
);

-- Provenance: which bytes produced which row counts, hashed by the loader on
-- the way in. This is what makes the sink checkable without trusting
-- run_manifest.json, and it is why re-running the sink is observable.
CREATE TABLE IF NOT EXISTS factory.load_run (
    sink_table  TEXT        NOT NULL,
    file_name   TEXT        NOT NULL,
    sha256      VARCHAR(64) NOT NULL,
    rows_loaded BIGINT      NOT NULL CHECK (rows_loaded >= 0),
    loaded_at   TIMESTAMPTZ NOT NULL,
    CONSTRAINT pk_factory_load_run PRIMARY KEY (sink_table),
    CONSTRAINT ck_factory_load_run_hash CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

-- A reader-friendly view of the one thing Stage 1 exists to answer, kept as a
-- view (no storage, no freshness question) because it only re-groups rows that
-- already live in this schema.
CREATE OR REPLACE VIEW factory.v_median_duration_by_type AS
SELECT complaint_type,
       status,
       COUNT(*) AS cases_with_duration,
       AVG(duration_seconds) AS average_seconds,
       PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY duration_seconds)
           AS median_seconds
FROM factory.case_file
WHERE duration_seconds IS NOT NULL
GROUP BY complaint_type, status;
