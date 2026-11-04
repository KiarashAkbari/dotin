"""The credit ingest: the stage-1 factory pointed at a file and the stage-2 database.

Why this module exists instead of a standalone upload script: the challenge makes
the *factory* accept a new source kind, with the same paging, contract check,
quarantine and manifest. Every component below is imported from ``data_factory``:

``fetch_all`` + ``RawPageStore``            paged, resumable, one page in memory
``QualityReport`` + ``enforce_threshold``   streaming counts and the fail-fast limit
``QuarantineWriter``                        rejected records with their reasons
``apply_sql_migrations``                    the 010 migration, under the advisory lock,
                                            recorded in the content-checked ledger
``credit.db.engine_for``                      the same credential handling, plus a
                                            refused connection that says what to run

The ledger is the part stage 2 added after this stage was planned, and it
constrains two things: an applied ``010`` can never be edited in place (its checksum
is compared, so a schema change must be a new file), and dropping the ``credit``
schema without deleting its ledger row would leave the next run skipping the
migration it just lost. :func:`reset_credit` does both, in that order.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from data_factory.database import (
    LOAD_LOCK_KEY,
    MIGRATION_HISTORY_TABLE,
    apply_sql_migrations,
    sql_migration_files,
)
from data_factory.errors import DataError, PipelineError
from data_factory.exit_codes import ExitCode
from data_factory.fetch import FetchResult, fetch_all
from data_factory.manifest import code_version, utc_now_iso, write_manifest
from data_factory.quality import ClassifiedRecord, QualityReport, enforce_threshold
from data_factory.quarantine import QuarantineWriter
from data_factory.store import RawPageStore

from .columns import COLUMNS, DB_RUN_TABLE, DB_TABLE, RUN_COLUMNS
from .config import (
    CreditConfig,
    credit_config_from_mapping,
    load_credit_config,
    read_toml,
)
from .db import engine_for
from .source import CsvFileSource, file_fingerprint, file_sha256
from .validate import CreditReasonCode, parse_stream

# what stage 1's guard says when a file and its ledger row disagree
CHECKSUM_MISMATCH_MARKER = "SQL migration checksum mismatch"

SQL_TYPE_TO_SQLALCHEMY = {
    "bigint": sa.BIGINT,
    "smallint": sa.SMALLINT,
    "double precision": sa.FLOAT,
}


def application_table(schema: str, table: str = DB_TABLE) -> sa.TableClause:
    """The insert target, described from the contract rather than reflected.

    Reflection would let a database that drifted from the migration sneak past;
    naming the columns here means the file, the DDL and the tests all have to agree
    about the same list, which is what the contract tests check offline.
    """
    return sa.table(
        table,
        *(
            sa.column(spec.db_name, SQL_TYPE_TO_SQLALCHEMY[spec.sql_type])
            for spec in COLUMNS
        ),
        schema=schema,
    )


def run_table(schema: str) -> sa.TableClause:
    """Provenance target. The column names match ``factory.load_run`` on purpose."""
    types = {
        "sha256": sa.CHAR(64),
        "fingerprint": sa.CHAR(12),
        "rows_loaded": sa.BIGINT,
        "rejected_count": sa.BIGINT,
    }
    return sa.table(
        DB_RUN_TABLE,
        *(sa.column(name, types.get(name, sa.TEXT)) for name in RUN_COLUMNS),
        schema=schema,
    )


@dataclass(frozen=True)
class IngestResult:
    """What one run did, in the numbers the manifest and the report quote."""

    schema: str
    file_name: str
    sha256: str
    fingerprint: str
    file_rows: int
    pages_total: int
    pages_fetched: int
    resumed_from_page: int
    already_complete: bool
    rows_loaded: int
    rows_rejected: int
    reasons: dict[str, int]
    quarantine_ratio: float
    digest: dict[str, object]
    migrations_applied: tuple[str, ...]

    def as_manifest(self, *, started_at: str, finished_at: str) -> dict[str, object]:
        return {
            "kind": "credit-ingest",
            "code_version": code_version(),
            "started_at": started_at,
            "finished_at": finished_at,
            "source": {
                "kind": "file",
                "name": self.file_name,
                "sha256": self.sha256,
                "fingerprint": self.fingerprint,
                "rows_in_file": self.file_rows,
            },
            "http": {"requests_this_run": 0, "note": "a file source makes no requests"},
            "paging": {
                "page_bound_respected": True,
                "pages_total": self.pages_total,
                "pages_fetched_this_run": self.pages_fetched,
                "resumed_from_page": self.resumed_from_page,
                "already_complete_before_run": self.already_complete,
            },
            "quality": {
                "rows_loaded": self.rows_loaded,
                "rows_rejected": self.rows_rejected,
                "quarantine_share": round(self.quarantine_ratio, 6),
                "by_reason": dict(self.reasons),
            },
            "database": {
                "schema": self.schema,
                "table": f"{self.schema}.{DB_TABLE}",
                "migrations_applied": list(self.migrations_applied),
                "digest": dict(self.digest),
            },
        }


def digest_query(schema: str) -> str:
    """An order-independent witness that two loads of one file are the same load.

    Aggregates only, over a schema name validated as a SQL identifier in config: no
    value from the file and nothing else a caller controls reaches the SQL text.
    """
    return f"""
