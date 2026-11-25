"""PostgreSQL destination for the existing data factory package."""


from __future__ import annotations



import csv
import hashlib
import json
import os
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit



import sqlalchemy as sa
from sqlalchemy.engine import URL



from .config import Config
from .errors import (
    ConfigError,
    DatabaseDataError,
    DatabaseSchemaError,
    DatabaseUnavailableError,
    DataError,
    PipelineError,
)
from .outputs import (
    COUNT_PREFIX,
    DATASET_FILENAME,
    FALSE_TEXT,
    RATE_PREFIX,
    TRUE_TEXT,
    WIDE_FILENAME,
    sha256_file,
)
from .profile import REPORT_FILENAME
from .publish import verify_outputs



OLIST_SCHEMA = "olist"
OLIST_CSV_FILES = {
    "product_category_name_translation": "product_category_name_translation.csv",
    "customers": "olist_customers_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "products": "olist_products_dataset.csv",
    "geolocation": "olist_geolocation_dataset.csv",
    "orders": "olist_orders_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "order_reviews": "olist_order_reviews_dataset.csv",
}
OLIST_TABLES = tuple(OLIST_CSV_FILES)
# Built once from the module constants above, never from config or file content.
# One TRUNCATE over the whole set checks the foreign keys between these tables as
# a set, so no row-by-row referential check runs. A row-by-row DELETE fires one RI
# check per parent row, and none of the referencing columns is indexed, so it
# scanned the child table once per deleted parent row (quadratic on a reload).
# A table outside this list that references one of them makes TRUNCATE fail
# loudly instead of silently cascading into another stage's data.
OLIST_TRUNCATE_SQL = (
    "TRUNCATE TABLE "
    + ", ".join(f"{OLIST_SCHEMA}.{table}" for table in OLIST_TABLES)
    + " RESTART IDENTITY"
)

OLIST_MATERIALIZED_VIEWS = (
    "mv_q1_monthly_category_sales",
    "mv_q3_delay_satisfaction",
    "mv_q4_seller_dashboard",
    "mv_q5_payment_distribution",
)


def clear_olist_tables(connection: sa.Connection) -> None:
    """Empty every Olist table in one set-based statement, inside the caller's transaction.

    TRUNCATE takes an ACCESS EXCLUSIVE lock on each table until the transaction
    ends, so everything else the load does with these tables (migrations, view
    refreshes) has to run on this same connection; a second connection would wait
    on this one forever.
    """
    connection.execute(sa.text(OLIST_TRUNCATE_SQL))


def refresh_olist_snapshots(connection: sa.Connection) -> None:
    """Refresh materialized views that already exist after replacing data."""
    existing = {
        row.matviewname
        for row in connection.execute(
            sa.text(
                """
                SELECT matviewname
                FROM pg_matviews
                WHERE schemaname = :schema
                """
            ),
            {"schema": OLIST_SCHEMA},
        )
    }
    for view in OLIST_MATERIALIZED_VIEWS:
        if view in existing:
            connection.execute(
                sa.text(f"REFRESH MATERIALIZED VIEW {OLIST_SCHEMA}.{view}")
            )

GEOLOCATION_KEY = (
    "geolocation_zip_code_prefix",
    "geolocation_lat",
    "geolocation_lng",
    "geolocation_city",
    "geolocation_state",
)
COLUMN_ALIASES = {
    "product_name_length": "product_name_lenght",
    "product_description_length": "product_description_lenght",
}

MIGRATION_HISTORY_TABLE = "public.data_factory_migrations"
MIGRATION_LOCK_KEY = "data_factory:migrations"
LOAD_LOCK_KEY = "data_factory:load"



# SQLSTATE class 42 is "syntax error or access rule violation"; 42P01 (undefined
# table), 3F000 (undefined schema) and 42883 (undefined function) all mean the
# migrations that create the object were not applied, which is a setup problem.
MISSING_OBJECT_SQLSTATES = frozenset({"42P01", "3F000", "42883", "42704"})


