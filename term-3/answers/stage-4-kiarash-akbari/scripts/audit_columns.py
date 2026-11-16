#!/usr/bin/env python3
"""Print the witnesses the column audit is built on, for every column of every table.

``04`` asks for a claim about time -- "this column already existed when the order was
approved" -- and a claim of that kind is worth nothing without evidence that can be
re-run. ROADMAP phase 2 names three: the column's meaning, when it gets filled relative
to approval, and how its presence correlates with ``order_status``. Two of the three are
SQL over the loaded database; the first is the data dictionary, which this repository
does not carry, so the script says so rather than inventing a definition.

Coverage is every column of the nine ``olist`` tables, because the denominator of the
audit is what the DDL *defines*, not what a convenient subset reaches. Tables reach the
order grain in two shapes, and the shape decides which witnesses exist:

* an **event** table carries a timestamp, so its columns can be dated against
  ``order_approved_at`` -- fill share, filled-at-or-before-approval, and the
  ``order_status`` split that exposes columns only filled at delivery;
* a **dimension** table carries no timestamp at all, so no query can date it. Its
  witnesses are nullability at table grain and how many orders cannot see a row. The
  verdict those support is weaker and says so: the row describes the entity as the
  snapshot holds it, not as it was at approval.

Printing is the whole job here on purpose. Executing the queries belongs to whoever
holds the database -- ``psql``, or the run that fills the CSV in -- because a script that
generates and then trusts its own output is the mistake this audit exists to prevent.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

STAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_ROOT / "src"))

DEFAULT_DDL = STAGE_ROOT.parent / "stage-2-kiarash-akbari" / "sql" / "001_create_olist_schema.sql"
#: the moment the brief fixes as "before this, nothing may be known"
DECISION_COLUMN = "order_approved_at"
#: a timestamp is the only kind of column whose arrival can be dated from the data
TIMESTAMP_TYPES = ("timestamp", "date")

#: how each table joins to the order grain. ``None`` means the table *is* the grain; a
#: string is the key it shares with ``olist.orders``; a pair is "join this table to
#: order_items on the first key", for the dimensions that are one hop further out.
GRAIN_KEYS: dict[str, str | tuple[str, str] | None] = {
    "orders": None,
    "order_items": "order_id",
    "order_reviews": "order_id",
    "payments": "order_id",
    "customers": "customer_id",
    "sellers": ("order_items", "seller_id"),
    "products": ("order_items", "product_id"),
    "product_category_name_translation": ("products", "product_category_name"),
    "geolocation": ("customers", "geolocation_zip_code_prefix"),
}
#: how an order reaches each dimension, written out rather than derived -- an
#: audit-grade claim must not rest on a join the script inferred. ``d`` is the dimension.
ORDER_PATH: dict[str, str] = {
    "customers": "JOIN olist.customers AS d ON d.customer_id = o.customer_id",
    "sellers": (
        "JOIN olist.order_items AS i ON i.order_id = o.order_id\n"
        "  JOIN olist.sellers AS d ON d.seller_id = i.seller_id"
    ),
    "products": (
        "JOIN olist.order_items AS i ON i.order_id = o.order_id\n"
        "  JOIN olist.products AS d ON d.product_id = i.product_id"
    ),
    "product_category_name_translation": (
        "JOIN olist.order_items AS i ON i.order_id = o.order_id\n"
        "  JOIN olist.products AS p ON p.product_id = i.product_id\n"
        "  JOIN olist.product_category_name_translation AS d"
        " ON d.product_category_name = p.product_category_name"
    ),
    "geolocation": (
        "JOIN olist.customers AS c ON c.customer_id = o.customer_id\n"
        "  JOIN olist.geolocation AS d"
        " ON d.geolocation_zip_code_prefix = c.customer_zip_code_prefix"
    ),
}


def table_block(ddl_text: str, table: str) -> str:
    """The body of one ``CREATE TABLE`` statement, for type lookups."""
    match = re.search(rf"CREATE TABLE olist\.{re.escape(table)} \((.*?)\n\);", ddl_text, re.DOTALL)
    return match.group(1) if match else ""


def column_type(ddl_text: str, table: str, column: str) -> str:
    """The declared type of one column, or an empty string when the DDL has no such row."""
    match = re.search(
        rf"^\s*{re.escape(column)}\s+([A-Za-z]+)", table_block(ddl_text, table), re.MULTILINE
    )
    return match.group(1).lower() if match else ""


def is_timestamp(ddl_text: str, table: str, column: str) -> bool:
    """Is this column a timestamp in the DDL? Only those get the span witness."""
    return column_type(ddl_text, table, column).startswith(TIMESTAMP_TYPES)


def timestamp_columns(ddl_text: str, table: str, columns: list[str]) -> list[str]:
    return [name for name in columns if is_timestamp(ddl_text, table, name)]


def grain_cte(table: str) -> str:
    """One subquery per table, carrying the approval moment and the status next to it.

    The two facts every event witness needs come from ``olist.orders``; pulling them in
    once, here, is what keeps three witnesses from disagreeing about the join.
    """
    key = GRAIN_KEYS[table]
    if key is None:
        # the grain table is orders itself: the decision column is already in it, but the
        # witnesses all speak of decision_at, so the alias is what this branch adds
        return f"  SELECT o.*, o.{DECISION_COLUMN} AS decision_at\n  FROM olist.{table} AS o"
    if isinstance(key, str):
        return (
            f"  SELECT t.*,\n"
            f"         o.{DECISION_COLUMN} AS decision_at,\n"
            f"         o.order_status AS order_status\n"
            f"  FROM olist.{table} AS t\n"
            f"  JOIN olist.orders AS o ON o.{key} = t.{key}"
        )
    bridge, on = key
    return (
        f"  SELECT t.*,\n"
        f"         o.{DECISION_COLUMN} AS decision_at,\n"
        f"         o.order_status AS order_status\n"
        f"  FROM olist.{table} AS t\n"
        f"  JOIN olist.{bridge} AS b ON b.{on} = t.{on}\n"
        f"  JOIN olist.orders AS o ON o.order_id = b.order_id"
    )


def event_witnesses(table: str, column: str, *, dated: bool) -> list[tuple[str, str]]:
    """The three ROADMAP witnesses, as SQL over one base subquery.

    ``dated`` is the DDL's own answer, not the auditor's: a column whose declared type is
    a timestamp can be compared with the approval moment, and one whose type is not can
    have no such witness at all. Saying ``NULL`` in the SQL is deliberate -- a zero would
    read like a measurement.
    """
    base = f"WITH base AS (\n{grain_cte(table)}\n)"
    filled = f"COUNT({column})"
    unapproved = "COUNT(*) FILTER (WHERE decision_at IS NULL) AS rows_of_unapproved_orders"
    presence = (
        f"COUNT({column}) FILTER (WHERE {column} <= decision_at) AS at_or_before_approval"
        if dated
        else "NULL AS at_or_before_approval"
    )
    rows: list[tuple[str, str]] = [
        (
            "fill and presence at approval",
            "\n".join(
                [
                    base,
                    "SELECT COUNT(*) AS rows_total,",
                    f"       {filled} AS filled_rows,",
                    f"       {presence},",
                    f"       {unapproved}",
                    "FROM base;",
                ]
            ),
        ),
        (
            "filled share by order_status",
            "\n".join(
                [
                    base,
                    "SELECT order_status,",
                    "       COUNT(*) AS rows_total,",
                    f"       {filled} AS filled_rows,",
                    f"       ROUND(100.0 * {filled} / NULLIF(COUNT(*), 0), 1) AS filled_pct",
                    "FROM base",
                    "GROUP BY order_status",
                    "ORDER BY rows_total DESC;",
                ]
            ),
        ),
    ]
    if dated and column != DECISION_COLUMN:
        span = f"EXTRACT(EPOCH FROM ({column} - decision_at)) / 60.0"
        rows.append(
            (
                "minutes between approval and this stamp",
                "\n".join(
                    [
                        base,
                        "SELECT COUNT(*) AS comparable_rows,",
                        f"       ROUND(AVG({span}), 1) AS avg_minutes_after_approval,",
                        f"       MIN({span}) AS min_minutes,",
                        f"       COUNT(*) FILTER (WHERE {column} <= decision_at) AS at_or_before_approval",
                        "FROM base",
                        f"WHERE {column} IS NOT NULL AND decision_at IS NOT NULL;",
                    ]
                ),
            )
        )
    return rows


def dimension_witnesses(table: str, column: str) -> list[tuple[str, str]]:
    """What can be measured when the table has no timestamp to date anything by.

    Nullability at table grain, and how many orders cannot see a row at all. The second
    one is the leak-shaped risk in the other direction: a feature that treats "no seller
    row" as zero is a feature with a silent default.
    """
    path = ORDER_PATH.get(table)
    reach = ""
    if path is not None:
        reach = "\n".join(
            [
                "",
                f"-- how many orders can see a {table} row at all",
                "WITH reachable AS (",
                "  SELECT DISTINCT o.order_id",
                "  FROM olist.orders AS o",
                f"  {path}",
                ")",
                "SELECT COUNT(*) AS orders_total,",
                "       COUNT(*) FILTER (WHERE r.order_id IS NULL) AS orders_without_a_row",
                "FROM olist.orders AS o",
                "LEFT JOIN reachable AS r ON r.order_id = o.order_id;",
            ]
        )
    rows = [
        (
            "nullability at table grain (no timestamp exists in this table)",
            f"SELECT COUNT(*) AS rows_total,\n"
            f"       COUNT({column}) AS filled_rows,\n"
            f"       COUNT(*) - COUNT({column}) AS null_rows,\n"
            f"       COUNT(DISTINCT {column}) AS distinct_values\n"
            f"FROM olist.{table};" + reach,
        )
    ]
    if isinstance(GRAIN_KEYS[table], str):
        # the inverse question, and the more useful one: can a row of this table exist for
        # an order that was never approved? If it can, the value is knowable at approval.
        rows.append(
            (
                "rows on orders that were never approved (pre-approval existence)",
                "\n".join(
                    [
                        f"WITH base AS (\n{grain_cte(table)}\n)",
                        "SELECT COUNT(*) AS rows_at_order_grain,",
                        "       COUNT(*) FILTER (WHERE decision_at IS NULL) AS rows_of_unapproved_orders,",
                        "       COUNT(*) FILTER (WHERE order_status = 'created') AS rows_of_created_orders,",
                        f"       COUNT({column}) AS filled_rows",
                        "FROM base;",
                    ]
                ),
            )
        )
    return rows


def build(tables: list[str], ddl: Path, only: str | None) -> str:
    from capstone.audit import columns_from_ddl  # see the note in main()

    ddl_text = ddl.read_text(encoding="utf-8")
    columns = columns_from_ddl(ddl)
    chunks = [
        "-- witnesses for the column time audit (ROADMAP phase 2, checklist 4.1 and 4.3)",
        f"-- column lists read from {ddl.relative_to(STAGE_ROOT.parent)}",
        "-- every column starts unverified; a verdict needs an answer from all three",
        "",
    ]
    for table in tables:
        names = columns.get(table)
        if not names:
            continue
        dated = timestamp_columns(ddl_text, table, names)
        kind = "event" if dated else "dimension"
        chunks.append(
            f"-- === olist.{table} ({kind}: {', '.join(dated) or 'no timestamp column'}) ==="
        )
        for column in names:
            if only and only not in {table, column} and only != f"{table}.{column}":
                continue
            chunks.append(
                f"-- {table}.{column}: {column_type(ddl_text, table, column) or 'type unknown'}"
            )
            witnesses = (
                event_witnesses(table, column, dated=is_timestamp(ddl_text, table, column))
                if dated
                else dimension_witnesses(table, column)
            )
            for title, sql in witnesses:
                chunks.append(f"-- {title}")
                chunks.append(sql)
            chunks.append("")
    return "\n".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="emit the SQL behind every column audit verdict (prints, never runs)"
    )
    parser.add_argument(
        "--ddl", type=Path, default=DEFAULT_DDL, help="schema file to read columns from"
    )
    parser.add_argument("--table", action="append", help="limit to this table (repeatable)")
    parser.add_argument("--only", help="limit to one table or column name")
    parser.add_argument("--out", type=Path, help="write to this file instead of stdout")
    args = parser.parse_args(argv)

    from capstone.audit import columns_from_ddl  # needs the sys.path insert at the top

    if not args.ddl.is_file():
        print(f"error: no DDL at {args.ddl}", file=sys.stderr)
        return 2
    universe = list(columns_from_ddl(args.ddl))
    tables = [t for t in (args.table or universe) if t in universe]
    if not tables:
        print(f"error: none of {args.table} are tables in {args.ddl}", file=sys.stderr)
        return 2
    text = build(tables, args.ddl, args.only)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out} ({text.count(chr(10)) + 1} lines, {len(tables)} tables)")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