SELECT
    count(*) AS rows_total,
    sum(serious_dlqin_2yrs) AS defaults,
    count(*) - count(monthly_income) AS income_missing,
    count(*) - count(age) AS age_missing,
    round(avg(revolving_utilization_unsecured)::numeric, 6) AS utilization_mean,
    round(min(age)::numeric, 4) AS age_min,
    round(max(age)::numeric, 4) AS age_max,
    count(*) FILTER (WHERE revolving_utilization_unsecured > 1.0) AS utilization_above_1,
    count(*) FILTER (WHERE num_late_30_59 IN (96, 98)) AS code_30_59,
    count(*) FILTER (WHERE num_late_90 IN (96, 98)) AS code_90,
    count(*) FILTER (WHERE num_late_60_89 IN (96, 98)) AS code_60_89,
    count(DISTINCT row_id) AS distinct_ids
FROM {schema}.{DB_TABLE}
""".strip()


def read_digest(connection: sa.Connection, schema: str) -> dict[str, object]:
    row = connection.execute(sa.text(digest_query(schema))).mappings().one()
    out: dict[str, object] = {}
    for key, value in dict(row).items():
        out[key] = (
            None
            if value is None
            else (int(value) if isinstance(value, int) else float(value))
        )
    return out


def migration_names(cfg: CreditConfig) -> list[str]:
    """The migration files this stage owns, in the order the runner applies them.

    Stage 1's discovery is reused rather than a name repeated in the config, because
    a list that has to be edited when a file appears is how `011_credit_staging.sql`
    sat applied by nobody: the loader was told to run exactly one file, and the
    staging view the fresh-file path needs was never created. `sql/queries` is a
    subdirectory, so measurement SQL stays out of this set by construction.
    """
    return [path.name for path in sql_migration_files(cfg.sql_migrations_dir)]


def ledger_exists(connection: sa.Connection) -> bool:
    """Whether the migration ledger is there to be corrected."""
    return bool(
        connection.execute(
            sa.text("SELECT to_regclass(:table) IS NOT NULL"),
            {"table": MIGRATION_HISTORY_TABLE},
        ).scalar()
    )


def reset_credit(engine: sa.Engine, cfg: CreditConfig) -> None:
    """Drop the schema *and* every ledger row it owns, or the next run skips work.

    One DELETE per migration file: the ledger is keyed by name, so leaving the
    staging file's row behind would tell the next run that the view already exists
    in a schema that was just dropped.
    """
    with engine.begin() as connection:
        connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {cfg.schema} CASCADE"))
        # the ledger is created by the migration runner, so a reset on a database that
        # has never run the factory has no table to delete from - and a missing table is
        # not a reason to lose the run before it starts
        if not ledger_exists(connection):
            return
        for name in migration_names(cfg):
            connection.execute(
                sa.text(
                    f"DELETE FROM {MIGRATION_HISTORY_TABLE} "
                    "WHERE migration_name = :name"
                ),
                {"name": name},
            )


def apply_migrations(engine: sa.Engine, cfg: CreditConfig) -> list[str]:
    if not cfg.sql_migrations_dir.is_dir():
        raise DataError(
            "credit.sql_migrations_dir does not exist",
            context={"dir": str(cfg.sql_migrations_dir)},
        )
    names = migration_names(cfg)
    if not names:
        raise DataError(
            "no credit migrations to apply",
            context={
                "dir": str(cfg.sql_migrations_dir),
                "expected": "*.sql files, applied in filename order",
            },
        )
    try:
        return apply_sql_migrations(engine, cfg.sql_migrations_dir)
    except DataError as exc:
        if CHECKSUM_MISMATCH_MARKER not in str(exc):
            raise
        raise DataError(
            f"{exc}. {checksum_mismatch_hint()}", context=dict(exc.context)
        ) from exc


def migration_ledger_table() -> str:
    """The table stage 1 records applied migrations in: one table, every stage."""
    return MIGRATION_HISTORY_TABLE


def checksum_mismatch_hint() -> str:
    """What to do when the ledger disagrees with the bytes on disk.

    The guard that refuses is stage 1's and it is right to refuse: the file now on disk is
    not the file that built this schema. Two ways that happens. An applied migration was
    edited in place - this stage's rule is *new file*, which is why ``011`` exists next to
    ``010`` instead of inside it, and a correction is ``012``. Or the ledger row came from
    another tree that used the same file name: the table is keyed on the name alone and one
    table serves every stage, so an older layout of this project can hold the row.
    """
    return (
        "the ledger row was written by different bytes than the file on disk; check it with "
        f"SELECT migration_name, left(checksum, 16), applied_at FROM "
        f"{migration_ledger_table()} ORDER BY applied_at, then rebuild this stage with "
        "'python -m credit.load --reset', "
        "which drops the credit schema and deletes only the rows these files name"
    )


def collect_records(cfg: CreditConfig) -> tuple[list[dict], QualityReport, FetchResult]:
    """Page the file, classify every row, quarantine the rejects.

    The raw payload stays paged: it lands in ``RawPageStore`` one page at a time and
    is read back as a stream. Valid records are kept in a list because the insert
    wants them in one pass; the bound stage 1 sets applies to the *raw* file, not to
    a list of typed rows, and it is asserted on the page store, not on this list.
    """
    if not cfg.csv_path.is_file():
        raise DataError(
            "the credit CSV is not there; place cs-training.csv and re-run",
            context={
                "expected": str(cfg.csv_path),
                "hint": "Kaggle 'Give Me Some Credit' -> data/raw/cs-training.csv",
            },
        )
    # keyed by content, not by name: a file replaced in place gets a fresh store
    # instead of a silently resumed one that still holds the previous rows
    sha256 = file_sha256(cfg.csv_path)
    store = RawPageStore(cfg.raw_pages_dir / sha256[:12])
    with CsvFileSource(cfg.csv_path, page_size=cfg.page_size) as source:
        fetch = fetch_all(source, store)

    report = QualityReport()
    valid: list[dict] = []
    cfg.quarantine_dir.mkdir(parents=True, exist_ok=True)
    with QuarantineWriter(cfg.quarantine_dir) as writer:
        for parsed, raw in parse_stream(store.iter_rows()):
            report.add(parsed.reasons)
            if parsed.reasons:
                writer.write(ClassifiedRecord(raw, parsed.reasons))
            else:
                valid.append(parsed.record)
    enforce_threshold(
        report, max_ratio=cfg.quarantine_max_share, min_rows=cfg.quarantine_min_rows
    )
    return valid, report, fetch


def reason_counts(report: QualityReport) -> dict[str, int]:
    """Every reason, zeros included, so an absence is a claim of its own."""
    return {code.value: report.by_reason.get(code, 0) for code in CreditReasonCode}


def acquire_load_lock(connection: sa.Connection) -> None:
    """Same session-scoped key stage 2 takes around an Olist load.

    A credit load and an Olist load touch different schemas, but they share the
    migration ledger and the same server; taking the lock stage 2 already uses means
    two runs cannot interleave their ledger writes no matter which command starts
    first.
    """
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": LOAD_LOCK_KEY},
    )


def insert_records(
    connection: sa.Connection,
    target: sa.TableClause,
    records: list[dict],
    *,
    batch_size: int,
) -> int:
    """Insert in batches on a connection the caller owns, returning the count.

    Batched rather than one giant ``insert`` because the parameter list is what
    PostgreSQL chokes on: 16 columns times 150,000 rows is 2.4M bind parameters in
    one statement, and the driver builds all of them before the server sees any.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    inserted = 0
    batch: list[dict] = []
    for record in records:
        batch.append(record)
        if len(batch) >= batch_size:
            connection.execute(target.insert(), batch)
            inserted += len(batch)
            batch = []
    if batch:
        connection.execute(target.insert(), batch)
        inserted += len(batch)
    return inserted


