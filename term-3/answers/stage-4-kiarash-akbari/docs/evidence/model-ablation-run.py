#!/usr/bin/env python3
"""The phase-5 measurement: baselines, families, the quota, and the external signal's ablation.

Run it where the data is::

    PYTHONPATH=src CAPSTONE_DATABASE_URI=postgresql+psycopg://… \\
      python3 docs/evidence/model-ablation-run.py --config config/capstone.toml

It reads the frame and its label through :func:`capstone.model.read_frame` -- one query, the two
views joined, the frame's separation from the label preserved -- and then scores the same rows over
and over: every scorer against every feature set, once with the ``external_*`` columns and once
without them, plus a third set that adds only the 614 density number. That contrast *is* the
deliverable. The brief's ``[external]`` block asks whether the external signal helped, and the only
honest answer this project can produce without a forward-in-time experiment is the difference
between two runs of the same arithmetic on the same rows.

What it deliberately does not do:

* it does not zero-fill or drop NULLs -- ``numeric`` refuses them, and :option:`--omit` is where a
  decision to leave a column out gets recorded, so the report can print what was excluded;
* the external signal is read through the bridge view of ``sql/006`` (see ``decisions.md`` §13), never
  by editing ``v_feature_frame``. A database where the bridge is absent, or present with no matched
  rows, is a *reported* state: every family refuses on a NULL column, so this runs ``train`` with
  ``on_refusal="record"`` and prints which arms refused and what the coverage view counted. A refusal
  is not a zero, and a coverage of zero is not a lift;
* it does not claim a lift; section 4 prints differences and the sentence that qualifies them;
* it does not fail when scikit-learn is missing. The two families are the point of that section, so
  it says the module is not importable and exits 0 -- a machine without the project's lock has not
  failed a measurement, and a red run there would read as a broken model rather than an absent one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

STAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(STAGE / "src"))

from capstone.capacity import capacity_table  # noqa: E402
from capstone.config import load_contract  # noqa: E402
from capstone.errors import CapstoneError  # noqa: E402
from capstone.exit_codes import ExitCode  # noqa: E402
from capstone.model import (  # noqa: E402
    DENSITY_COLUMN,
    EXTERNAL_MODES,
    PRIOR_RATE,
    Scorer,
    external_readiness,
    label_definition,
    read_frame,
    train,
)

#: the two families the ROADMAP's phase-5 row names, as the brief's "two families, not two
#: settings" reads on scikit-learn. A reader who prefers other estimators edits this dict; the
#: module refuses a list whose families collapse to one, so the edit cannot silently cheat.
FAMILIES = {
    # max_iter because the solver's 100-step default does not converge on 14k labelled orders,
    # and a non-converged linear model would be reported as a worse model rather than as an
    # unfinished fit -- the difference only matters to whoever reads the table
    "logreg.l2": ("sklearn.linear_model", "LogisticRegression", {"max_iter": 1000}),
    "gbdt.depth3": ("sklearn.ensemble", "HistGradientBoostingClassifier", {}),
}


def rule(text: str) -> None:
    print(f"\n{'-' * 72}\n{text}\n{'-' * 72}")


def bullet(value: Any) -> str:
    if value is None:
        return "not defined (empty denominator)"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def build_families() -> tuple[list[Scorer], list[str]]:
    """Import the estimators, and say which ones could not be built.

    Returns the scorers and the names that were skipped. A class that exists but cannot be
    constructed (a version that renamed a keyword argument) is reported the same way as a missing
    module: both mean the family was not measured, and neither belongs in the report as a zero.
    """
    import importlib

    out: list[Scorer] = []
    skipped: list[str] = []
    for name, spec in FAMILIES.items():
        module_name, class_name, kwargs = spec
        try:
            module = importlib.import_module(module_name)
            estimator = getattr(module, class_name)(**kwargs)
        except Exception as exc:  # noqa: BLE001 - the whole point is to report, not to guess
            print(f"  skipped {name}: {type(exc).__name__}: {exc}")
            skipped.append(name)
            continue
        out.append(Scorer.family(name, estimator))
    return out, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=STAGE / "config" / "capstone.example.toml")
    parser.add_argument(
        "--share",
        type=float,
        default=None,
        help="override the operational quota for this run (default: capacity.share from config)",
    )
    parser.add_argument(
        "--external",
        choices=EXTERNAL_MODES,
        default="auto",
        help="auto: join the bridge view of sql/006 if the database has it; off: never join it; "
        "required: refuse if it is missing",
    )
    parser.add_argument(
        "--require-matched",
        action="store_true",
        help="keep only the orders the bridge matched -- this changes the population, so n_orders "
        "below is read beside this flag",
    )
    parser.add_argument(
        "--omit",
        default="",
        help="comma-separated frame columns to leave out of every feature set, e.g. the ones with NULLs",
    )
    args = parser.parse_args(argv)

    try:
        config = load_contract(args.config)
    except CapstoneError as exc:
        print(f"error: {exc}")
        return int(66)

    print(f"config: {args.config}")
    print(f"label:  {label_definition()}")
    print(f"quota:  {config.capacity.share if args.share is None else args.share}")

    omit = tuple(name for name in args.omit.split(",") if name.strip())
    try:
        frame = read_frame(
            config,
            omit=omit,
            external=args.external,
            require_matched=args.require_matched,
        )
    except CapstoneError as exc:
        print(f"\nerror: {exc}")
        return int(ExitCode.CONFIG)

    families, skipped = build_families()

    rule("0. what this run can and cannot witness")
    print(f"  frame rows: {len(frame.rows)}")
    print(f"  feature columns: {len(frame.columns)}")
    labelled = sum(1 for row in frame.rows if row["is_negative"] is not None)
    print(f"  labelled rows: {labelled}  (unlabelled = D2's separate population, reported as a count)")
    print(f"  external columns in the frame: {', '.join(frame.external_columns()) or 'none'}")
    if args.external != "off":
        # a second catalog/aggregate read, never a frame read: this is the sentence that tells the
        # reader whether the numbers below come from a signal or from an empty bridge
        ready = external_readiness(config)
        print(f"  bridge: {ready['note']}")
        if ready["coverage"] is not None:
            cov = ready["coverage"]
            print(
                f"  bridge coverage: {cov['matched_rows']} of {cov['frame_rows']} orders matched, "
                f"{cov['valued_rows']} carry a value"
            )
    if skipped:
        print(f"  families NOT measured here: {', '.join(skipped)}")
        print("  sections 2 to 4 still run for the baseline alone, and every family row is absent")
        print("  rather than zero -- install scikit-learn and re-run to fill them in")

    rule("1. the feature sets the ablation is defined over")
    for name, columns in frame.feature_sets().items():
        print(f"  {name:<14} {len(columns)} columns: {', '.join(columns)}")
    if DENSITY_COLUMN not in frame.columns:
        print(f"  note: {DENSITY_COLUMN} is not in this read, so the density-only contrast is absent")
        if args.external == "off":
            print("        --external=off asked for that; the frame itself was left alone by design")
        else:
            print("        apply sql/006_external_bridge.sql and re-run -- 003 and 005 never put it here")

    try:
        result = train(
            config,
            frame,
            scorers=families,
            baseline_rules=(PRIOR_RATE,),
            share=args.share,
            omit=omit,
            # record, not raise: on a database whose bridge is empty the NULL column makes every
            # family refuse, and a traceback there would hide the one number worth printing -- the
            # coverage above -- behind an exit code. train() still refuses to rank a refused row.
            on_refusal="record",
        )
    except CapstoneError as exc:
        print(f"\nerror: {exc}")
        return int(ExitCode.NOT_READY)

    rule("2. the comparison table, at the operational quota")
    print("  scorer            set            catch share   AUC       AP        tp  fp  fn  tn")
    for run in sorted(result["runs"], key=table_order):
        q = run["quota"]
        print(
            f"  {run['scorer']:<17} {run['feature_set']:<14} {bullet(q['recall']):<13} "
            f"{bullet(run['auc']):<9} {bullet(run['average_precision']):<9} "
            f"{q['tp']:>3} {q['fp']:>3} {q['fn']:>3} {q['tn']:>4}"
        )
    print(f"  rows scored: {result['rows']}   negatives: {result['negatives']}   unlabelled: {result['unlabelled_rows']}")
    print(f"  best by the brief's primary metric: {result['best']['model_run_id']}")

    rule("3. the month spread of the best run (quota re-ranked inside each month)")
    for row in result["best"].get("months", []):
        print(
            f"  {row['month']}  rows {row['rows']:>6}  negatives {row['negatives']:>6}  "
            f"reviewed {row['reviewed']:>5}  caught {row['caught']:>5}  share {bullet(row['catch_share'])}"
        )
    if not result["best"].get("months"):
        print("  skipped: the frame carries no decision_month tags, so 6.6 has nothing to spread over")

    rule("4. the external signal, as an ablation of the runs above")
    contrasts = result["ablation"]["contrasts"]
    if not contrasts:
        print("  skipped: no scorer was run on both feature sets, so there is no difference to print")
    for contrast in contrasts:
        print(
            f"  {contrast['scorer']:<17} catch share {bullet(contrast['catch_share_without'])} -> "
            f"{bullet(contrast['catch_share_with'])}   delta {bullet(contrast['delta_catch_share'])}"
            f"   |   AUC {bullet(contrast['auc_without'])} -> {bullet(contrast['auc_with'])}"
            f"   delta {bullet(contrast['delta_auc'])}"
        )
    density = [run for run in result["runs"] if run["feature_set"] == "density_only"]
    if density:
        outside = {
            run["scorer"]: run
            for run in result["runs"]
            if run["feature_set"] == "no_external"
        }
        print(f"  the 614 number alone, beside the same rows without it ({DENSITY_COLUMN}):")
        for run in density:
            pair = outside.get(run["scorer"])
            if pair is None:
                continue
            print(
                f"    {run['scorer']:<17} catch share {bullet(pair['quota']['recall'])} -> "
                f"{bullet(run['quota']['recall'])}   delta "
                f"{bullet(subtract(run['quota']['recall'], pair['quota']['recall']))}"
            )
    for refused in result["refused_runs"]:
        print(
            f"  refused: {refused['scorer']} on {refused['feature_set']}: {refused['error'].splitlines()[0]}"
        )
    # one word, four states, and a sentence that does not let the vacuous one read as a finding
    status = result["ablation"]["status"]
    gloss = {
        "measured": "both arms of every row below were scored",
        "partial": "some arms refused -- the differences printed are only the pairs that were scored",
        "refused": "no arm was scored, so this section carries no difference at all",
        "absent": "no scorer ran on both feature sets, so there was nothing to compare",
    }[status]
    print(f"  ablation status: {status} -- {gloss}")
    print(f"  reading: {result['ablation']['reading']}")

    rule("5. the capacity table, from the best run's own scores")
    labels = [bool(row["is_negative"]) for row in frame.rows if row["is_negative"] is not None]
    for row in capacity_table(labels, result["best"]["scores"], list(config.capacity.report_shares)):
        print(
            f"  share {row['share']:.2f}: reviewed {row['reviewed']:>6}  caught {row['caught']:>6}  "
            f"share of negatives {bullet(row['caught_share'])}  extra work {row['extra']:>6}"
        )
    print("  note: this section recomputes the table from the best run's scores; capacity.report")
    print("        is what writes the secondary-metric and cheating-model columns, and it is the")
    print("        same three lines of arithmetic")

    checks = [
        ("every run has a quota row", all("quota" in run for run in result["runs"])),
        (
            "a refused run is never the best row",
            "error" not in result["best"] and result["best"]["auc"] is not None,
        ),
        (
            "the ablation is a difference of rows in section 2, not a new measurement",
            all(any(r["scorer"] == c["scorer"] for r in result["runs"]) for c in contrasts),
        ),
        ("the frame's label never entered a feature set", all(
            name not in {"is_negative", "worst_score", "review_count", "has_review"}
            for run in result["runs"]
            for name in run["features"]
        )),
    ]
    rule("checks")
    failed = 0
    for name, ok in checks:
        print(f"  check[{name}]: {'ok' if ok else 'FAILED'}")
        failed += 0 if ok else 1
    return int(bool(failed))


def table_order(run: dict) -> tuple[float, float, str, str]:
    """Best first, and an undefined metric last -- never treated as zero.

    The sort has to put a ``None`` catch share below every measured one: an empty denominator is
    not a model that scored nothing, and a comparison table that ranks it as ``0.0`` hands a
    reader a loser that was never in the race.
    """
    catch = run["quota"]["recall"]
    rank = -1.0 if catch is None else -catch
    auc_value = -1.0 if run["auc"] is None else -run["auc"]
    return (rank, auc_value, run["scorer"], run["feature_set"])


def subtract(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


if __name__ == "__main__":
    raise SystemExit(main())