def _first_line(exc: BaseException) -> str:
    text = str(getattr(exc, "orig", None) or exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def translate_database_error(exc: sa.exc.SQLAlchemyError) -> PipelineError:
    """Map a driver error to the project hierarchy, so it gets an exit code.

    Unreachable server or lost connection -> DatabaseUnavailableError (69, the
    same class as an unreachable API); a missing table, schema or function ->
    DatabaseSchemaError (78, migrations not applied); a rejected value or
    constraint -> DatabaseDataError (65). Anything else stays a PipelineError
    (1) but still prints one readable line instead of a traceback.
    """
    message = _first_line(exc)
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    context = {"error": message}
    if sqlstate:
        context["sqlstate"] = sqlstate
    if isinstance(exc, sa.exc.NoSuchTableError):
        return DatabaseSchemaError(
            "Database table not found; apply the SQL migrations first",
            context={"table": str(exc)},
        )
    if isinstance(exc, (sa.exc.OperationalError, sa.exc.InterfaceError)) and (
        sqlstate is None or sqlstate.startswith("08") or sqlstate.startswith("57P")
    ):
        return DatabaseUnavailableError("Database is unavailable", context=context)
    if sqlstate in MISSING_OBJECT_SQLSTATES:
        return DatabaseSchemaError(
            "Database object not found; apply the SQL migrations first", context=context
        )
    if isinstance(exc, (sa.exc.IntegrityError, sa.exc.DataError)):
        return DatabaseDataError("Database rejected the data", context=context)
    return PipelineError("Database error", context=context)


def build_database_url(cfg: Config) -> URL:
    """Keep the password outside versioned configuration."""
    if cfg.database is None:
        raise ConfigError("Missing config section: [database]")
    parsed = urlsplit(cfg.database.url)
    return URL.create(
        drivername=parsed.scheme,
        username=parsed.username,
        password=os.environ.get(cfg.database.password_env),
        host=parsed.hostname,
        port=parsed.port,
        database=parsed.path.lstrip("/"),
    )



def read_csv_batches(path: Path, batch_size: int) -> Iterator[list[dict]]:
    """Read bounded batches and reject malformed CSV rows."""
    if isinstance(batch_size, bool) or batch_size < 1:
        raise DataError("CSV batch size must be positive")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise DataError("CSV header is missing or contains duplicate columns")
        batch = []
        for line, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise DataError("Malformed CSV row", context={"file": str(path), "line": line})
            batch.append({key: value if value != "" else None for key, value in row.items()})
            if len(batch) == batch_size:
                yield batch
                batch = []
        if batch:
            yield batch



def deduplicate_geolocation(rows: Iterator[list[dict]]) -> Iterator[list[dict]]:
    """Deduplicate raw observations; the distinct-key set grows with input."""
    seen = set()
    for batch in rows:
        unique_rows = []
        for row in batch:
            key = tuple(row.get(column) for column in GEOLOCATION_KEY)
            if key not in seen:
                seen.add(key)
                unique_rows.append(row)
        if unique_rows:
            yield unique_rows



def _convert(value, column):
    """One typed contract for every CSV column this project loads.



    The Boolean branch exists for Stage 1's own output: cases.csv writes
    true/false text (outputs.format_cell) and a Boolean column must not rely on
    the server coercing that string, so the loader decides it here. No Olist
    table has a Boolean column, so the existing loads are unaffected.
    """
    if value is None:
        if not column.nullable:
            raise ValueError("Required value is empty")
        return None
    if isinstance(column.type, sa.Boolean):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text == TRUE_TEXT:
            return True
        if text == FALSE_TEXT:
            return False
        raise ValueError("Boolean value must be true or false")
    if isinstance(column.type, sa.DateTime):
        return datetime.fromisoformat(value)
    if isinstance(column.type, sa.Integer):
        return int(value)
    if isinstance(column.type, sa.Float):
        number = float(value)
        import math


        if not math.isfinite(number):
            raise ValueError("Non-finite number")
        return number
    if isinstance(column.type, sa.Numeric):
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("Non-finite number")
        return number
    return value



def load_csv(
    connection: sa.Connection,
    csv_path: Path,
    schema: str,
    table: str,
    batch_size: int,
    deduplicate: bool = False,
) -> dict[str, int]:
    """Reflect identifiers and bind typed values through SQLAlchemy Core."""
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise DataError("CSV file not found", context={"file": str(csv_path)})
    target = sa.Table(table, sa.MetaData(), schema=schema, autoload_with=connection)
    columns = [c for c in target.columns if c.identity is None and c.computed is None]
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        header = next(csv.reader(fh), [])
    source_names = {
        c.name: c.name if c.name in header else COLUMN_ALIASES.get(c.name, c.name) for c in columns
    }
    missing = [name for name in source_names.values() if name not in header]
    if missing:
        raise DataError(
            "CSV columns are missing", context={"file": str(csv_path), "columns": missing}
        )
    counts = {"raw": 0, "loaded": 0, "duplicates": 0}
    seen = set()
    for batch in read_csv_batches(csv_path, batch_size):
        converted = []
        for row in batch:
            counts["raw"] += 1
            if deduplicate:
                key = tuple(row.get(name) for name in GEOLOCATION_KEY)
                if key in seen:
                    counts["duplicates"] += 1
                    continue
                seen.add(key)
            record = {}
            for column in columns:
                try:
                    record[column.name] = _convert(row[source_names[column.name]], column)
                except (ValueError, TypeError, InvalidOperation) as exc:
                    raise DataError(
                        "CSV value violates the typed load contract",
                        context={
                            "file": str(csv_path),
                            "line": counts["raw"] + 1,
                            "column": column.name,
                        },
                    ) from exc
            converted.append(record)
        if converted:
            connection.execute(sa.insert(target), converted)
            counts["loaded"] += len(converted)
    return counts



def load_olist(
    cfg: Config, connection: sa.Connection | None = None
) -> dict[str, dict[str, int]]:
    """Replace Olist tables atomically; dispose an owned engine on every path."""
    if cfg.database is None:
        raise ConfigError("Missing config section: [database]")

    def _load(target_connection: sa.Connection) -> dict[str, dict[str, int]]:
        clear_olist_tables(target_connection)
        return {
            table: load_csv(
                target_connection,
                cfg.database.olist_csv_dir / OLIST_CSV_FILES[table],
                OLIST_SCHEMA,
                table,
                cfg.database.batch_size,
                deduplicate=(table == "geolocation"),
            )
            for table in OLIST_TABLES
        }

    if connection is not None:
        return _load(connection)

    engine = sa.create_engine(build_database_url(cfg))
    try:
        with engine.begin() as owned:
            return _load(owned)
    finally:
        engine.dispose()



def sql_migration_files(sql_dir: Path) -> list[Path]:
    """Return direct, lexically ordered SQL migration files."""
    sql_dir = Path(sql_dir)
    if not sql_dir.is_dir():
        raise DataError(
            "SQL migrations directory not found",
            context={"dir": str(sql_dir)},
        )
    return sorted(path for path in sql_dir.glob("*.sql") if path.is_file())



def _migration_checksum(sql: str) -> str:
    """Return a stable content checksum for one SQL migration file."""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()



def _ensure_migration_history(connection: sa.Connection) -> None:
    """Create the migration-history table if it does not already exist."""
    connection.execute(
        sa.text(
            f"""
            CREATE TABLE IF NOT EXISTS {MIGRATION_HISTORY_TABLE} (
                migration_name TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )



# Which numbered migration builds what. load-database applies the groups in its
# own order; a reset forgets exactly the ledger rows of the schema it drops.
BASE_MIGRATIONS = frozenset({"001", "002", "003", "004"})
SNAPSHOT_MIGRATIONS = frozenset({"005", "006", "007", "008"})
SINK_MIGRATIONS = frozenset({"009"})


def is_base_migration(path: Path) -> bool:
    """001-003 build the schema, views and functions the load writes into.

    004 is stage 2's index migration (the dashboard-query indexes). It belongs to
    the base group so the indexes exist before the load, the same way on a fresh
    and on a repeated run; the load and the index build share one transaction.
    """
    return Path(path).name[:3] in BASE_MIGRATIONS


def is_snapshot_migration(path: Path) -> bool:
    return Path(path).name[:3] in SNAPSHOT_MIGRATIONS


def is_sink_migration(path: Path) -> bool:
    """The factory sink schema is its own group, applied right before its data.

    It is deliberately not folded into the base group: the sink is a separate
    destination, and a run that only rebuilds Olist must not need it.
    """
    return Path(path).name[:3] in SINK_MIGRATIONS


def is_olist_migration(path: Path) -> bool:
    """Everything that lives in the olist schema: the base and snapshot groups."""
    return is_base_migration(path) or is_snapshot_migration(path)


def forget_migrations(connection: sa.Connection, include) -> list[str]:
    """Delete the ledger rows of the migrations `include` selects, and only those.

    The ledger is shared: stage 3 records its own 010/011 in it, and the sink
    records 009. Dropping the whole table on an Olist reset erased their history
    too. Names are matched in Python with the same predicates load-database
    applies, then deleted with one bound parameter.
    """
    exists = connection.scalar(
        sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": MIGRATION_HISTORY_TABLE}
    )
    if not exists:
        return []
    names = connection.execute(
        sa.text(f"SELECT migration_name FROM {MIGRATION_HISTORY_TABLE} ORDER BY migration_name")
    ).scalars()
    forgotten = [name for name in names if include(Path(name))]
    if forgotten:
        connection.execute(
            sa.text(f"DELETE FROM {MIGRATION_HISTORY_TABLE} WHERE migration_name = ANY(:names)"),
            {"names": forgotten},
        )
    return forgotten


def reset_olist_schema(connection: sa.Connection) -> list[str]:
    """Drop the complete Olist schema so a rebuild starts from an empty database.

    Only the ledger rows of the olist migrations (base and snapshot groups) are
    forgotten, so 001-008 run again and every other stage keeps its history. It
    runs in the caller's transaction, so a load that fails after the reset rolls
    the drop back too and the previous data survives. Returns the forgotten names.
    """
    connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {OLIST_SCHEMA} CASCADE"))
    return forget_migrations(connection, is_olist_migration)



def apply_sql_migrations(
    bind: sa.Engine | sa.Connection,
    sql_dir: Path,
    include=None,
) -> list[str]:
    """Apply selected SQL migrations once, safely under an advisory lock.

    Each file is identified by name and content checksum. A previously applied
    file with the same checksum is skipped; a changed file fails explicitly.
    The advisory lock serializes concurrent CLI runs so two processes cannot
    create the same schema or table at the same time.

    `bind` is an Engine or a Connection. An Engine gets a transaction of its own
    that commits here. A Connection is used as it is: the files run inside the
    caller's transaction, the transaction-level advisory lock lasts until the
    caller commits or rolls back, and nothing is committed here. That is what
    `load-database` needs: its load holds locks on the olist tables, and a
    migration that reads them from a second connection would wait on the first
    one forever.
    """
    files = sql_migration_files(sql_dir)
    if include is not None:
        files = [path for path in files if include(path)]

    if isinstance(bind, sa.Connection):
        return _apply_migration_files(bind, files)
    with bind.begin() as connection:
        return _apply_migration_files(connection, files)


def _apply_migration_files(connection: sa.Connection, files: list[Path]) -> list[str]:
    applied: list[str] = []
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
        {"lock_key": MIGRATION_LOCK_KEY},
    )
    _ensure_migration_history(connection)

    raw_connection = connection.connection.driver_connection
    for path in files:
        sql = path.read_text(encoding="utf-8")
        checksum = _migration_checksum(sql)

        existing_checksum = connection.execute(
            sa.text(
                f"""
                SELECT checksum
                FROM {MIGRATION_HISTORY_TABLE}
                WHERE migration_name = :name
                """
            ),
            {"name": path.name},
        ).scalar()

        if existing_checksum is not None:
            if existing_checksum != checksum:
                raise DataError(
                    "SQL migration checksum mismatch",
                    context={
                        "file": str(path),
                        "expected": existing_checksum,
                        "actual": checksum,
                    },
                )
            continue

        try:
            with raw_connection.cursor() as cursor:
                cursor.execute(sql)
        except Exception as exc:
            raise DataError(
                "SQL migration failed",
                context={
                    "file": str(path),
                    "error": str(exc),
                },
            ) from exc

        connection.execute(
            sa.text(
                f"""
                INSERT INTO {MIGRATION_HISTORY_TABLE}
                    (migration_name, checksum)
                VALUES (:name, :checksum)
                """
            ),
            {"name": path.name, "checksum": checksum},
        )
        applied.append(path.name)

    return applied



FACTORY_SCHEMA = "factory"
SINK_CASE_TABLE = "case_file"
SINK_WIDE_TABLE = "complaint_area_month"
SINK_QUALITY_TABLE = "column_quality"
SINK_META_TABLE = "quality_meta"
SINK_CONFLICT_TABLE = "borough_conflict"
SINK_PROVENANCE_TABLE = "load_run"
SINK_TABLES = {
    SINK_CASE_TABLE: DATASET_FILENAME,
    SINK_WIDE_TABLE: WIDE_FILENAME,
    SINK_QUALITY_TABLE: REPORT_FILENAME,
}
NARROW_COLUMNS = (
    "modzcta",
    "complaint_type",
    "month_start",
    "complaint_count",
    "rate_per_10k",
    "population",
)
PROFILE_KEYS = (
    "rows",
    "null_count",
    "null_percent",
    "distinct_values",
    "distinct_capped",
    "min",
    "max",
)
WIDE_AREA_COLUMN = "modzcta"
WIDE_TYPE_COLUMN = "complaint_type"
WIDE_POPULATION_COLUMN = "population"
MONTH_PATTERN = re.compile(r"^(\d{4}-(?:0[1-9]|1[0-2]))$")



def _month_of(column_name: str, prefix: str) -> str:
    if not column_name.startswith(prefix):
        raise DataError("Unexpected sink column", context={"column": column_name})
    month = column_name[len(prefix) :]
    if MONTH_PATTERN.match(month) is None:
        raise DataError(
            "Sink month column is not a YYYY-MM label",
            context={"column": column_name},
        )
    return month



def sink_months(header) -> tuple[str, ...]:
    """Months of the delivered wide table, read from its header.



    A count without its rate (or the reverse) is a half-written artifact, so it
    is refused here rather than silently producing a table with missing months.
    """
    header = list(header)
    counts = [name for name in header if name.startswith(COUNT_PREFIX)]
    rates = [name for name in header if name.startswith(RATE_PREFIX)]
    if not counts:
        raise DataError(
            "Wide CSV has no monthly count columns",
            context={"columns": len(header), "counts": 0},
        )
    months = tuple(_month_of(name, COUNT_PREFIX) for name in counts)
    rate_months = tuple(_month_of(name, RATE_PREFIX) for name in rates)
    if months != rate_months:
        raise DataError(
            "Wide CSV count and rate columns do not match",
            context={"counts": list(months), "rates": list(rate_months)},
        )
    if len(set(months)) != len(months):
        raise DataError("Wide CSV repeats a month", context={"months": list(months)})
    return months



def narrow_wide_rows(row: dict, months) -> Iterator[dict]:
    """One wide row (area, type, every month) to one narrow row per month.



    An empty cell is a NULL here, the same rule read_csv_batches applies to the
    Olist files: a missing rate is unknown, and a missing count is refused
    below rather than stored as a zero that never happened.
    """
    row = {key: (None if value == "" else value) for key, value in row.items()}
    area = row.get(WIDE_AREA_COLUMN)
    complaint_type = row.get(WIDE_TYPE_COLUMN)
    if not area or not complaint_type:
        raise DataError(
            "Wide row is missing its area or complaint type",
            context={WIDE_AREA_COLUMN: area, WIDE_TYPE_COLUMN: complaint_type},
        )
    for month in months:
        count = row.get(f"{COUNT_PREFIX}{month}")
        if count is None:
            raise DataError(
                "Wide row has an empty month count",
                context={
                    WIDE_AREA_COLUMN: area,
                    WIDE_TYPE_COLUMN: complaint_type,
                    "month": month,
                },
            )
        yield {
            "modzcta": area,
            "complaint_type": complaint_type,
            "month_start": month,
            "complaint_count": count,
            "rate_per_10k": row.get(f"{RATE_PREFIX}{month}"),
            "population": row.get(WIDE_POPULATION_COLUMN),
        }



def _sink_target(connection: sa.Connection, table: str, required: tuple[str, ...]) -> sa.Table:
    """Reflect a sink table and refuse to write columns it does not have."""
    target = sa.Table(table, sa.MetaData(), schema=FACTORY_SCHEMA, autoload_with=connection)
    missing = [name for name in required if name not in target.columns]
    if missing:
        raise DataError(
            "Sink table is missing columns the loader writes",
            context={"table": f"{FACTORY_SCHEMA}.{table}", "columns": missing},
        )
    return target



def _insert_records(connection: sa.Connection, target: sa.Table, records: list[dict]) -> None:
    if records:
        connection.execute(sa.insert(target), records)



def _profile_bound(value):
    """Keep a profile bound text-comparable; a float must not meet a string."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return TRUE_TEXT if value else FALSE_TEXT
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    raise ValueError(f"Unsupported profile bound type: {type(value).__name__}")



def parse_quality_report(path: Path) -> dict:
    """Read the JSON quality report as JSON; the CSV reader never sees it.



    Every shape assumption is checked here so the inserts below deal only with
    typed values, and so a report written by an older Stage 1 fails as a
    contract problem instead of a column count mismatch.
    """
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise DataError("Data quality report is not readable", context={"file": str(path)}) from exc
    except json.JSONDecodeError as exc:
        raise DataError(
            "Data quality report is not valid JSON",
            context={"file": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        raise DataError("Data quality report must be a JSON object", context={"file": str(path)})


    columns = payload.get("columns")
    if not isinstance(columns, dict) or not columns:
        raise DataError("Data quality report has no column profiles", context={"file": str(path)})
    cap = payload.get("distinct_cap")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise DataError(
            "Data quality report distinct_cap must be an integer >= 1",
            context={"file": str(path), "distinct_cap": cap},
        )
    conflicts = payload.get("borough_modzcta_conflicts", [])
    if not isinstance(conflicts, list):
        raise DataError("borough_modzcta_conflicts must be a list", context={"file": str(path)})
    declared = payload.get("borough_modzcta_conflict_count", len(conflicts))
    if declared != len(conflicts):
        raise DataError(
            "Data quality report conflict count disagrees with its list",
            context={"file": str(path), "declared": declared, "listed": len(conflicts)},
        )


    profiles = []
    for name in sorted(columns):
        stats = columns[name]
        if not isinstance(stats, dict):
            raise DataError(
                "Column profile is not an object",
                context={"file": str(path), "column": name},
            )
        absent = [key for key in PROFILE_KEYS if key not in stats]
        if absent:
            raise DataError(
                "Column profile is incomplete",
                context={"file": str(path), "column": name, "missing": absent},
            )
        try:
            profiles.append(
                {
                    "column_name": name,
                    "rows_total": stats["rows"],
                    "null_count": stats["null_count"],
                    "null_percent": stats["null_percent"],
                    "distinct_values": stats["distinct_values"],
                    "distinct_capped": stats["distinct_capped"],
                    "min_value": _profile_bound(stats["min"]),
                    "max_value": _profile_bound(stats["max"]),
                }
            )
        except ValueError as exc:
            raise DataError(
                "Column profile holds a value the sink cannot store",
                context={"file": str(path), "column": name, "error": str(exc)},
            ) from exc


    pairs = []
    for entry in conflicts:
        boroughs = entry.get("boroughs") if isinstance(entry, dict) else None
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("modzcta"), str)
            or not isinstance(boroughs, (list, tuple))
            or not boroughs
        ):
            raise DataError(
                "Borough conflict entry is malformed",
                context={"file": str(path), "entry": entry},
            )
        for borough in boroughs:
            if not isinstance(borough, str) or not borough.strip():
                raise DataError(
                    "Borough conflict entry holds a bad borough",
                    context={"file": str(path), "modzcta": entry["modzcta"]},
                )
            pairs.append({"modzcta": entry["modzcta"].strip(), "borough": borough.strip()})


    return {
        "profiles": profiles,
        "conflicts": pairs,
        "conflict_entries": len(conflicts),
        "distinct_cap": cap,
        "conflict_count": len(pairs),
    }



def load_narrow_wide(connection: sa.Connection, csv_path: Path, batch_size: int) -> dict[str, int]:
    """Stream the wide CSV and insert it in narrow form, one row per month.



    A duplicate (area, type, month) is the database's to reject: the primary key
    exists, and because the sink is a single transaction a rejection leaves no
    partial state behind.
    """
    csv_path = Path(csv_path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        header = next(csv.reader(fh), [])
    months = sink_months(header)
    target = _sink_target(connection, SINK_WIDE_TABLE, NARROW_COLUMNS)
    counts = {"raw": 0, "loaded": 0, "months": len(months)}
    for batch in read_csv_batches(csv_path, batch_size):
        records = []
        for row in batch:
            counts["raw"] += 1
            for narrow in narrow_wide_rows(row, months):
                record = {}
                for name in NARROW_COLUMNS:
                    try:
                        record[name] = _convert(narrow[name], target.columns[name])
                    except (ValueError, TypeError, InvalidOperation) as exc:
                        raise DataError(
                            "Wide CSV value violates the typed load contract",
                            context={
                                "file": str(csv_path),
                                "line": counts["raw"] + 1,
                                "column": name,
                            },
                        ) from exc
                records.append(record)
        _insert_records(connection, target, records)
        counts["loaded"] += len(records)
    return counts



def load_quality_report(connection: sa.Connection, json_path: Path) -> dict[str, int]:
    """Insert the per-column profiles, the report scalars and the conflicts."""
    parsed = parse_quality_report(json_path)
    quality = _sink_target(
        connection,
        SINK_QUALITY_TABLE,
        (
            "column_name",
            "rows_total",
            "null_count",
            "null_percent",
            "distinct_values",
            "distinct_capped",
            "min_value",
            "max_value",
        ),
    )
    _insert_records(connection, quality, parsed["profiles"])
    meta = _sink_target(connection, SINK_META_TABLE, ("id", "distinct_cap", "conflict_count"))
    _insert_records(
        connection,
        meta,
        [
            {
                "id": 1,
                "distinct_cap": parsed["distinct_cap"],
                "conflict_count": parsed["conflict_count"],
            }
        ],
    )
    if parsed["conflicts"]:
        conflict = _sink_target(connection, SINK_CONFLICT_TABLE, ("modzcta", "borough"))
        _insert_records(connection, conflict, parsed["conflicts"])
    return {
        "raw": len(parsed["profiles"]),
        "loaded": len(parsed["profiles"]),
        "conflicts": parsed["conflict_entries"],
    }



def factory_sink_paths(curated_dir: Path) -> dict[str, Path]:
    """Resolve the three delivered files, before any connection is opened."""
    curated_dir = Path(curated_dir)
    if not curated_dir.is_dir():
        raise DataError("Stage 1 curated directory not found", context={"dir": str(curated_dir)})
    paths = {table: curated_dir / name for table, name in SINK_TABLES.items()}
    missing = sorted(str(path) for path in paths.values() if not path.is_file())
    if missing:
        raise DataError(
            "Stage 1 output is incomplete for the database sink",
            context={
                "files": missing,
                "hint": "run 'data-factory run' or 'data-factory clean' first",
            },
        )
    return paths



def truncate_factory(connection: sa.Connection) -> None:
    """Clear every sink table so a repeated load converges instead of growing.



    The table list is module constants, never config or file content, so this
    is a fixed statement and not string-built SQL from input.
    """
    tables = (
        SINK_CASE_TABLE,
        SINK_WIDE_TABLE,
        SINK_QUALITY_TABLE,
        SINK_META_TABLE,
        SINK_CONFLICT_TABLE,
        SINK_PROVENANCE_TABLE,
    )
    connection.execute(
        sa.text(
            "TRUNCATE TABLE "
            + ", ".join(f"{FACTORY_SCHEMA}.{table}" for table in tables)
            + " RESTART IDENTITY"
        )
    )



def _load_sink(
    connection: sa.Connection, cfg: Config, paths: dict[str, Path]
) -> dict[str, dict[str, int]]:
    results = {
        SINK_CASE_TABLE: load_csv(
            connection,
            paths[SINK_CASE_TABLE],
            FACTORY_SCHEMA,
            SINK_CASE_TABLE,
            cfg.database.batch_size,
        ),
        SINK_WIDE_TABLE: load_narrow_wide(
            connection,
            paths[SINK_WIDE_TABLE],
            cfg.database.batch_size,
        ),
        SINK_QUALITY_TABLE: load_quality_report(connection, paths[SINK_QUALITY_TABLE]),
    }
    provenance = _sink_target(
        connection,
        SINK_PROVENANCE_TABLE,
        (
            "sink_table",
            "file_name",
            "sha256",
            "rows_loaded",
            "loaded_at",
        ),
    )
    loaded_at = datetime.now(timezone.utc)
    records = [
        {
            "sink_table": table,
            "file_name": paths[table].name,
            "sha256": sha256_file(paths[table]),
            "rows_loaded": results[table]["loaded"],
            "loaded_at": loaded_at,
        }
        for table in SINK_TABLES
    ]
    _insert_records(connection, provenance, records)
    results[SINK_PROVENANCE_TABLE] = {"raw": len(records), "loaded": len(records)}
    return results



def load_stage1_sink(
    cfg: Config, connection: sa.Connection | None = None
) -> dict[str, dict[str, int]]:
    """Load Stage 1's curated outputs into the factory schema, atomically.



    Truncate then insert, in one transaction: a rerun converges on the same
    table contents instead of appending, and a failure at any point leaves the
    previously loaded rows exactly as they were. A caller that already holds a
    transaction (a test, or a larger rebuild script) may pass its connection and
    keeps the right to roll the whole thing back.
    """
    if cfg.database is None:
        raise ConfigError("Missing config section: [database]")
    paths = factory_sink_paths(cfg.database.stage1_curated_dir)
    # A set left half-published by a killed run must not reach the database.
    verify_outputs(
        cfg.paths.manifest_path,
        cfg.root,
        {
            "dataset": paths[SINK_CASE_TABLE],
            "wide_table": paths[SINK_WIDE_TABLE],
            "quality_report": paths[SINK_QUALITY_TABLE],
        },
    )
    if connection is not None:
        truncate_factory(connection)
        return _load_sink(connection, cfg, paths)
    engine = sa.create_engine(build_database_url(cfg))
    try:
        with engine.begin() as owned:
            truncate_factory(owned)
            return _load_sink(owned, cfg, paths)
    finally:
        engine.dispose()



def reset_factory_schema(connection: sa.Connection) -> list[str]:
    """Drop the sink schema and forget 009, in the caller's transaction.

    The counterpart of reset_olist_schema: without forgetting 009 the next run
    would skip the migration and load into a schema that no longer exists.
    """
    connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {FACTORY_SCHEMA} CASCADE"))
    return forget_migrations(connection, is_sink_migration)
