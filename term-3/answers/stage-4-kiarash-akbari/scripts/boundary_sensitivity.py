#!/usr/bin/env python3
"""How much the reported numbers depend on the split boundary: the model step, re-run per boundary.

``decisions.md`` section 4 moved the boundary from 2018-07-31 to 2018-05-31 and left the sensitivity
run as an open commitment. This script keeps it: it reads the frame once from the database (the same
``capstone.system.read_frame`` the model step uses), then runs ``capstone.system.experiment`` -- split,
rule baseline, roster chosen on the last ``validation_months`` of the training window, refit, one
evaluation -- once per boundary, changing nothing but ``split.boundary``. Nothing is stored in the
database and no model file is written, so the chain's own run and its follow-up list are untouched.

    python scripts/boundary_sensitivity.py                              # 2018-05-31, 2018-06-30, 2018-07-31
    python scripts/boundary_sensitivity.py --boundary 2018-06-30 --tag mine

Writes ``docs/evidence/boundary-sensitivity[.<tag>].txt`` (+ ``.json``) and prints the same text.
The URI is never printed.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from datetime import date
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))

DEFAULT_BOUNDARIES = ("2018-05-31", "2018-06-30", "2018-07-31")
EVIDENCE = STAGE / "docs" / "evidence" / "boundary-sensitivity.txt"
TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


def with_boundary(config, boundary: date):
    """The same config with only ``split.boundary`` replaced (the dataclasses are frozen)."""
    return dataclasses.replace(config, split=dataclasses.replace(config.split, boundary=boundary))


def summary_row(boundary: str, result: dict) -> dict:
    """The comparable numbers of one run, flat, so a table and the JSON cannot disagree."""
    best = result["chosen_model"]
    ev = result["evaluation"][best]
    capacity = {round(row["share"], 2): row["caught_share"] for row in ev["capacity"]}
    rule = result["rule_baseline"]
    rule_cap = {round(row["share"], 2): row["caught_share"] for row in rule["evaluation"]["capacity"]}
    split = result["split"]
    judged = result.get("drift_vs_model", {}).get("last_full_month") or {}
    return {
        "boundary": boundary,
        "train_rows": split["train"]["rows"],
        "train_negatives": split["train"]["negatives"],
        "validation_months": split["validation"]["months"],
        "eval_rows": split["evaluation"]["rows"],
        "eval_labelled": ev["labelled"],
        "eval_negatives": ev["negatives"],
        "eval_months": [m["month"] for m in ev["months"]],
        "chosen_model": best,
        "rule": rule["chosen"]["column"],
        "recall_5": capacity.get(0.05),
        "recall_10": capacity.get(0.1),
        "recall_20": capacity.get(0.2),
        "auc": ev["auc"],
        "rule_recall_10": rule_cap.get(0.1),
        "monthly_recall": {m["month"]: m["recall"] for m in ev["months"]},
        "last_judged_month": judged.get("month"),
        "last_judged_recall": judged.get("recall"),
    }


def render(rows: list[dict], header: list[str]) -> str:
    lines = list(header)
    lines.append("")
    lines.append(
        "| boundary | train rows (neg) | eval rows / labelled (neg) | eval months | chosen | recall@5% "
        "| recall@10% | recall@20% | AUC | rule@10% | last judged month |"
    )
    lines.append("|---|---|---|---|---|---:|---:|---:|---:|---:|---|")
    for r in rows:
        last = (
            f"{r['last_judged_month']} {r['last_judged_recall']:.4f}" if r["last_judged_month"] else "-"
        )
        lines.append(
            f"| {r['boundary']} | {r['train_rows']} ({r['train_negatives']}) | {r['eval_rows']} / "
            f"{r['eval_labelled']} ({r['eval_negatives']}) | {', '.join(r['eval_months'])} | "
            f"{r['chosen_model']} | {r['recall_5']:.4f} | {r['recall_10']:.4f} | {r['recall_20']:.4f} | "
            f"{r['auc']:.4f} | {r['rule_recall_10']:.4f} | {last} |"
        )
    lines.append("")
    lines.append("monthly recall@10% (re-ranked within the month):")
    for r in rows:
        months = ", ".join(f"{m} {v:.4f}" for m, v in r["monthly_recall"].items() if v is not None)
        lines.append(f"  {r['boundary']}: {months}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from capstone.cli import default_config_path
    from capstone.config import load_config
    from capstone.features import connect, database_uri
    from capstone.system import experiment, read_frame

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=default_config_path(STAGE))
    parser.add_argument("--boundary", action="append", help="YYYY-MM-DD (repeatable)")
    parser.add_argument("--tag", help="write boundary-sensitivity.<tag>.txt instead of the default")
    args = parser.parse_args(argv)
    if args.tag and not TAG.match(args.tag):
        print("error: --tag takes letters, digits, '-' and '_' only", file=sys.stderr)
        return 2
    try:
        boundaries = [date.fromisoformat(b) for b in (args.boundary or DEFAULT_BOUNDARIES)]
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    evidence = EVIDENCE.with_name(f"boundary-sensitivity.{args.tag}.txt") if args.tag else EVIDENCE

    config = load_config(args.config)
    connection = connect(database_uri(config), purpose="read the frame for the boundary sensitivity run")
    try:
        frame = read_frame(connection, external=None if config.external.enabled else False)
    finally:
        connection.close()

    rows = []
    for boundary in boundaries:
        result = experiment(frame, with_boundary(config, boundary), with_drift=True)
        rows.append(summary_row(boundary.isoformat(), result))
        print(f"done {boundary.isoformat()}: {rows[-1]['chosen_model']} recall@10% {rows[-1]['recall_10']:.4f}",
              flush=True)

    decided = config.split.boundary.isoformat() if config.split.boundary else "unset"
    header = [
        "# boundary sensitivity (scripts/boundary_sensitivity.py)",
        f"config {config.source.name}; decided boundary {decided}; seed {config.model.random_seed}; "
        f"quota {config.capacity.share:.0%}; frame rows {len(frame)}; roster {', '.join(config.model.families)}",
        "only split.boundary changes between rows; selection is on the last "
        f"{config.model.validation_months} months before each boundary, evaluation on everything after it",
    ]
    text = render(rows, header)
    evidence.write_text(text, encoding="utf-8")
    evidence.with_suffix(".json").write_text(json.dumps({"rows": rows}, indent=2) + "\n", encoding="utf-8")
    print(text)
    print(f"wrote {evidence.relative_to(STAGE)} and {evidence.with_suffix('.json').relative_to(STAGE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
