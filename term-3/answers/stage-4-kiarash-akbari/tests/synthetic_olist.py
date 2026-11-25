"""A tiny, fully synthetic Olist: nine CSVs with the Kaggle names and stage 2's columns.

Used by tests/test_end_to_end.py to walk the whole chain -- stage 1's loader, the SQL feature layer,
the model, the 10 % list and the prediction path -- on data small enough to run in seconds. The
label is planted: orders with many items and a high freight share are more often reviewed 1-2, so a
working pipeline has something to find. Nothing here comes from the real dataset.
"""

from __future__ import annotations

import csv
import hashlib
import random
from datetime import datetime, timedelta
from pathlib import Path

STATES = {"SP": "01001", "RJ": "20001", "MG": "30001", "BA": "40001", "RS": "90001"}
CATEGORIES = {
    "cama_mesa_banho": "bed_bath_table",
    "beleza_saude": "health_beauty",
    "esporte_lazer": "sports_leisure",
    "moveis_decoracao": "furniture_decor",
    "brinquedos": "toys",
}


def hexid(*parts: object) -> str:
    return hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()


def _write(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def stamp(moment: datetime | None) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S") if moment else ""


def write(folder: Path, *, orders: int = 900, seed: int = 4) -> dict[str, int]:
    """Write the nine files; return the number of orders per approval month."""
    rng = random.Random(seed)
    folder.mkdir(parents=True, exist_ok=True)
    _write(
        folder / "product_category_name_translation.csv",
        ["product_category_name", "product_category_name_english"],
        [[pt, en] for pt, en in CATEGORIES.items()],
    )
    geo = []
    for state, zip_code in STATES.items():
        for k in range(3):
            geo.append([zip_code, -20.0 - len(geo) * 0.7 + k * 0.01, -45.0 + len(geo) * 0.5,
                        f"city {state.lower()}", state])
    _write(
        folder / "olist_geolocation_dataset.csv",
        ["geolocation_zip_code_prefix", "geolocation_lat", "geolocation_lng", "geolocation_city",
         "geolocation_state"],
        geo,
    )
    sellers = [[hexid("s", i), list(STATES.values())[i % 5], f"city {list(STATES)[i % 5].lower()}",
                list(STATES)[i % 5]] for i in range(12)]
    _write(folder / "olist_sellers_dataset.csv",
           ["seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"], sellers)
    products = []
    for i in range(40):
        category = list(CATEGORIES)[i % 5] if i % 13 else ""
        products.append([hexid("p", i), category, 40 + i, 300 + i, 1 + i % 4, 200 + 50 * i, 20, 10, 15])
    _write(
        folder / "olist_products_dataset.csv",
        ["product_id", "product_category_name", "product_name_lenght", "product_description_lenght",
         "product_photos_qty", "product_weight_g", "product_length_cm", "product_height_cm",
         "product_width_cm"],
        products,
    )
    customers, order_rows, items, payments, reviews = [], [], [], [], []
    months: dict[str, int] = {}
    start = datetime(2017, 1, 1)
    for i in range(orders):
        state = list(STATES)[rng.randrange(5)]
        unique = hexid("u", i if rng.random() > 0.15 else i % 40)
        customer = hexid("c", i)
        customers.append([customer, unique, STATES[state], f"city {state.lower()}", state])
        purchase = start + timedelta(days=605 * i / orders, hours=rng.randrange(24))
        approved = None if rng.random() < 0.01 else purchase + timedelta(hours=rng.randrange(1, 30))
        estimated = purchase + timedelta(days=rng.randrange(10, 40))
        delivered = purchase + timedelta(days=rng.randrange(3, 45))
        order_id = hexid("o", i)
        order_rows.append([order_id, customer, "delivered", stamp(purchase), stamp(approved),
                           stamp(purchase + timedelta(days=2)), stamp(delivered), stamp(estimated)])
        n_items = rng.choice([1, 1, 1, 2, 3, 4])
        freight_total, price_total = 0.0, 0.0
        for k in range(n_items):
            price = round(rng.uniform(15, 300), 2)
            freight = round(rng.uniform(5, 60), 2)
            price_total += price
            freight_total += freight
            items.append([order_id, k + 1, products[rng.randrange(40)][0], sellers[rng.randrange(12)][0],
                          stamp(purchase + timedelta(days=5)), f"{price:.2f}", f"{freight:.2f}"])
        payments.append([order_id, 1, rng.choice(["credit_card", "boleto", "voucher"]),
                         rng.randrange(1, 10), f"{price_total + freight_total:.2f}"])
        share = freight_total / (price_total + freight_total)
        risk = 0.07 + 0.08 * (n_items - 1) + 0.5 * share + (0.05 if state == "BA" else 0.0)
        if rng.random() > 0.02:  # 2 % never reviewed (D2)
            score = rng.choice([1, 2]) if rng.random() < risk else rng.choice([3, 4, 5, 5, 5])
            reviews.append([hexid("r", i), order_id, score, "", "", stamp(delivered + timedelta(days=1)),
                            stamp(delivered + timedelta(days=2))])
        if approved:
            months[approved.strftime("%Y-%m")] = months.get(approved.strftime("%Y-%m"), 0) + 1
    _write(folder / "olist_customers_dataset.csv",
           ["customer_id", "customer_unique_id", "customer_zip_code_prefix", "customer_city",
            "customer_state"], customers)
    _write(folder / "olist_orders_dataset.csv",
           ["order_id", "customer_id", "order_status", "order_purchase_timestamp", "order_approved_at",
            "order_delivered_carrier_date", "order_delivered_customer_date",
            "order_estimated_delivery_date"], order_rows)
    _write(folder / "olist_order_items_dataset.csv",
           ["order_id", "order_item_id", "product_id", "seller_id", "shipping_limit_date", "price",
            "freight_value"], items)
    _write(folder / "olist_order_payments_dataset.csv",
           ["order_id", "payment_sequential", "payment_type", "payment_installments", "payment_value"],
           payments)
    _write(folder / "olist_order_reviews_dataset.csv",
           ["review_id", "order_id", "review_score", "review_comment_title", "review_comment_message",
            "review_creation_date", "review_answer_timestamp"], reviews)
    return months
