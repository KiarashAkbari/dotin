"""The end of the analysis: one look at the holdout, the subgroups, and the card.

Three things the challenge asks for are easy to do superficially, so they are
functions here with tests rather than prose in a notebook cell:

* **the holdout is looked at once**, which means the decision to stop tuning has to be
  a single call whose inputs are recorded (:func:`holdout_report`);
* **fairness across subgroups**, which needs a band definition that is not fitted on
  anything (:data:`AGE_BANDS` is a business grouping, stated in code before any score
  exists), and a per-band cost, not just a per-band accuracy;
* **where it fails**, which has to be three concrete situations with numbers
  attached, not a paragraph about "more data would help".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .cost import (
    CHALLENGE_MATRIX,
    CostMatrix,
    auc_rank,
    binary_counts,
    constant_baselines,
    cost_per_1000,
    matrix_sensitivity,
    metrics,
    reduction_vs_baseline,
)

#: half-open [low, high) bands, the last one open-ended; a 10-year width is the
#: grouping a credit policy would actually defend, not a quantile of this dataset
AGE_BANDS: tuple[tuple[int, int | None], ...] = (
    (0, 25),
    (25, 35),
    (35, 45),
    (45, 55),
    (55, 65),
    (65, None),
)
BAND_COLUMN = "age_band"
MISSING_AGE_BAND = "age_unreported"
#: the concentration check: how much of the default mass sits in this slice of scores
TOP_SLICE = 0.10


def define_bands(frame: pd.DataFrame, *, column: str = "age") -> pd.Series:
    """Label rows by age band. Unfitted, and a NULL age gets its own band.

    A missing age is not quietly put in the youngest bucket: an applicant who did not
    report an age is a different population, and mixing them into a band would hide a
    fairness finding rather than show one.
    """
    values = pd.to_numeric(frame[column], errors="coerce")
    labels = pd.Series(MISSING_AGE_BAND, index=frame.index, dtype=object)
    for low, high in AGE_BANDS:
        mask = values >= low if high is None else (values >= low) & (values < high)
        labels[mask.fillna(False)] = f"{low}-{high}" if high is not None else f"{low}+"
    return labels


def _ordered_counts(counts: dict[str, int]) -> dict[str, int]:
    return {key: int(counts[key]) for key in ("tp", "fp", "tn", "fn")}


def baseline_costs(
    truth: object, *, matrix: CostMatrix = CHALLENGE_MATRIX
) -> dict[str, float]:
    """The two no-learning rules, keyed by the name each one reports.

    ``cost.constant_baselines`` keys its result by dict key and its rows by a ``name``
    field, and the two spellings differ; reading the names rather than the keys is what
    keeps this working when the wording is edited, and it is why the floor the report
    quotes comes from here rather than from a cell.
    """
    entries = constant_baselines(truth, matrix=matrix)
    return {
        str(entry["name"]): float(entry["cost_per_1000"]) for entry in entries.values()
    }


@dataclass(frozen=True)
class HoldoutReport:
    """One look at the reserved data, with everything that decision needs."""

    rows: int
    positives: int
    threshold: float
    family: str
    matrix: str
    counts: dict[str, int]
    cost_per_1000: float
    metrics: dict[str, float]
    auc: float
    baselines: dict[str, float]
    reduction_vs_flagging_nobody_pct: float
    cv_mean_cost: float | None
    cv_std_cost: float | None
    sensitivity: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "positives": self.positives,
            "threshold": round(self.threshold, 4),
            "family": self.family,
            "matrix": self.matrix,
            "counts": _ordered_counts(self.counts),
            "cost_per_1000": round(self.cost_per_1000, 3),
            "metrics": {
                k: round(v, 6) for k, v in self.metrics.items() if k != "undefined"
            },
            "undefined_metrics": list(self.metrics.get("undefined", [])),
            "auc": round(self.auc, 4),
            "baselines_cost_per_1000": {
                k: round(v, 3) for k, v in self.baselines.items()
            },
            "reduction_vs_flagging_nobody_pct": round(
                self.reduction_vs_flagging_nobody_pct, 2
            ),
            "cv_mean_cost": None
            if self.cv_mean_cost is None
            else round(self.cv_mean_cost, 3),
            "cv_std_cost": None
            if self.cv_std_cost is None
            else round(self.cv_std_cost, 3),
            "cv_holdout_gap": (
                None
                if self.cv_mean_cost is None
                else round(self.cost_per_1000 - self.cv_mean_cost, 3)
            ),
            "matrix_sensitivity": self.sensitivity,
        }


def holdout_report(
    scores: Sequence[float] | np.ndarray,
    truth: Sequence[int] | np.ndarray,
    *,
    threshold: float,
    family: str,
    matrix: CostMatrix = CHALLENGE_MATRIX,
    cv: tuple[float, float] | None = None,
    sensitivity_matrix: CostMatrix | None = None,
) -> HoldoutReport:
    """Score the reserved rows once, at the threshold chosen on validation data.

    ``cv`` is the (mean, std) from cross-validation, passed in so the gap between
    "what the folds said" and "what the holdout said" is part of the same record as
    the number it explains - a holdout cost above the CV mean is the honest headline,
    not something to average away.
    """
    scores_array = np.asarray(scores, dtype="float64")
    truth_array = np.asarray(truth, dtype="int64")
    if scores_array.shape != truth_array.shape:
        raise ValueError(
            f"length mismatch: {scores_array.shape} vs {truth_array.shape}"
        )
    flags = (scores_array >= float(threshold)).astype("int64")
    counts = binary_counts(truth_array, flags)
    cost = cost_per_1000(counts, matrix, n=len(truth_array))
    baselines = baseline_costs(truth_array, matrix=matrix)
    nobody = baselines["flag-nobody"]
    matrices = [matrix] if sensitivity_matrix is None else [matrix, sensitivity_matrix]
    sensitivity = matrix_sensitivity(truth_array, scores_array, matrices)
    return HoldoutReport(
        rows=len(truth_array),
        positives=int(truth_array.sum()),
        threshold=float(threshold),
        family=family,
        matrix=matrix.name,
        counts=counts,
        cost_per_1000=cost,
        metrics=metrics(counts),
        auc=auc_rank(truth_array, scores_array),
        baselines=dict(baselines),
        reduction_vs_flagging_nobody_pct=reduction_vs_baseline(nobody, cost),
        cv_mean_cost=None if cv is None else float(cv[0]),
        cv_std_cost=None if cv is None else float(cv[1]),
        sensitivity=sensitivity,
    )


def subgroup_table(
    frame: pd.DataFrame,
    truth: Sequence[int] | np.ndarray,
    flags: Sequence[int] | np.ndarray,
    scores: Sequence[float] | np.ndarray,
    *,
    matrix: CostMatrix = CHALLENGE_MATRIX,
    band_column: str = BAND_COLUMN,
) -> pd.DataFrame:
    """Cost per 1,000 by band - the fairness view the challenge asks for.

    Accuracy per subgroup is the wrong lens here: at a 12:1 matrix a band can have
    fine accuracy and be the one where the model misses defaults, which is where the
    money and the harm actually are. ``flag_rate`` is included next to
    ``positive_rate`` because a band that is flagged far above its own base rate is
    being over-denied, whatever the average cost says.
    """
    work = frame.copy()
    work["_truth"] = np.asarray(truth, dtype="int64")
    work["_flag"] = np.asarray(flags, dtype="int64")
    work["_score"] = np.asarray(scores, dtype="float64")
    if band_column not in work.columns:
        work[band_column] = define_bands(work)
    rows: list[dict[str, object]] = []
    for label, part in work.groupby(band_column, sort=True, dropna=False):
        counts = binary_counts(part["_truth"], part["_flag"])
        rows.append(
            {
                band_column: str(label),
                "rows": len(part),
                "positive_rate": round(float(part["_truth"].mean()), 6),
                "flag_rate": round(float(part["_flag"].mean()), 6),
                "recall": round(float(counts["tp"] / part["_truth"].sum()), 6)
                if part["_truth"].sum()
                else None,
                "precision": round(float(counts["tp"] / part["_flag"].sum()), 6)
                if part["_flag"].sum()
                else None,
                "cost_per_1000": round(cost_per_1000(counts, matrix, n=len(part)), 3),
                "mean_score": round(float(part["_score"].mean()), 6),
            }
        )
    return pd.DataFrame(rows)


def failure_concentration(
    scores: Sequence[float] | np.ndarray,
    truth: Sequence[int] | np.ndarray,
    *,
    threshold: float,
    slice_share: float = TOP_SLICE,
) -> dict[str, object]:
    """Where the misses are, in numbers.

    ``defaults_below_threshold`` is the count that costs the most per row under the
    matrix, and ``lift_in_top_slice`` says how much better than chance the top of the
    ranking is - a model can beat the flag-nobody baseline overall while concentrating
    nothing at the top, which is what "it fails quietly" means in practice.
    """
    scores_array = np.asarray(scores, dtype="float64")
    truth_array = np.asarray(truth, dtype="int64")
    if scores_array.size == 0:
        raise ValueError("no rows to examine")
    if not 0 < slice_share <= 1:
        raise ValueError("slice_share must be in (0, 1]")
    cut = int(np.floor(scores_array.size * slice_share))
    cut = max(cut, 1)
    order = np.argsort(-scores_array, kind="stable")
    top = order[:cut]
    positives = int(truth_array.sum())
    top_positives = int(truth_array[top].sum())
    base_rate = positives / truth_array.size
    below = truth_array[scores_array < float(threshold)]
    return {
        "rows": int(scores_array.size),
        "positives": positives,
        "positive_rate": round(base_rate, 6),
        "slice_share": slice_share,
        "slice_rows": cut,
        "slice_positives": top_positives,
        "lift_in_top_slice": round((top_positives / cut) / base_rate, 4)
        if base_rate
        else None,
        "defaults_below_threshold": int((below == 1).sum()),
        "flagged_rows": int((scores_array >= float(threshold)).sum()),
        "mean_score_positives": round(float(scores_array[truth_array == 1].mean()), 6)
        if positives
        else None,
        "mean_score_negatives": round(float(scores_array[truth_array == 0].mean()), 6)
        if positives < scores_array.size
        else None,
    }


def where_it_fails(
    concentration: dict[str, object],
    subgroups: pd.DataFrame,
    *,
    matrix: CostMatrix = CHALLENGE_MATRIX,
    band_column: str = BAND_COLUMN,
) -> list[str]:
    """Three concrete failure situations, each with the number that says so.

    Built from the measurements rather than from adjectives, so the section cannot
    survive as boilerplate: if a finding stops being true, the sentence changes.
    """
    lines: list[str] = []
    misses = int(concentration["defaults_below_threshold"])
    lines.append(
        f"1. Applicants the score leaves under the threshold who still default: {misses} of "
        f"{concentration['positives']} defaults. Each one costs {matrix.false_negative:g} units, "
        f"so this slice is the whole argument for moving the threshold down."
    )
    lift = concentration.get("lift_in_top_slice")
    if lift is not None:
        lines.append(
            f"2. Weakness at the top of the ranking: the {int(concentration['slice_share'] * 100)}% "
            f"highest scores hold defaults {lift:g}x their share, which is far from perfect "
            f"separation - the model sorts, it does not identify."
        )
    if len(subgroups):
        worst = subgroups.loc[subgroups["cost_per_1000"].astype(float).idxmax()]
        best = subgroups.loc[subgroups["cost_per_1000"].astype(float).idxmin()]
        lines.append(
            f"3. Uneven cost across groups: {worst[band_column]} pays "
            f"{worst['cost_per_1000']:g} per 1,000 against {best['cost_per_1000']:g} in "
            f"{best[band_column]}, with recall "
            f"{_fmt_rate(worst.get('recall'))} vs {_fmt_rate(best.get('recall'))}. A single "
            f"threshold is a single number applied to populations the model is not equally sure about."
        )
    return lines


def _fmt_rate(value: object) -> str:
    return (
        "n/a"
        if value is None or (isinstance(value, float) and np.isnan(value))
        else f"{float(value):g}"
    )


def model_card(
    report: HoldoutReport,
    *,
    dataset_line: str,
    preparation_line: str,
    feature_lines: Sequence[str],
    trained_rows: int,
    code_version: str,
    intended_use: str,
    excluded_uses: Sequence[str],
) -> str:
    """Half a page to a page of card, in markdown, from the numbers that exist.

    Every line is filled from a measured object; nothing here is a promise about
    behaviour nobody tested, which is how most model cards become decoration.
    """
    numeric = {
        key: value
        for key, value in report.metrics.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    metrics_rows = "\n".join(f"| {key} | {value:g} |" for key, value in numeric.items())
    exclusions = "\n".join(f"- {line}" for line in excluded_uses) or "- (none recorded)"
    features = "\n".join(f"- {line}" for line in feature_lines) or "- (none)"
    return f"""## Model card

