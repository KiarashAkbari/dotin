#!/usr/bin/env python3
"""Rehearse the grader's sealed cut: carve one month out of Olist, score it through ``capstone predict``.

The grader keeps a time slice and runs it through the prediction entry point. This script does the
same with a slice we can check: every order approved in ``--month`` (default: the last full
evaluation month, 2018-08), written as a fresh-order bundle with the Kaggle names -- orders,
customers, items, payments, products, sellers. Reviews are *not* written into the bundle; the
script reads them on its own afterwards to grade the ranking, which is exactly the separation the
sealed evaluation has.

Two checks, both written to ``docs/evidence/sealed-cut-rehearsal.txt``:

1. **same preparation**: every score ``predict`` produced is compared with the score the chain's
   model step stored for the same order in ``capstone.order_score`` (same model run). A prediction
   path that re-implemented the features would disagree here.
2. **same metric**: recall of negative reviews in the top ``capacity.share`` of the cut, next to the
   model step's own number for that month.

Needs the chain to have run (a model and a scored evaluation window). Run from the stage folder::

    python scripts/sealed_cut_rehearsal.py --olist data/raw/olist --month 2018-08
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))

NAMES = {
    "orders": "olist_orders_dataset.csv",
    "customers": "olist_customers_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "products": "olist_products_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
}


def carve(olist: Path, month: str, target: Path) -> list[str]:
    """Write the month's bundle; return its order ids."""
    import pandas as pd

    target.mkdir(parents=True, exist_ok=True)
    orders = pd.read_csv(olist / NAMES["orders"], dtype=str, keep_default_na=False)
    cut = orders[orders["order_approved_at"].str.startswith(month)]
    ids = set(cut["order_id"])
    cut.to_csv(target / NAMES["orders"], index=False)
    customers = pd.read_csv(olist / NAMES["customers"], dtype=str, keep_default_na=False)
    customers[customers["customer_id"].isin(set(cut["customer_id"]))].to_csv(
        target / NAMES["customers"], index=False
    )
    items = pd.read_csv(olist / NAMES["order_items"], dtype=str, keep_default_na=False)
    items = items[items["order_id"].isin(ids)]
    items.to_csv(target / NAMES["order_items"], index=False)
    payments = pd.read_csv(olist / NAMES["payments"], dtype=str, keep_default_na=False)
    payments[payments["order_id"].isin(ids)].to_csv(target / NAMES["payments"], index=False)
    for table, key in (("products", "product_id"), ("sellers", "seller_id")):
        frame = pd.read_csv(olist / NAMES[table], dtype=str, keep_default_na=False)
        frame[frame[key].isin(set(items[key]))].to_csv(target / NAMES[table], index=False)
    return sorted(ids)


def labels(olist: Path, ids: list[str]) -> dict[str, bool]:
    """The worst review per order (D3), read only here, after predict has finished."""
    import pandas as pd

    reviews = pd.read_csv(olist / "olist_order_reviews_dataset.csv", usecols=["order_id", "review_score"])
    reviews = reviews[reviews["order_id"].isin(set(ids))]
    worst = reviews.groupby("order_id")["review_score"].min()
    return {order_id: bool(score <= 2) for order_id, score in worst.items()}


