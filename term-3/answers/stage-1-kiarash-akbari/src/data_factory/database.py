"""PostgreSQL destination for the existing data factory package."""
from __future__ import annotations

import csv
import os
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.engine import URL

from .config import Config
from .errors import DataError

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
GEOLOCATION_KEY = (
    "geolocation_zip_code_prefix", "geolocation_lat",
    "geolocation_lng", "geolocation_city", "geolocation_state",
)
COLUMN_ALIASES = {
    "product_name_length": "product_name_lenght",
    "product_description_length": "product_description_lenght",
}


def build_database_url(cfg: Config) -> URL:
    """Keep the password outside versioned configuration."""
    if cfg.database is None:
        raise DataError("Database destination is not configured")
    parsed = urlsplit(cfg.database.url)
    return URL.create(
        drivername=parsed.scheme, username=parsed.username,
        password=os.environ.get(cfg.database.password_env),
        host=parsed.hostname, port=parsed.port,
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
    if value is None:
        if not column.nullable:
            raise ValueError("Required value is empty")
        return None
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


def load_csv(connection: sa.Connection, csv_path: Path, schema: str, table: str,
             batch_size: int, deduplicate: bool = False) -> dict[str, int]:
    """Reflect identifiers and bind typed values through SQLAlchemy Core."""
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise DataError("CSV file not found", context={"file": str(csv_path)})
    target = sa.Table(table, sa.MetaData(), schema=schema, autoload_with=connection)
    columns = [c for c in target.columns if c.identity is None and c.computed is None]
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        header = next(csv.reader(fh), [])
    source_names = {c.name: c.name if c.name in header else COLUMN_ALIASES.get(c.name, c.name)
                    for c in columns}
    missing = [name for name in source_names.values() if name not in header]
    if missing:
        raise DataError("CSV columns are missing", context={"file": str(csv_path), "columns": missing})
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
                    raise DataError("CSV value violates the typed load contract", context={
                        "file": str(csv_path), "line": counts["raw"] + 1,
                        "column": column.name}) from exc
            converted.append(record)
        if converted:
            connection.execute(sa.insert(target), converted)
            counts["loaded"] += len(converted)
    return counts


def load_olist(cfg: Config) -> dict[str, dict[str, int]]:
    """Load into existing tables atomically; dispose the engine on every path."""
    if cfg.database is None:
        raise DataError("Database destination is not configured")
    engine = sa.create_engine(build_database_url(cfg))
    try:
        with engine.begin() as connection:
            return {table: load_csv(
                connection, cfg.database.olist_csv_dir / OLIST_CSV_FILES[table],
                OLIST_SCHEMA, table, cfg.database.batch_size,
                deduplicate=(table == "geolocation"),
            ) for table in OLIST_TABLES}
    finally:
        engine.dispose()