| field | value |
|---|---|
| dataset | {dataset_line} |
| rows fitted | {trained_rows} |
| rows held out and looked at once | {report.rows} |
| preparation | {preparation_line} |
| family | {report.family} |
| operating point | score >= {report.threshold:.4f}, cost matrix `{report.matrix}` |
| cost per 1,000 (holdout) | {report.cost_per_1000:.2f} |
| flag-nobody baseline | {report.baselines["flag-nobody"]:.2f} |
| reduction vs baseline | {report.reduction_vs_flagging_nobody_pct:.2f}% |
| AUC | {report.auc:.4f} |
| code version | {code_version} |

**Intended use.** {intended_use}

**Inputs.**
{features}

**Metrics at the chosen threshold** - reported because the cost depends on them, not
because any one of them selects the model:

| metric | value |
|---|---|
{metrics_rows}

**Not for:**
{exclusions}
"""


#: why each familiar metric is not enough on its own - printed by the evaluation
#: section, kept here so the prose and the numbers cannot be edited apart
METRIC_LIMITATIONS: dict[str, str] = {
    "accuracy": "at 12:1 it rewards missing defaults; the trivial answer scores well",
    "precision": "silent about the defaults that were not flagged, the expensive half",
    "recall": "silent about how many clean applicants were pulled in to catch them",
    "f1": "weights the two errors equally, and this matrix says they are not equal",
    "auc": "threshold-free, so it cannot price the operating point actually used",
    "flag_rate": "a policy choice shown as a measurement; read it next to cost",
}


def metrics_table(report: HoldoutReport) -> str:
    """Metric, its value at the chosen threshold, and what it cannot say."""
    lines = ["| metric | value | why it is not enough alone |", "|---|---|---|"]
    values = dict(report.metrics)
    values["auc"] = report.auc
    for key, note in METRIC_LIMITATIONS.items():
        if key in values:
            lines.append(f"| {key} | {values[key]:.4f} | {note} |")
    return "\n".join(lines)
