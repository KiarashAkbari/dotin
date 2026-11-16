-- 001_capstone_schema.sql
-- The capstone schema, and the column audit as the database sees it.
--
-- Nothing in stage 4 writes to `olist`: the source schema is stage 2's and is read
-- only. Everything this stage builds -- the frame, the label, the audit mirror, the
-- as-of functions -- lives in `capstone`, so a reader can audit the origin of a number
-- by looking at one schema (ROADMAP phase 3 item 3).
--
-- Idempotent by contract (see sql/README.md): every object here is IF NOT EXISTS, and
-- every view/function in the later files is CREATE OR REPLACE, so re-running the
-- directory repairs a half-built schema instead of failing on the second statement.

CREATE SCHEMA IF NOT EXISTS capstone;

-- The audit mirror. `docs/evidence/column_audit.csv` is the tracked source of truth;
-- this table is that file inside the database, and the phase-4 loader
-- (`capstone.features.build`, still marked pending) is what copies the rows in. It exists so
-- the guard is checkable in SQL, by somebody who never opens Python:
-- `SELECT count(*) FROM capstone.v_audit_rejections` answers "how many columns may not enter
-- the model" without a Python dependency.
--
-- The table starts empty, and empty means refused: `capstone.require_allowed` asks whether a
-- row says `allowed`, so a frame applied against an unloaded audit is rejected rather than
-- waved through. Failing closed is the only safe direction for a guard nobody has run yet.
--
-- The constraints are the CSV's rules, enforced on the way in rather than assumed:
-- a verdict outside the three words, and a column marked `allowed` that was not
-- available at the decision moment. The second one is the interesting constraint: it is
-- the brief's whole guard, expressed as one CHECK.
CREATE TABLE IF NOT EXISTS capstone.column_audit (
    "table"      TEXT    NOT NULL,
    "column"     TEXT    NOT NULL,
    available_at TEXT    NOT NULL,
    at_approval  BOOLEAN NOT NULL,
    source       TEXT    NOT NULL,
    verdict      TEXT    NOT NULL,
    CONSTRAINT pk_column_audit PRIMARY KEY ("table", "column"),
    CONSTRAINT ck_column_audit_verdict
        CHECK (verdict IN ('allowed', 'forbidden', 'unverified')),
    CONSTRAINT ck_column_audit_allowed_at_approval
        CHECK (verdict <> 'allowed' OR at_approval),
    -- an `allowed` row with no source is a verdict nobody can defend
    CONSTRAINT ck_column_audit_source_present
        CHECK (length(btrim(source)) > 0)
);

-- Provenance of the last build: how many audit rows the frame was checked against, and
-- which DDL the columns came from. A frame built against an empty audit table is the
-- failure mode this row documents.
CREATE TABLE IF NOT EXISTS capstone.build_run (
    built_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    audit_rows     INTEGER     NOT NULL,
    allowed_rows   INTEGER     NOT NULL,
    feature_view   TEXT        NOT NULL,
    feature_count  INTEGER     NOT NULL,
    source_tables  INTEGER     NOT NULL,
    CONSTRAINT ck_build_run_audit_rows CHECK (audit_rows > 0)
);
