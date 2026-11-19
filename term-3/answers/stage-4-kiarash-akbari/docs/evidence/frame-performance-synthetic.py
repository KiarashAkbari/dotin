#!/usr/bin/env python3
"""Seed a *scratch* PostgreSQL with a production-sized synthetic olist, to measure the frame on.

``docs/evidence/frame-performance-run.py`` measures whatever database it is pointed at, which is
correct for a report and useless for a plan: on the two-order fixture every plan is trivial, and a
"412 ms" measured on ninety-nine thousand rows is a different claim than one measured on nine. This
script builds the second one -- same schema, same shapes, invented values -- so the plan, the
planner's statistics and the row estimates are the ones a real load would produce, and so a reader
can re-run the measurement at a size they choose instead of trusting a number pasted from a
sandbox.

It writes rows, so it is the one evidence script that has to refuse things:

* ``CAPSTONE_SMOKE_ALLOW_DESTROY=1`` is required, the same flag that guards the smoke tests. It says
  "this database is disposable", which is the only state in which generating 100k rows is acceptable;
* it refuses a target whose ``olist`` tables already hold rows, because the honest way to be sure
  those rows are nobody's data is to require them to be absent -- no TRUNCATE, no DELETE, ever;
* it refuses a table it does not recognise as stage-2's ``olist`` schema, so pointing it at a database
  with a same-named-but-different schema fails before the first insert rather than after it.

Run from ``stage-4/`` after ``stage-2/sql/001_create_olist_schema.sql`` has been applied::

    CAPSTONE_SMOKE_ALLOW_DESTROY=1 CAPSTONE_DATABASE_URI="postgresql://…" \
      python3 docs/evidence/frame-performance-synthetic.py --orders 100000

The numbers are printed at the end, and they are the numbers the performance log quotes: a log
whose scale is not stated is how a synthetic measurement gets read as a production one.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

STAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(STAGE / "src"))

from capstone.config import load_contract  # noqa: E402
from capstone.errors import InputError  # noqa: E402
from capstone.features import connect, database_uri  # noqa: E402

#: the columns each generated table must have for the frame to read it; the check is existence,
#: not type
REQUIRED = {
    "olist.orders": ("order_id", "customer_id", "order_status", "order_purchase_timestamp"),
    "olist.customers": ("customer_id", "customer_unique_id", "customer_zip_code_prefix"),
    "olist.order_items": ("order_id", "product_id", "seller_id", "shipping_limit_date"),
    "olist.payments": ("order_id", "payment_type"),
    "olist.geolocation": (
        "geolocation_zip_code_prefix", "geolocation_lat", "geolocation_lng",
        "geolocation_city", "geolocation_state",
    ),
    "olist.sellers": ("seller_id", "seller_zip_code_prefix"),
    "olist.products": ("product_id", "product_category_name", "product_weight_g"),
}
#: the frame's anchor is the approval; anything outside this window is not the corpus the brief
#: describes
WINDOW = "'2016-09-01 00:00:00'::timestamp + (random() * 700) * interval '1 day'"


def query(cursor: Any, sql: str, params: tuple = ()) -> list[tuple]:
    cursor.execute(sql, params)
    return list(cursor.fetchall())


def check_schema(cursor: Any) -> list[str]:
    """Missing columns, in the shape the caller prints. An empty list means the target is stage-2's."""
    problems: list[str] = []
    for qualified, columns in REQUIRED.items():
        schema, table = qualified.split(".", 1)
        found = {
            str(row[0])
            for row in query(
                cursor,
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema = %s AND table_name = %s",
                (schema, table),
            )
        }
        if not found:
            problems.append(f"{qualified} does not exist")
            continue
        missing = [name for name in columns if name not in found]
        if missing:
            problems.append(f"{qualified} is missing {', '.join(missing)}")
    return problems


