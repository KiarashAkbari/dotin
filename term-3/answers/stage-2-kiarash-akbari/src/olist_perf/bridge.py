"""Python <-> SQL bridge shown in the notebook: chunked reads, typed writes, FK audit.

All statements are constants with bound parameters; connections are opened
with ``with`` blocks so none outlives the call that opened it.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from . import db

# Read from the multi-table view (002), streamed in chunks: the client never
# holds the 112k order lines at once, only one chunk plus the running totals.
VIEW_LINES = sa.text(
    "SELECT order_id, order_purchase_timestamp, product_category_name_english, "
    "item_value FROM olist.v_order_item_sales "
    "WHERE order_purchase_timestamp >= CAST(:since AS timestamp) "
    "ORDER BY order_purchase_timestamp, order_id")


def read_view_in_chunks(engine: sa.Engine, *, since: str = "2016-01-01",
                        chunksize: int = 20_000) -> dict:
    """Monthly revenue from v_order_item_sales, aggregated chunk by chunk."""
    chunks, rows, largest = 0, 0, 0
    parts = []
    with engine.connect() as connection:
        streaming = connection.execution_options(stream_results=True,
                                                 max_row_buffer=chunksize)
        for frame in pd.read_sql_query(VIEW_LINES, streaming, params={"since": since},
                                       chunksize=chunksize):
            chunks += 1
            rows += len(frame)
            largest = max(largest, len(frame))
            frame["month"] = frame["order_purchase_timestamp"].dt.to_period("M")
            parts.append(frame.groupby("month")["item_value"]
                         .agg(lambda s: sum(s, start=0)).rename("revenue"))
    monthly = (pd.concat(parts).groupby(level=0).agg(lambda s: sum(s, start=0))
               .reset_index())
    monthly["month"] = monthly["month"].astype(str)
    return {"chunksize": chunksize, "chunks": chunks, "rows": rows,
            "largest_chunk_rows": largest, "monthly": monthly}


# The column types are ours, not pandas' guesses (which would be TEXT,
# BIGINT and DOUBLE PRECISION): a month is a DATE, a state is CHAR(2), counts
# are INTEGER and the ratio keeps four exact decimals.
Q2_EXPORT_DTYPES = {
    "purchase_month": sa.Date(),
    "customer_state": sa.CHAR(2),
    "delivered_orders": sa.Integer(),
    "late_orders": sa.Integer(),
    "late_ratio": sa.Numeric(6, 4),
}
EXPORT_SCHEMA = "reporting"
EXPORT_TABLE = "delivery_late_ratio_by_state_month"
EXPORT_COLUMNS = sa.text(
    "SELECT column_name, data_type, character_maximum_length, numeric_precision, "
    "numeric_scale FROM information_schema.columns "
    "WHERE table_schema = :schema AND table_name = :table ORDER BY ordinal_position")


def write_typed(engine: sa.Engine, frame: pd.DataFrame) -> pd.DataFrame:
    """Write the Q2 result with the explicit dtype map; return the stored types."""
    missing = set(Q2_EXPORT_DTYPES) - set(frame.columns)
    if missing:
        raise ValueError(f"frame lacks columns {sorted(missing)}")
    with engine.begin() as connection:
        frame[list(Q2_EXPORT_DTYPES)].to_sql(
            EXPORT_TABLE, connection, schema=EXPORT_SCHEMA, if_exists="replace",
            index=False, dtype=Q2_EXPORT_DTYPES, chunksize=1_000)
        stored = pd.read_sql_query(EXPORT_COLUMNS, connection,
                                   params={"schema": EXPORT_SCHEMA, "table": EXPORT_TABLE})
        count = connection.scalar(sa.select(sa.func.count()).select_from(
            sa.table(EXPORT_TABLE, schema=EXPORT_SCHEMA)))
    stored.attrs["rows_written"] = count
    return stored


# --- foreign-key audit of the raw CSVs ---------------------------------------

CSV = {
    "customers": "olist_customers_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "products": "olist_products_dataset.csv",
    "orders": "olist_orders_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "order_reviews": "olist_order_reviews_dataset.csv",
    "geolocation": "olist_geolocation_dataset.csv",
    "product_category_name_translation": "product_category_name_translation.csv",
}
# (child table, child column, parent table, parent column, declared in 001?)
REFERENCES = (
    ("orders", "customer_id", "customers", "customer_id", True),
    ("order_items", "order_id", "orders", "order_id", True),
    ("order_items", "product_id", "products", "product_id", True),
    ("order_items", "seller_id", "sellers", "seller_id", True),
    ("payments", "order_id", "orders", "order_id", True),
    ("order_reviews", "order_id", "orders", "order_id", True),
    ("products", "product_category_name", "product_category_name_translation",
     "product_category_name", False),
    ("customers", "customer_zip_code_prefix", "geolocation",
     "geolocation_zip_code_prefix", False),
    ("sellers", "seller_zip_code_prefix", "geolocation",
     "geolocation_zip_code_prefix", False),
)
TABLE_COUNT = "SELECT count(*) FROM {}"


def csv_dir_from_config() -> Path:
    """The Olist CSV folder of the stage-1 config, resolved like the CLI does."""
    from data_factory.config import load_config

    cfg = load_config(db.config_path())
    path = Path(cfg.database.olist_csv_dir)
    return path if path.is_absolute() else (db.STAGE1_DIR / path).resolve()


def fk_audit(engine: sa.Engine, csv_dir: Path) -> dict:
    """Orphan keys per reference in the raw CSVs, and raw vs loaded row counts.

    Only the key columns are read (usecols, dtype=str), one file at a time.
    """
    keys: dict[tuple[str, str], pd.Series] = {}

    def column(table: str, name: str) -> pd.Series:
        if (table, name) not in keys:
            keys[(table, name)] = pd.read_csv(
                Path(csv_dir) / CSV[table], usecols=[name], dtype=str,
                keep_default_na=False)[name]
        return keys[(table, name)]

    references = []
    for child, child_col, parent, parent_col, declared in REFERENCES:
        values = column(child, child_col)
        present = values[values != ""]
        parent_values = set(column(parent, parent_col))
        orphans = present[~present.isin(parent_values)]
        references.append({
            "reference": f"{child}.{child_col} -> {parent}.{parent_col}",
            "fk_declared": declared,
            "child_rows": int(len(values)),
            "empty_key_rows": int(len(values) - len(present)),
            "orphan_rows": int(len(orphans)),
            "distinct_orphan_keys": int(orphans.nunique()),
        })
        keys.pop((child, child_col), None)
    from psycopg import sql as pgsql

    counts = []
    with engine.connect() as connection:
        raw = connection.connection.driver_connection
        for table, filename in CSV.items():
            # Parsed records, not text lines: review comments contain newlines.
            raw_rows = len(pd.read_csv(Path(csv_dir) / filename, usecols=[0],
                                       dtype=str, keep_default_na=False))
            with raw.cursor() as cursor:
                cursor.execute(pgsql.SQL(TABLE_COUNT).format(
                    pgsql.Identifier("olist", table)))
                loaded = cursor.fetchone()[0]
            counts.append({"table": table, "raw_csv_records": raw_rows,
                           "loaded_rows": loaded, "not_loaded": raw_rows - loaded})
        connection.rollback()
    return {"references": references, "row_counts": counts}
