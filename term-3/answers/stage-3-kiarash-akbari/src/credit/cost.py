"""Cost-sensitive decisions: the counts, the money, and the threshold the money picks.

The challenge fixes the matrix - a missed default costs 12 units, a false alarm
costs 1 - and makes that matrix the objective. Keeping it in one module means
the number a reader is asked to trust (cost per 1,000 applicants) has exactly one
implementation, pinned by hand-computed tests instead of by a library's default.

Accuracy is not a selection criterion anywhere in this module. It is reported by
``metrics`` only so the notebook can show how far it drifts from cost.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

THRESHOLD_GRID_SIZE = 201
#: 0.00 .. 1.00 by 0.005; the library default 0.5 sits inside it, unfavoured
THRESHOLD_GRID = np.round(np.linspace(0.0, 1.0, THRESHOLD_GRID_SIZE), 3)
COST_REPORT_SCALE = 1_000


@dataclass(frozen=True)
class CostMatrix:
    """Unit cost of each cell. True predictions are free in this challenge."""

    false_positive: float = 1.0
    false_negative: float = 12.0
    true_positive: float = 0.0
    true_negative: float = 0.0
    name: str = "12:1"

    @property
    def ratio(self) -> float:
        """How many false alarms one missed default pays for."""
        if self.false_positive == 0:
            return float("inf")
        return self.false_negative / self.false_positive


#: the matrix the challenge sets
CHALLENGE_MATRIX = CostMatrix()
#: the alternative the challenge asks about, to show how the threshold moves
FLATTER_MATRIX = CostMatrix(false_positive=1.0, false_negative=4.0, name="4:1")


def _as_binary(values: Sequence[object] | np.ndarray, label: str) -> np.ndarray:
    """Coerce to 0/1 integers, refusing anything that was not a clean flag.

    A silent truncation here would turn probabilities into labels, so a value
    like 0.9 is an error rather than a zero.
    """
    try:
        numeric = np.asarray(values, dtype="float64")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must hold 0/1 values, got non-numeric entries") from exc
    if not np.isin(numeric, (0.0, 1.0)).all():
        raise ValueError(f"{label} must be 0/1; some values are neither")
    return numeric.astype("int64")


def binary_counts(
    y_true: Sequence[object] | np.ndarray, y_pred: Sequence[object] | np.ndarray
) -> dict[str, int]:
    """Absolute confusion counts, the only numbers the cost is computed from."""
    truth = _as_binary(y_true, "y_true")
    pred = _as_binary(y_pred, "y_pred")
    if truth.shape != pred.shape:
        raise ValueError(f"length mismatch: {truth.shape} vs {pred.shape}")
    positive = truth == 1
    flagged = pred == 1
    tp = int(np.count_nonzero(positive & flagged))
    fn = int(np.count_nonzero(positive & ~flagged))
    fp = int(np.count_nonzero(~positive & flagged))
    tn = int(np.count_nonzero(~positive & ~flagged))
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn}


def total_cost(counts: Mapping[str, int], matrix: CostMatrix = CHALLENGE_MATRIX) -> float:
    return float(
        counts["fp"] * matrix.false_positive
        + counts["fn"] * matrix.false_negative
        + counts["tp"] * matrix.true_positive
        + counts["tn"] * matrix.true_negative
    )


def cost_per_1000(
    counts: Mapping[str, int], matrix: CostMatrix = CHALLENGE_MATRIX, *, n: int | None = None
) -> float:
    """The report's headline unit: expected cost for a thousand applicants."""
    rows = int(n if n is not None else sum(counts.values()))
    if rows <= 0:
        raise ValueError("cost per 1000 needs at least one row")
    return total_cost(counts, matrix) / rows * COST_REPORT_SCALE


def metrics(counts: Mapping[str, int]) -> dict[str, float]:
    """Standard ratios from the same counts.

    A ratio with an empty denominator is reported as ``0.0`` and flagged in
    ``undefined``, because a blank cell in a graded table invites a reader to
    compare numbers that were never measurable.
    """
    tp, fp, tn, fn = counts["tp"], counts["fp"], counts["tn"], counts["fn"]
    n = tp + fp + tn + fn
    undefined: list[str] = []

    def ratio(numerator: int, denominator: int, name: str) -> float:
        if denominator == 0:
            undefined.append(name)
            return 0.0
        return numerator / denominator

    precision = ratio(tp, tp + fp, "precision")
    recall = ratio(tp, tp + fn, "recall")
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {
        "n": float(n),
        "positive_rate": ratio(tp + fn, n, "positive_rate") if n else 0.0,
        # the number a naive report would lead with, and its ceiling
        "accuracy": ratio(tp + tn, n, "accuracy") if n else 0.0,
        "majority_class_accuracy": ratio(max(tp + fn, tn + fp), n, "majority") if n else 0.0,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "specificity": ratio(tn, tn + fp, "specificity"),
        "flag_rate": ratio(tp + fp, n, "flag_rate") if n else 0.0,
        "undefined": undefined,  # type: ignore[dict-item]
    }