def insert(cursor: Any, label: str, sql: str) -> float:
    """One generated table, with the wall time printed -- the scale statement has to be honest too."""
    started = time.perf_counter()
    cursor.execute(sql)
    elapsed = (time.perf_counter() - started) * 1000.0
    print(f"      {label:<26} {elapsed:>9.0f} ms")
    return elapsed


def generate(cursor: Any, orders: int, prefixes: int) -> dict[str, int]:
    """The corpus, in dependency order: geolocation and sellers first, then the fact tables."""
    print(f"generating a synthetic corpus of {orders} orders ({prefixes} zip prefixes)")
    insert(
        cursor,
        "geolocation",
        f"""
INSERT INTO olist.geolocation (geolocation_zip_code_prefix, geolocation_lat, geolocation_lng,
                                geolocation_city, geolocation_state)
SELECT lpad((n % {prefixes})::text, 5, '0'),
       -30 + random() * 10, -70 + random() * 20,
       'city_' || (n % {prefixes})::text,
       (ARRAY['SP','RJ','MG','RS','PR','BA','SC','PE','GO','CE'])[1 + n % 10]
FROM generate_series(1, {prefixes * 8}) AS n
ON CONFLICT DO NOTHING""",
    )
    insert(
        cursor,
        "sellers",
        f"""
INSERT INTO olist.sellers (seller_id, seller_zip_code_prefix, seller_city, seller_state)
SELECT md5('seller' || n), lpad((n % {prefixes})::text, 5, '0'), 'city_' || (n % {prefixes})::text,
       (ARRAY['SP','RJ','MG','RS','PR'])[1 + n % 5]
FROM generate_series(1, {max(1, orders // 8)}) AS n
ON CONFLICT DO NOTHING""",
    )
    insert(
        cursor,
        "products",
        f"""
INSERT INTO olist.products (product_id, product_category_name, product_weight_g)
SELECT md5('product' || n), 'cat_' || (n % 40)::text, 100 + (n * 37) % 4000
FROM generate_series(1, {max(1, orders // 2)}) AS n
ON CONFLICT DO NOTHING""",
    )
    insert(
        cursor,
        "customers",
        f"""
INSERT INTO olist.customers (customer_id, customer_unique_id, customer_zip_code_prefix,
                             customer_city, customer_state)
SELECT md5('customer' || n),
       -- a returning-buyer population: 30% of ids repeat, so the history features have something
       -- to count. One row per customer here; the real orders table repeats a customer per order.
       md5('unique' || (n % (1 + {orders} * 7 / 10))),
       lpad((n % {prefixes})::text, 5, '0'), 'city_' || (n % {prefixes})::text,
       (ARRAY['SP','RJ','MG','RS','PR','BA'])[1 + n % 6]
FROM generate_series(1, {orders}) AS n
ON CONFLICT DO NOTHING""",
    )
    insert(
        cursor,
        "orders",
        f"""
INSERT INTO olist.orders (order_id, customer_id, order_status, order_purchase_timestamp,
                          order_approved_at, order_delivered_customer_date,
                          order_estimated_delivery_date)
SELECT md5('order' || n), md5('customer' || n),
       -- D1 drops rows with no anchor; keeping a few is the point, so the count is visible
       (ARRAY['delivered','delivered','delivered','shipped','canceled','unavailable'])[1 + n % 6],
       {WINDOW},
       CASE WHEN n % 40 = 0 THEN NULL
            ELSE {WINDOW.replace('(random() * 700)', '(random() * 690)')} + interval '1 hour'
       END,
       {WINDOW.replace('(random() * 700)', '(random() * 710)')} + interval '9 days',
       {WINDOW.replace('(random() * 700)', '(random() * 695)')} + interval '20 days'
FROM generate_series(1, {orders}) AS n
ON CONFLICT DO NOTHING""",
    )
    insert(
        cursor,
        "order_items",
        f"""
INSERT INTO olist.order_items (order_id, order_item_id, product_id, seller_id,
                               shipping_limit_date, price, freight_value)
SELECT o.order_id, item.n AS order_item_id,
       (SELECT product_id FROM olist.products ORDER BY random() LIMIT 1),
       (SELECT seller_id FROM olist.sellers ORDER BY random() LIMIT 1),
       o.order_approved_at + interval '2 days',
       10 + random() * 400, 5 + random() * 60
-- a third of the orders carry a second item, which is what makes the rollup's
       -- "primary row" rule (lowest item number) matter at all
FROM olist.orders AS o,
     generate_series(1, 1 + (abs(hashtext(o.order_id)) % 3 = 0)::int) AS item(n)
WHERE o.order_approved_at IS NOT NULL""",
    )
    insert(
        cursor,
        "payments",
        f"""
INSERT INTO olist.payments (order_id, payment_sequential, payment_type,
                            payment_installments, payment_value)
SELECT o.order_id, 1, (ARRAY['credit_card','credit_card','boleto','voucher'])[1 + n % 4],
       1 + n % 6, 20 + random() * 500
FROM olist.orders AS o, generate_series(1, 1) AS n
WHERE o.order_approved_at IS NOT NULL""",
    )
    final: dict[str, int] = {}
    for qualified in REQUIRED:
        final[qualified] = int(query(cursor, f"SELECT count(*) FROM {qualified}")[0][0])
    print(
        "ANALYZE, so the planner is not guessing -- a plan built on empty statistics is not this"
        "\nquery's plan, and the row estimates in the log would describe a database that does not exist"
    )
    for qualified in REQUIRED:
        cursor.execute(f"ANALYZE {qualified}")
    return final


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--orders", type=int, default=100_000)
    parser.add_argument("--zip-prefixes", type=int, default=32_248)
    args = parser.parse_args(argv)
    if os.environ.get("CAPSTONE_SMOKE_ALLOW_DESTROY", "").strip() != "1":
        print(
            "refusing: this script inserts tens of thousands of rows, so it only runs when\n"
            "CAPSTONE_SMOKE_ALLOW_DESTROY=1 says the target database is disposable. Point\n"
            "CAPSTONE_DATABASE_URI at a scratch cluster (the smoke tests start one with pgserver)\n"
            "and re-run. Nothing was written and nothing was deleted."
        )
        return 0
    config = load_contract(STAGE / "config" / "capstone.example.toml")
    try:
        uri = database_uri(config, purpose="seed a scratch cluster for the performance log")
    except InputError as error:
        reason = re.sub(r"\s+", " ", str(error)).strip()
        print(f"refusing: {reason}")
        return 0
    connection = connect(uri, autocommit=True, purpose="seed a scratch cluster for the performance log")
    try:
        cursor = connection.cursor()
        database = query(cursor, "SELECT current_database()")[0][0]
        problems = check_schema(cursor)
        if problems:
            for line in problems:
                print(f"refusing: {line}")
            print(
                "this is not stage-2's olist schema, so the insert would either fail mid-way or land\n"
                "somewhere unexpected. Apply stage-2/sql/001_create_olist_schema.sql first."
            )
            return 0
        existing = {
            qualified: int(
                query(cursor, f"SELECT count(*) FROM {qualified}")[0][0]
            )
            for qualified in REQUIRED
        }
        occupied = {name: count for name, count in existing.items() if count}
        if occupied:
            print("refusing: the target already holds rows, and this script never deletes:")
            for name, count in sorted(occupied.items()):
                print(f"      {name:<24} {count:>9} rows")
            print("point it at an empty database; a scratch cluster is the only supported target")
            return 0
        print(f"target: {database} (empty olist schema, checked above)")
        final = generate(cursor, args.orders, args.zip_prefixes)
        print("\nthe counts this run wrote, which the log has to quote:")
        for name, count in sorted(final.items()):
            print(f"      {name:<24} {count:>9} rows")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