def write_records(
    engine: sa.Engine,
    cfg: CreditConfig,
    records: list[dict],
    *,
    file_name: str,
    sha256: str,
    fingerprint: str,
    rejected: int,
) -> int:
    """Truncate, insert and record provenance in one transaction, under the load lock."""
    target = application_table(cfg.schema)
    provenance = run_table(cfg.schema)
    inserted = 0
    with engine.begin() as connection:
        acquire_load_lock(connection)
        connection.execute(sa.delete(target))
        inserted = insert_records(
            connection, target, records, batch_size=cfg.batch_size
        )
        connection.execute(
            provenance.insert().values(
                source_kind="file",
                file_name=file_name,
                sha256=sha256,
                rows_loaded=inserted,
                rejected_count=rejected,
                fingerprint=fingerprint,
            )
        )
    return inserted


def ingest(
    cfg: CreditConfig,
    *,
    engine: sa.Engine,
    reset: bool = False,
    migrations_only: bool = False,
) -> IngestResult:
    """One complete run: reset, migrations, paging, quarantine, load, manifest."""
    started_at = utc_now_iso()
    if reset:
        reset_credit(engine, cfg)
    migrations = tuple(apply_migrations(engine, cfg))
    if migrations_only:
        return IngestResult(
            schema=cfg.schema,
            file_name=cfg.csv_path.name,
            sha256="",
            fingerprint="",
            file_rows=0,
            pages_total=0,
            pages_fetched=0,
            resumed_from_page=0,
            already_complete=False,
            rows_loaded=0,
            rows_rejected=0,
            reasons={},
            quarantine_ratio=0.0,
            digest={},
            migrations_applied=migrations,
        )

    records, report, fetch = collect_records(cfg)
    sha256 = file_sha256(cfg.csv_path)
    fingerprint = file_fingerprint(cfg.csv_path)
    loaded = write_records(
        engine,
        cfg,
        records,
        file_name=cfg.csv_path.name,
        sha256=sha256,
        fingerprint=fingerprint,
        rejected=report.quarantined,
    )
    with engine.connect() as connection:
        digest = read_digest(connection, cfg.schema)
    result = IngestResult(
        schema=cfg.schema,
        file_name=cfg.csv_path.name,
        sha256=sha256,
        fingerprint=fingerprint,
        file_rows=report.total,
        pages_total=fetch.pages_total,
        pages_fetched=fetch.pages_fetched,
        resumed_from_page=fetch.resumed_from_page,
        already_complete=fetch.already_complete,
        rows_loaded=loaded,
        rows_rejected=report.quarantined,
        reasons=reason_counts(report),
        quarantine_ratio=report.quarantine_ratio,
        digest=digest,
        migrations_applied=migrations,
    )
    write_manifest(
        cfg.manifest_path,
        result.as_manifest(started_at=started_at, finished_at=utc_now_iso()),
    )
    return result