def main(argv: list[str] | None = None) -> int:
    import csv

    from capstone.capacity import capacity_table
    from capstone.system import seed_label
    from capstone.cli import default_config_path
    from capstone.config import load_config
    from capstone.features import connect, database_uri

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    raw_dir = Path(os.environ.get("CAPSTONE_OLIST_DIR") or (STAGE / "data" / "raw" / "olist"))
    parser.add_argument(
        "--olist",
        type=Path,
        default=raw_dir,
        help="the folder of raw Olist CSVs (default: CAPSTONE_OLIST_DIR, else data/raw/olist)",
    )
    parser.add_argument("--month", default="2018-08")
    parser.add_argument("--config", type=Path, default=default_config_path(STAGE))
    parser.add_argument("--evidence", type=Path, default=STAGE / "docs/evidence/sealed-cut-rehearsal.txt")
    args = parser.parse_args(argv)
    config = load_config(args.config)

    # This rehearsal reads the CSVs, not the database: a checkout that has loaded Olist into
    # PostgreSQL still has nothing to carve, and a traceback out of pandas says less than one
    # sentence naming both places the corpus can be pointed at. Exit 0 for the reason the performance
    # probe gives for a missing database -- an input that is not on this machine is not a failed
    # measurement, and the difference matters to whoever reads the log afterwards.
    missing = [name for name in NAMES.values() if not (args.olist / name).is_file()]
    if missing:
        others = f", and {len(missing) - 3} more" if len(missing) > 3 else ""
        print(f"not carved: {args.olist} has no {', '.join(missing[:3])}{others}")
        print(
            "the sealed cut is built from the raw Olist CSVs, so a loaded database is not enough: "
            "pass --olist (or CAPSTONE_OLIST_DIR) at the folder holding them. Nothing was written."
        )
        return 0

    bundle = STAGE / "data" / "raw" / f"sealed_cut_{args.month}"
    ids = carve(args.olist, args.month, bundle)
    out = config.features.curated_dir / f"sealed_cut_{args.month}_ranked.csv"
    command = [sys.executable, "-m", "capstone", "predict", "--config", str(args.config),
               "--input", str(bundle), "--out", str(out)]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        [str(STAGE / "src"), str(STAGE.parent / "stage-1-kiarash-akbari" / "src"),
         os.environ.get("PYTHONPATH", "")]))
    result = subprocess.run(command, cwd=STAGE, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return result.returncode
    summary = json.loads(result.stdout.strip().splitlines()[-1])

    with out.open(encoding="utf-8") as handle:
        ranked = list(csv.DictReader(handle))
    predicted = {row["order_id"]: float(row["score"]) for row in ranked}

    connection = connect(database_uri(config), purpose="compare the rehearsal with the model step")
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT order_id, score FROM capstone.order_score WHERE run_id = %s AND order_id = ANY(%s)",
            (summary["model_run_id"], list(predicted)),
        )
        stored = dict(cursor.fetchall())
        cursor.execute(
            "SELECT metrics -> 'evaluation' -> (metrics ->> 'chosen_model') -> 'months' "
            "FROM capstone.model_run WHERE run_id = %s",
            (summary["model_run_id"],),
        )
        months = cursor.fetchone()[0]
    finally:
        connection.close()
    common = sorted(set(stored) & set(predicted))
    worst = max((abs(stored[key] - predicted[key]) for key in common), default=None)

    truth = labels(args.olist, list(predicted))
    graded = [row for row in ranked if row["order_id"] in truth]
    table = capacity_table(
        [truth[row["order_id"]] for row in graded],
        [float(row["score"]) for row in graded],
        list(config.capacity.report_shares),
    )
    month_row = next((row for row in months if row["month"] == args.month), None)

    lines = [
        f"# sealed-cut rehearsal, month {args.month} (scripts/sealed_cut_rehearsal.py)",
        f"bundle: data/raw/sealed_cut_{args.month}/ (orders, customers, items, payments, products, sellers; no reviews)",
        f"orders in the cut: {len(ids)}; scored: {summary['scored']}; follow-up list: {summary['follow_up']}; "
        f"not scored (no order_approved_at): {summary['not_scored_no_approval']}",
        f"model: {summary['model']} run {summary['model_run_id']}",
        f"config {args.config.name}; {seed_label(config)}",
        "",
        "## 1. same preparation: predict vs the model step's stored score for the same order",
        f"orders compared: {len(common)} of {len(predicted)}; max |difference|: {worst}",
        "",
        "## 2. same metric: recall of negative reviews at each capacity, labelled orders of the cut",
        f"labelled orders: {len(graded)}; negatives: {sum(truth[row['order_id']] for row in graded)}",
    ]
    for row in table:
        lines.append(
            f"  {row['share']:.0%}: reviewed {row['reviewed']}, caught {row['caught']}, "
            f"recall {row['caught_share']:.4f}"
        )
    if month_row:
        lines.append(f"model step, same month, 10%: recall {month_row['recall']:.4f} "
                     f"({month_row['caught']} of {month_row['negatives']})")
    args.evidence.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
