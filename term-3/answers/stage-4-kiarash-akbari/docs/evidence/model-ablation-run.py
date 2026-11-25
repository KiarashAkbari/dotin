#!/usr/bin/env python3
"""The external signal's ablation, on the same time split as the model step, for every family.

Run it from the stage folder after the chain::

    CAPSTONE_DATABASE_URI=postgresql://... python3 docs/evidence/model-ablation-run.py

The earlier version of this harness fitted and scored the *same* rows (``capstone.model.train``): an
in-sample number, which says how well a model memorises, not how well it ranks orders it has not
seen. This version reuses the model step's own pieces from :mod:`capstone.system` -- the frame read
from the database (``read_frame``), the time split on ``split.boundary`` (``time_split``), the
pipelines (``fit_score``) and the metric (``evaluate`` / ``paired_bootstrap``) -- so the numbers it
prints are comparable with ``docs/evidence/model-run.json`` and with nothing else:

* every family in ``model.families`` is fitted on the whole training window (<= boundary) and
  scored on the evaluation months after it, once without and once with ``external_density_2010``;
* the difference at the operating quota gets a paired bootstrap interval (same orders resampled for
  both arms), because a difference smaller than its interval is not a finding;
* coverage of the external column is printed first: a NULL density is left NULL (the pipeline's
  imputer adds a missing indicator), never zero-filled.

It writes ``docs/evidence/model-ablation-run.txt`` and prints the same text.
"""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

STAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(STAGE / "src"))


def reading(rows: list[dict]) -> str:
    """What the table says, composed from the intervals in it -- not one sentence about intervals.

    The old line explained what an interval covering 0 would mean, under a table where three families
    covered it and one did not. A reader who wanted the result had to do the comparison by eye, which is
    how a mixed witness gets quoted as "no effect": the honest summary of these numbers is *which*
    families are inconclusive and which one is not, and in which direction.

    Pure on purpose: the caller feeds it the rows it just printed, so the sentence and the table cannot
    come from different runs -- and so the test can hand it a table without fitting a model.

    An empty table is not a table of inconclusive families. It is the shape a config with no roster
    produces, and "no family moved measurably" would dress that up as a finding.
    """
    if not rows:
        return (
            "reading: nothing was measured -- the table above has no families in it, so these numbers "
            "say nothing about the external signal; check [model] families in the config named above"
        )
    inside = [r for r in rows if r["ci"] is None or (r["ci"]["low"] <= 0.0 <= r["ci"]["high"])]
    outside = [r for r in rows if r not in inside]
    if not outside:
        return (
            f"reading: no family moved measurably -- all {len(rows)} 95% intervals cover 0, so at this "
            "quota the external signal changes neither how many negative reviews the 10% list catches "
            "nor, within noise, how the list is ranked"
        )
    named = ", ".join(
        f"{r['name']} ({r['delta']:+.4f}, {r['ci']['low']:+.4f} .. {r['ci']['high']:+.4f})" for r in outside
    )
    signs = {1 if r["delta"] > 0 else -1 for r in outside}
    direction = "raises" if signs == {1} else ("lowers" if signs == {-1} else "moves")
    tail = (
        "; the other {} stay inside their own intervals and are inconclusive: {}".format(
            len(inside), ", ".join(r["name"] for r in inside)
        )
        if inside
        else "; every family in the table excludes 0"
    )
    return (
        f"reading: {len(outside)} of {len(rows)} families have an interval that excludes 0 -- {named} "
        f"-- and it {direction} recall@10% on the evaluation months{tail}. A lift is measured here, on "
        "the model's own split and quota; the coverage line above it is only the right to ask the question."
    )


def main(argv: list[str] | None = None) -> int:
    from capstone import system
    from capstone.cli import default_config_path
    from capstone.config import load_config
    from capstone.features import connect, database_uri

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=default_config_path(STAGE))
    parser.add_argument("--evidence", type=Path, default=STAGE / "docs/evidence/model-ablation-run.txt")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    share = float(config.capacity.share)
    shares = list(config.capacity.report_shares)
    roster = system.roster_of(config)
    seed_label = system.seed_label(config)
    seed = int(config.model.random_seed)

    connection = connect(database_uri(config), purpose="read the frame for the ablation")
    try:
        frame = system.read_frame(connection, external=True)
    finally:
        connection.close()

    split = system.time_split(frame, config.split.boundary, config.model.validation_months)
    train_rows, eval_rows = frame[split.train], frame[split.evaluation]
    numeric, categorical = system.feature_columns(frame, external=False)
    ext_numeric, _ = system.feature_columns(frame, external=True)

    out = io.StringIO()
    with redirect_stdout(out):
        print("# external-signal ablation on the time split (docs/evidence/model-ablation-run.py)")
        print(f"boundary {config.split.boundary}; train {len(train_rows)} rows, evaluation {len(eval_rows)} rows; "
              f"quota {share:.0%}; {seed_label}; roster {' '.join(roster)}")
        print(f"external column: {system.EXTERNAL_COLUMN}; coverage over all approved orders "
              f"{frame[system.EXTERNAL_COLUMN].notna().mean():.4f} (NULL stays NULL)")
        print()
        print("| family | recall@10% without | recall@10% with | delta | 95% bootstrap of delta | AUC without | AUC with |")
        print("|---|---:|---:|---:|---|---:|---:|")
        measured: list[dict] = []
        for name in config.model.families:
            _, without = system.fit_score(name, train_rows, eval_rows, numeric, categorical, seed)
            _, with_ext = system.fit_score(name, train_rows, eval_rows, ext_numeric, categorical, seed)
            a = system.evaluate(eval_rows, without, share=share, shares=shares)
            b = system.evaluate(eval_rows, with_ext, share=share, shares=shares)
            ci = system.paired_bootstrap(eval_rows, with_ext, without, share, seed=seed)
            ra, rb = a["operating_point"]["recall"], b["operating_point"]["recall"]
            interval = f"{ci['low']:+.4f} .. {ci['high']:+.4f}" if ci else "n/a"
            print(f"| {name} | {ra:.4f} | {rb:.4f} | {rb - ra:+.4f} | {interval} | {a['auc']:.4f} | {b['auc']:.4f} |")
            measured.append(
                {
                    "name": name,
                    "delta": rb - ra,
                    "ci": {"low": ci["low"], "high": ci["high"]} if ci else None,
                }
            )
        print()
        print(reading(measured))
    text = out.getvalue()
    print(text, end="")
    args.evidence.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