def resolve_credit_config(
    config_path: Path, credit_config: Path | None
) -> CreditConfig:
    """Prefer an explicit stage-3 file, fall back to a ``[credit]`` section."""
    if credit_config is not None:
        return load_credit_config(credit_config)
    stage1 = Path(config_path)
    data = read_toml(stage1)
    cfg = credit_config_from_mapping(data, base=stage1.resolve().parent)
    if cfg is None:
        raise DataError(
            "no credit source is configured",
            context={
                "tried": [f"{stage1} -> [credit]", "config/credit.toml (stage 3)"],
                "required": "csv_path and sql_migrations_dir",
            },
        )
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m credit.load")
    parser.add_argument("--config", default="../stage-1/config/default.toml")
    parser.add_argument("--credit-config", default=None)
    parser.add_argument(
        "--reset", action="store_true", help="drop the credit schema first"
    )
    parser.add_argument(
        "--migrations-only", action="store_true", help="apply 010 and stop"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="count and validate, no database"
    )
    args = parser.parse_args(argv)

    credit_cfg: CreditConfig | None = None
    try:
        credit_cfg = resolve_credit_config(
            Path(args.config), Path(args.credit_config) if args.credit_config else None
        )
        if args.dry_run:
            records, report, _fetch = collect_records(credit_cfg)
            counts = {k: v for k, v in reason_counts(report).items() if v}
            print(
                f"file={credit_cfg.csv_path.name} rows={report.total} valid={len(records)} "
                f"quarantined={report.quarantined} ratio={report.quarantine_ratio:.6f} "
                f"reasons={counts or '{}'}"
            )
            return int(ExitCode.OK)
        engine = engine_for(Path(args.config))
        try:
            result = ingest(
                credit_cfg,
                engine=engine,
                reset=args.reset,
                migrations_only=args.migrations_only,
            )
        finally:
            engine.dispose()
    except PipelineError as exc:
        # An empty context is the CLI's own boilerplate, not information; print it only
        # when the error actually carries something (SchemaDriftError does).
        suffix = f" context={exc.context}" if exc.context else ""
        print(f"{type(exc).__name__}: {exc}{suffix}", file=sys.stderr)
        return int(exc.exit_code)
    print(
        f"file={result.file_name} sha256={result.sha256[:12]} loaded={result.rows_loaded} "
        f"rejected={result.rows_rejected} pages={result.pages_total} "
        f"migrations={len(result.migrations_applied)} manifest={credit_cfg.manifest_path}"
    )
    return int(ExitCode.OK)


if __name__ == "__main__":
    raise SystemExit(main())