def auc_rank(y_true: Sequence[object] | np.ndarray, scores: Sequence[object] | np.ndarray) -> float:
    """AUC as the Mann-Whitney U statistic, ties at one half.

    Implemented here rather than imported so the value the report quotes has a
    definition a test can check by hand; ``tests/test_credit_cost.py`` also
    cross-checks it against ``sklearn.metrics.roc_auc_score`` when scikit-learn
    is installed.
    """
    truth = _as_binary(y_true, "y_true")
    ranked = np.asarray(scores, dtype="float64")
    if truth.shape != ranked.shape:
        raise ValueError(f"length mismatch: {truth.shape} vs {ranked.shape}")
    positives = ranked[truth == 1]
    negatives = ranked[truth == 0]
    if positives.size == 0 or negatives.size == 0:
        raise ValueError("AUC needs both classes present")
    order = np.argsort(ranked, kind="mergesort")
    ranks = np.empty(ranked.size, dtype="float64")
    ranks[order] = np.arange(1, ranked.size + 1, dtype="float64")
    # average the ranks inside each tie block, the textbook correction
    sorted_values = ranked[order]
    start = 0
    for index in range(1, sorted_values.size + 1):
        if index == sorted_values.size or sorted_values[index] != sorted_values[start]:
            if index - start > 1:
                ranks[order[start:index]] = ranks[order[start:index]].mean()
            start = index
    rank_sum = float(ranks[truth == 1].sum())
    n_pos, n_neg = positives.size, negatives.size
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def cost_curve(
    y_true: Sequence[object] | np.ndarray,
    proba: Sequence[object] | np.ndarray,
    *,
    matrix: CostMatrix = CHALLENGE_MATRIX,
    thresholds: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    """Cost at every threshold. One comparison per threshold, never a 3-D array."""
    truth = _as_binary(y_true, "y_true")
    scores = np.asarray(proba, dtype="float64")
    if truth.shape != scores.shape:
        raise ValueError(f"length mismatch: {truth.shape} vs {scores.shape}")
    grid = THRESHOLD_GRID if thresholds is None else np.asarray(thresholds, dtype="float64")
    if grid.ndim != 1 or grid.size == 0:
        raise ValueError("thresholds must be a non-empty 1-D array")
    n = truth.size
    costs = np.empty(grid.size, dtype="float64")
    fp = np.empty(grid.size, dtype="int64")
    fn = np.empty(grid.size, dtype="int64")
    tp = np.empty(grid.size, dtype="int64")
    tn = np.empty(grid.size, dtype="int64")
    positive = truth == 1
    for position, cutoff in enumerate(grid):
        flagged = scores >= cutoff
        tp[position] = int(np.count_nonzero(positive & flagged))
        fn[position] = int(np.count_nonzero(positive & ~flagged))
        fp[position] = int(np.count_nonzero(~positive & flagged))
        tn[position] = int(np.count_nonzero(~positive & ~flagged))
        costs[position] = (
            (
                fp[position] * matrix.false_positive
                + fn[position] * matrix.false_negative
                + tp[position] * matrix.true_positive
                + tn[position] * matrix.true_negative
            )
            / n
            * COST_REPORT_SCALE
        )
    return {
        "threshold": grid,
        "cost_per_1000": costs,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
    }


@dataclass(frozen=True)
class ThresholdChoice:
    """The selected operating point, with everything needed to defend it."""

    threshold: float
    counts: dict[str, int]
    cost_per_1000: float
    metrics: dict[str, float]
    grid_size: int
    ties: int
    matrix: str

    def as_dict(self) -> dict[str, object]:
        return {
            "threshold": self.threshold,
            "cost_per_1000": self.cost_per_1000,
            "matrix": self.matrix,
            "grid_size": self.grid_size,
            "ties": self.ties,
            "counts": dict(self.counts),
            "metrics": {k: v for k, v in self.metrics.items() if k != "undefined"},
            "undefined_metrics": list(self.metrics.get("undefined", [])),
        }


def select_threshold(
    y_true: Sequence[object] | np.ndarray,
    proba: Sequence[object] | np.ndarray,
    *,
    matrix: CostMatrix = CHALLENGE_MATRIX,
    thresholds: np.ndarray | None = None,
) -> ThresholdChoice:
    """The minimum-cost point on the grid.

    Ties are broken toward the lower threshold: at equal expected cost, flagging
    more applicants misses fewer defaults, which is the direction the cost matrix
    itself says is worth money.
    """
    curve = cost_curve(y_true, proba, matrix=matrix, thresholds=thresholds)
    best = float(curve["cost_per_1000"].min())
    winners = np.flatnonzero(curve["cost_per_1000"] == best)
    position = int(winners[0])
    counts = {
        "tp": int(curve["tp"][position]),
        "fp": int(curve["fp"][position]),
        "tn": int(curve["tn"][position]),
        "fn": int(curve["fn"][position]),
    }
    return ThresholdChoice(
        threshold=float(curve["threshold"][position]),
        counts=counts,
        cost_per_1000=best,
        metrics=metrics(counts),
        grid_size=int(curve["threshold"].size),
        ties=int(winners.size),
        matrix=matrix.name,
    )


def rule_baseline(
    y_true: Sequence[object] | np.ndarray,
    flags: Sequence[object] | np.ndarray,
    *,
    matrix: CostMatrix = CHALLENGE_MATRIX,
    name: str = "rule",
) -> dict[str, object]:
    """Cost of a decision rule with no learning in it - the floor to beat."""
    truth = _as_binary(y_true, "y_true")
    counts = binary_counts(truth, np.asarray(flags))
    return {
        "name": name,
        "counts": counts,
        "cost_per_1000": cost_per_1000(counts, matrix),
        "metrics": metrics(counts),
    }


def constant_baselines(
    y_true: Sequence[object] | np.ndarray, *, matrix: CostMatrix = CHALLENGE_MATRIX
) -> dict[str, dict[str, object]]:
    """Approve everyone and reject everyone: the two rules that need no data."""
    truth = _as_binary(y_true, "y_true")
    nobody = rule_baseline(
        truth, np.zeros(truth.size, dtype="int64"), matrix=matrix, name="flag-nobody"
    )
    everybody = rule_baseline(
        truth, np.ones(truth.size, dtype="int64"), matrix=matrix, name="flag-everybody"
    )
    return {"flag_nobody": nobody, "flag_everybody": everybody}


def reduction_vs_baseline(baseline_cost_per_1000: float, model_cost_per_1000: float) -> float:
    """Percent reduction; the acceptance bar in the challenge is 25%."""
    if baseline_cost_per_1000 == 0:
        raise ZeroDivisionError("baseline cost is zero, so a reduction is undefined")
    return (baseline_cost_per_1000 - model_cost_per_1000) / baseline_cost_per_1000 * 100.0


def matrix_sensitivity(
    y_true: Sequence[object] | np.ndarray,
    proba: Sequence[object] | np.ndarray,
    matrices: Sequence[CostMatrix],
    *,
    thresholds: np.ndarray | None = None,
) -> list[dict[str, object]]:
    """How the chosen threshold and the cost move when the matrix changes.

    The cost at the library default 0.5 is reported next to the optimum for each
    matrix, because the gap between those two numbers is the argument for
    choosing a threshold at all.
    """
    grid = THRESHOLD_GRID if thresholds is None else np.asarray(thresholds, dtype="float64")
    rows: list[dict[str, object]] = []
    for matrix in matrices:
        choice = select_threshold(y_true, proba, matrix=matrix, thresholds=grid)
        counts_at_default = binary_counts(
            y_true, (np.asarray(proba, dtype="float64") >= 0.5).astype("int64")
        )
        rows.append(
            {
                "matrix": matrix.name,
                "ratio": matrix.ratio,
                "threshold": choice.threshold,
                "cost_per_1000": choice.cost_per_1000,
                "cost_per_1000_at_0.5": cost_per_1000(counts_at_default, matrix),
                "recall": choice.metrics["recall"],
                "flag_rate": choice.metrics["flag_rate"],
                "ties": choice.ties,
            }
        )
    return rows
