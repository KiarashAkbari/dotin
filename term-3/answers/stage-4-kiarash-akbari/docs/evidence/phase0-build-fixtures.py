"""Build the nine minimal fixture CSVs against a real PostgreSQL.

The header must contain, for each table, exactly the columns the stage-1 loader
inserts: `load_csv` reflects the table and keeps `identity is None and computed
is None`. Reflection is therefore done against the real schema, not hardcoded,
and the resulting header list is printed so the test file can embed it.
"""
import csv
import pathlib

import pgserver
import sqlalchemy as sa

ROWS = {
    "customers": [["0" * 32, "0" * 32, "01122", "city", "SP"]],
    "sellers": [["0" * 32, "01122", "city", "SP"]],
    "product_category_name_translation": [["books", "books"]],
    "products": [["0" * 32, "books", 10, 10, 1, 100, 10, 10, 10]],
    "geolocation": [["01122", -23.5, -46.6, "city", "SP"]],
    "orders": [["0" * 32, "0" * 32, "delivered", "2018-01-01 00:00:00", "2018-01-02 00:00:00",
                "2018-01-04 00:00:00", "2018-01-11 00:00:00", "2018-01-20 00:00:00"]],
    "order_items": [["0" * 32, 1, "0" * 32, "0" * 32, "2018-01-05 00:00:00", 10.0, 2.0]],
    "payments": [["0" * 32, 1, "credit_card", 1, 12.0]],
    "order_reviews": [["0" * 32, "0" * 32, 1, "", "", "2018-01-12 00:00:00", ""]],
}
OUT = pathlib.Path("/tmp/fixtures/olist_csv")
# the loader looks up these exact names, not the table names
NAMES = {
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
SQL = pathlib.Path("/home/user/data-factory-v2/stage-2/sql")

srv = pgserver.get_server(pathlib.Path("/tmp/pgdata_v4"), cleanup_mode=None)
admin = sa.create_engine(str(sa.make_url(srv.get_uri()).set(drivername="postgresql+psycopg2")),
                        isolation_level="AUTOCOMMIT")
with admin.connect() as c:
    c.execute(sa.text("DROP DATABASE IF EXISTS fixture_build WITH (FORCE)"))
    c.execute(sa.text("CREATE DATABASE fixture_build"))
eng = sa.create_engine(str(sa.make_url(srv.get_uri()).set(
    drivername="postgresql+psycopg2", database="fixture_build")))
with eng.begin() as c:
    c.execute(sa.text((SQL / "001_create_olist_schema.sql").read_text(encoding="utf-8")))
    md = sa.MetaData()
    OUT.mkdir(parents=True, exist_ok=True)
    headers = {}
    for table, rows in ROWS.items():
        target = sa.Table(table, md, schema="olist", autoload_with=c)
        cols = [col.name for col in target.columns if col.identity is None and col.computed is None]
        headers[table] = cols
        path = OUT / NAMES[table]
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(cols)
            for row in rows:
                writer.writerow(row)
        if len(cols) != len(rows[0]):
            print(f"!! column/row mismatch {table}: {len(cols)} cols, {len(rows[0])} values")
eng.dispose()
with admin.connect() as c:
    c.execute(sa.text("DROP DATABASE IF EXISTS fixture_build WITH (FORCE)"))
admin.dispose()
print("geolocation header:", headers["geolocation"])
print("products header    :", headers["products"])
print("FIXTURE_COLUMNS = {")
for table in ROWS:
    print(f'    "{table}": ({", ".join(repr(c) for c in headers[table])}),')
print("}")
