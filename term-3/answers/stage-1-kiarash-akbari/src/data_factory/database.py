"""PostgreSQL destination for the data factory."""

from __future__ import annotations

import csv
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from .config import Config
from .errors import DataError

OLIST_TABLES = (
    "product_category_name_translation",
    "customers",
    "sellers",
    "products",
    "geolocation",
    "orders",
    "order_items",
    "payments",
    "order_reviews",
)

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

OLIST_SCHEMA = "olist"

GEOLOCATION_KEY = (
    "geolocation_zip_code_prefix",
    "geolocation_lat",
    "geolocation_lng",
    "geolocation_city",
    "geolocation_state",
)


def build_database_url(cfg: Config) -> URL:
    """Build a SQLAlchemy URL without exposing or misplacing the password."""
    if cfg.database is None:
        raise DataError("Database destination is not configured")

    parsed = urlsplit(cfg.database.url)
    password = os.environ.get(cfg.database.password_env)

    return URL.create(
        drivername=parsed.scheme,
        username=parsed.username,
        password=password,
        host=parsed.hostname,
        port=parsed.port,
        database=parsed.path.lstrip("/"),
    )


def read_csv_batches(
    path: Path,
    batch_size: int,
) -> Iterator[list[dict[str, str | None]]]:
    """Stream CSV rows in bounded batches to avoid loading the whole file."""
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise DataError(f"CSV has no header: {path}")

        batch: list[dict[str, str | None]] = []
        for row in reader:
            batch.append(
                {key: value if value != "" else None for key, value in row.items()}
            )
            if len(batch) == batch_size:
                yield batch
                batch = []

        if batch:
            yield batch


def deduplicate_geolocation(
    rows: Iterator[list[dict[str, str | None]]],
) -> Iterator[list[dict[str, str | None]]]:
    """Remove exact geolocation duplicates while retaining streaming behavior."""
    seen: set[tuple[str | None, ...]] = set()

    for batch in rows:
        unique_rows: list[dict[str, str | None]] = []
        for row in batch:
            key = tuple(row.get(column) for column in GEOLOCATION_KEY)
            if key not in seen:
                seen.add(key)
                unique_rows.append(row)

        if unique_rows:
            yield unique_rows


def _table_columns(
    connection: sa.Connection,
    schema: str,
    table: str,
) -> list[str]:
    result = connection.execute(
        text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = :schema
              AND table_name = :table
              AND is_identity = 'NO'
              AND is_generated = 'NEVER'
            ORDER BY ordinal_position
            """
        ),
        {"schema": schema, "table": table},
    )
    return [row[0] for row in result]


def load_csv(
    connection: sa.Connection,
    csv_path: Path,
    schema: str,
    table: str,
    batch_size: int,
    deduplicate: bool = False,
) -> dict[str, int]:
    """Load one CSV into an existing table using parameterized batch inserts."""
    if not csv_path.is_file():
        raise DataError(f"CSV file not found: {csv_path}")

    columns = _table_columns(connection, schema, table)
    if not columns:
        raise DataError(f"Table not found: {schema}.{table}")

    quoted_columns = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join(f":{column}" for column in columns)
    statement = text(
        f'INSERT INTO "{schema}"."{table}" ({quoted_columns}) '
        f"VALUES ({placeholders})"
    )

    rows = read_csv_batches(csv_path, batch_size)
    if deduplicate:
        rows = deduplicate_geolocation(rows)

    loaded = 0
    for batch in rows:
        filtered = [
            {column: row.get(column) for column in columns}
            for row in batch
        ]
        connection.execute(statement, filtered)
        loaded += len(filtered)

    return {"loaded": loaded}


def load_olist(cfg: Config) -> dict[str, dict[str, int]]:
    """Load all Olist CSV files into the configured PostgreSQL database."""
    if cfg.database is None:
        raise DataError("Database destination is not configured")

    csv_dir = cfg.database.olist_csv_dir
    engine = create_engine(build_database_url(cfg))

    results: dict[str, dict[str, int]] = {}
    try:
        with engine.begin() as connection:
            for table in OLIST_TABLES:
                results[table] = load_csv(
                    connection=connection,
                    csv_path=csv_dir / OLIST_CSV_FILES[table],
                    schema=OLIST_SCHEMA,
                    table=table,
                    batch_size=cfg.database.batch_size,
                    deduplicate=(table == "geolocation"),
                )
    finally:
        engine.dispose()

    return results