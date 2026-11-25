#!/usr/bin/env python3
"""The cheating model: the real model plus the tempting columns the audit forbids, measured honestly.

Brief section 1: build, on purpose, a model that also reads the columns we excluded, and put its
metric beside the real model's -- the gap is the cost of a leak. This script is the only code in the
repository that reads those columns, and it reads them straight from ``olist.orders`` (never through
``v_feature_frame``, whose leak guard would refuse them):

* ``order_delivered_customer_date`` -> ``leak_delivery_days`` (approval to delivery) and
  ``leak_late_days`` (delivery minus the promised date): known only after the parcel arrives;
* ``order_delivered_carrier_date`` -> ``leak_carrier_days``: known only after hand-over;
* ``order_status``: its final value ("delivered", "canceled", ...) is written at the end of the
  order's life; at approval every order is "approved".

Same frame, same time split, same model family the model step chose, same 10 % quota. Two cheating
numbers are reported: on the evaluation months *with* the leaked values (what a notebook would
show), and with those values as they really are at the decision moment -- not there yet (blank) and
status "approved" -- which is what the cheating model would score in production.

Writes ``docs/evidence/cheating-model-gap.txt`` (+ ``.json``). Run from the stage folder after the
chain::

    python scripts/cheating_model_gap.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))

TEMPTING_SQL = """
SELECT o.order_id,
       o.order_status AS leak_order_status,
       EXTRACT(EPOCH FROM (o.order_delivered_customer_date - o.order_approved_at)) / 86400.0
           AS leak_delivery_days,
       EXTRACT(EPOCH FROM (o.order_delivered_carrier_date - o.order_approved_at)) / 86400.0
           AS leak_carrier_days,
       EXTRACT(EPOCH FROM (o.order_delivered_customer_date - o.order_estimated_delivery_date)) / 86400.0
           AS leak_late_days
FROM olist.orders AS o
WHERE o.order_approved_at IS NOT NULL
"""
LEAK_NUMERIC = ["leak_delivery_days", "leak_carrier_days", "leak_late_days"]
LEAK_CATEGORICAL = ["leak_order_status"]


def main(argv: list[str] | None = None) -> int:
    import pandas as pd

    from capstone import system
    from capstone.cli import default_config_path
    from capstone.config import load_config
    from capstone.features import connect, database_uri

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=default_config_path(STAGE))
    parser.add_argument("--evidence", type=Path, default=STAGE / "docs/evidence/cheating-model-gap.txt")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    share = float(config.capacity.share)
    seed = int(config.model.random_seed or 0)

    metrics_path = config.features.curated_dir / system.METRICS_NAME
    chosen = json.loads(metrics_path.read_text(encoding="utf-8"))["chosen_model"]

    connection = connect(database_uri(config), purpose="read the frame and the tempting columns")
    try:
        frame = system.read_frame(connection, external=False)
        cursor = connection.cursor()
        cursor.execute(TEMPTING_SQL)
        names = [column.name for column in cursor.description]
        tempting = pd.DataFrame(cursor.fetchall(), columns=names)
    finally:
        connection.close()
    for column in LEAK_NUMERIC:
        tempting[column] = pd.to_numeric(tempting[column], errors="coerce")
    frame = frame.merge(tempting, on="order_id", how="left", validate="one_to_one")

    split = system.time_split(frame, config.split.boundary, config.model.validation_months)
    train, evaluation = frame[split.train], frame[split.evaluation]
    numeric, categorical = system.feature_columns(frame, external=False)
    numeric = [c for c in numeric if c not in LEAK_NUMERIC + LEAK_CATEGORICAL]

    _, real = system.fit_score(chosen, train, evaluation, numeric, categorical, seed)
    cheat_numeric, cheat_categorical = numeric + LEAK_NUMERIC, categorical + LEAK_CATEGORICAL
    cheat_pipeline, cheat = system.fit_score(
        chosen, train, evaluation, cheat_numeric, cheat_categorical, seed
    )
    # the same cheating model, fed what exists at order_approved_at: no delivery yet, status approved
    at_decision = evaluation.copy()
    at_decision[LEAK_NUMERIC] = float("nan")
    at_decision["leak_order_status"] = "approved"
    cheat_live = cheat_pipeline.predict_proba(at_decision[cheat_numeric + cheat_categorical])[:, 1]

    shares = list(config.capacity.report_shares)
    rows = {
        "real model": system.evaluate(evaluation, real, share=share, shares=shares),
        "cheating model, leaked values": system.evaluate(evaluation, cheat, share=share, shares=shares),
        "cheating model, values as at approval": system.evaluate(
            evaluation, cheat_live, share=share, shares=shares
        ),
    }
    lines = [
        "# cheating-model gap (scripts/cheating_model_gap.py)",
        f"model family: {chosen}; split boundary {config.split.boundary}; evaluation window "
        f"{split.describe(frame)['evaluation']['months']}; quota {share:.0%}",
        "tempting columns added: order_status, order_delivered_customer_date (as delivery_days and "
        "late_days), order_delivered_carrier_date (as carrier_days)",
        "",
        "| model | recall@5% | recall@10% | recall@20% | precision@10% | AUC |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, out in rows.items():
        cap = {row["share"]: row["caught_share"] for row in out["capacity"]}
        lines.append(
            f"| {name} | {cap[0.05]:.4f} | {cap[0.1]:.4f} | {cap[0.2]:.4f} | "
            f"{out['operating_point']['precision']:.4f} | {out['auc']:.4f} |"
        )
    real10 = rows["real model"]["operating_point"]["recall"]
    cheat10 = rows["cheating model, leaked values"]["operating_point"]["recall"]
    live10 = rows["cheating model, values as at approval"]["operating_point"]["recall"]
    lines += [
        "",
        f"gap at the 10% quota, leaked notebook number minus real: {cheat10 - real10:+.4f} "
        f"({cheat10:.4f} vs {real10:.4f})",
        f"what the cheating model actually delivers at approval time: {live10:.4f} "
        f"({live10 - real10:+.4f} vs the real model)",
    ]
    args.evidence.write_text("\n".join(lines) + "\n", encoding="utf-8")
    args.evidence.with_suffix(".json").write_text(
        json.dumps({name: {k: v for k, v in out.items() if k != "months"} for name, out in rows.items()},
                   indent=2, default=str),
        encoding="utf-8",
    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
