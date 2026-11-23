"""Two model families, chosen by expected cost, and the bundle the notebook saves.

Deliberately thin: fitting is sklearn's job. What lives here is the part the
challenge actually grades - the acceptance rule is a cost reduction against a
no-learning baseline, the threshold comes from the cost curve, and the comparison has
to be per-fold so the spread is visible. Those three things are easy to get wrong in a
notebook cell, so they are functions with tests instead of cells.

Scaling is a per-family decision, not a global one, and :data:`FAMILIES` records the
reason next to the flag: a logistic model with an ``L2`` penalty shrinks coefficients,
and shrinking is meaningless when the columns differ by three orders of magnitude;
tree splits compare one column against a threshold, so any monotone rescaling leaves
the split and the prediction untouched.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression

from .cost import (
    CHALLENGE_MATRIX,
    CostMatrix,
    ThresholdChoice,
    auc_rank,
    binary_counts,
    cost_per_1000,
    metrics,
    select_threshold,
)
from .prep import PrepPlan, PrepStats
from .prep import apply as prep_apply
from .prep import fit as prep_fit
from .prep import for_family as prep_for_family

CV_FOLDS = 5


@dataclass(frozen=True)
class Family:
    """One estimator plus the preparation it needs and the reason for that."""

    name: str
    build: Callable[..., object]
    requires_scaling: bool
    scaling_note: str
    family_note: str
    #: whether the estimator can be fitted on NaN (the ``native`` prep plan)
    handles_missing: bool = False

    def make(self, *, seed: int) -> object:
        return self.build(random_state=seed)


def _logistic(random_state: int) -> LogisticRegression:
    # penalty is left at its l2 default on purpose: naming it warns on sklearn >= 1.8,
    # where the same intent is expressed through C and l1_ratio
    return LogisticRegression(
        C=1.0, solver="lbfgs", max_iter=2_000, random_state=random_state
    )


def _boosted(random_state: int) -> HistGradientBoostingClassifier:
    # learning_rate and early_stopping kept explicit: the defaults are tuned for large
    # open datasets, and here the training split is whatever 60% of 150k happens to be
    return HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=300,
        max_depth=3,
        early_stopping=True,
        validation_fraction=0.15,
        random_state=random_state,
    )


FAMILIES: dict[str, Family] = {
    "logistic": Family(
        name="logistic",
        build=_logistic,
        requires_scaling=True,
        scaling_note=(
            "the L2 penalty is applied to coefficients, so it is only meaningful when the "
            "columns share a scale; income in thousands would otherwise own the model"
        ),
        family_note="one linear boundary, readable coefficients, the cheapest thing worth trying",
    ),
    "boosted_trees": Family(
        name="boosted_trees",
        build=_boosted,
        requires_scaling=False,
        scaling_note=(
            "each split compares one column against a threshold, so a monotone rescaling "
            "changes neither the split nor the prediction; NaNs are also handled natively"
        ),
        family_note="non-linear and interaction-carrying, which is what a credit file implies",
        handles_missing=True,
    ),
}

#: share of a fold's training rows held back to choose that fold's threshold
INNER_FRACTION = 0.25


def plan_for(family: str, plan: PrepPlan) -> PrepPlan:
    """The missing-data plan with the scaling this family needs (``requires_scaling``)."""
    spec = FAMILIES[family]
    return prep_for_family(
        plan, scaled=spec.requires_scaling, handles_missing=spec.handles_missing
    )


def compatible(family: str, plan: PrepPlan) -> bool:
    return plan.impute != "none" or FAMILIES[family].handles_missing


@dataclass(frozen=True)
class FoldResult:
    """One fold's held-out score card, at one operating point."""

    fold: int
    rows: int
    positives: int
    threshold: float
    cost_per_1000: float
    auc: float
    counts: dict[str, int]


@dataclass(frozen=True)
class CVResult:
    """The spread across folds, which is the honest part of the number."""

    family: str
    plan: str
    matrix: str
    folds: list[FoldResult] = field(default_factory=list)
    mean_cost: float = 0.0
    std_cost: float = 0.0
    worst_cost: float = 0.0
    mean_auc: float = 0.0
    std_auc: float = 0.0
    threshold_mean: float = 0.0
    threshold_std: float = 0.0

    @property
    def cost_range(self) -> tuple[float, float]:
        if not self.folds:
            return (0.0, 0.0)
        costs = [fold.cost_per_1000 for fold in self.folds]
        return (min(costs), max(costs))

    def as_dict(self) -> dict[str, object]:
        return {
            "family": self.family,
            "plan": self.plan,
            "matrix": self.matrix,
            "folds": len(self.folds),
            "mean_cost_per_1000": round(self.mean_cost, 3),
            "std_cost_per_1000": round(self.std_cost, 3),
            "worst_cost_per_1000": round(self.worst_cost, 3),
            "cost_range": [round(v, 3) for v in self.cost_range],
            "mean_auc": round(self.mean_auc, 4),
            "std_auc": round(self.std_auc, 4),
            "threshold_mean": round(self.threshold_mean, 4),
            "threshold_std": round(self.threshold_std, 4),
            "per_fold": [
                {
                    "fold": fold.fold,
                    "rows": fold.rows,
                    "positives": fold.positives,
                    "threshold": round(fold.threshold, 4),
                    "cost_per_1000": round(fold.cost_per_1000, 3),
                    "auc": round(fold.auc, 4),
                    "counts": dict(fold.counts),
                }
                for fold in self.folds
            ],
        }


def cross_validate_cost(
    family: str,
    X: pd.DataFrame,
    y: pd.Series,
    folds: list[tuple[pd.Index, pd.Index]],
    *,
    plan: str,
    seed: int,
    matrix: CostMatrix = CHALLENGE_MATRIX,
    fixed_threshold: float | None = None,
    inner_fraction: float = INNER_FRACTION,
) -> CVResult:
    """Cross-validated cost of a matrix that is already prepared.

    Use :func:`cross_validate_recipe` when the preparation has fitted constants:
    this function cannot refit them inside each fold. See ``_run_folds`` for how the
    threshold is chosen without looking at the fold being scored.
    """
    matrix_X = X

    def prepare(_fit_index: pd.Index) -> Callable[[pd.Index], pd.DataFrame]:
        return lambda index: matrix_X.loc[index]

    return _run_folds(
        family,
        X.index,
        y,
        folds,
        prepare,
        plan=plan,
        seed=seed,
        matrix=matrix,
        fixed_threshold=fixed_threshold,
        inner_fraction=inner_fraction,
    )


def cross_validate_recipe(
    family: str,
    frame: pd.DataFrame,
    y: pd.Series,
    folds: list[tuple[pd.Index, pd.Index]],
    *,
    plan: PrepPlan,
    columns: tuple[str, ...],
    seed: int,
    matrix: CostMatrix = CHALLENGE_MATRIX,
    fixed_threshold: float | None = None,
    inner_fraction: float = INNER_FRACTION,
) -> CVResult:
    """Cross-validated cost of the whole recipe: prep, model, threshold.

    Inside every fold the preparation is fitted on that fold's own model-fitting
    rows - never on the rows it is scored on - and with the scaling the family needs
    (:func:`plan_for`). That is the same boundary the final model keeps against the
    validation and holdout splits, applied once per fold.
    """
    family_plan = plan_for(family, plan)

    def prepare(fit_index: pd.Index) -> Callable[[pd.Index], pd.DataFrame]:
        stats = prep_fit(frame.loc[fit_index], plan=family_plan, columns=columns)
        names = list(stats.feature_names())
        return lambda index: prep_apply(frame.loc[index], stats)[names]

    return _run_folds(
        family,
        frame.index,
        y,
        folds,
        prepare,
        plan=plan.name,
        seed=seed,
        matrix=matrix,
        fixed_threshold=fixed_threshold,
        inner_fraction=inner_fraction,
    )


def inner_split(
    index: pd.Index, labels: np.ndarray, *, seed: int, fraction: float = INNER_FRACTION
) -> tuple[pd.Index, pd.Index]:
    """Stratified (fit, threshold) split of one fold's training rows."""
    if not 0.0 < fraction < 1.0:
        raise ValueError("inner fraction must be between 0 and 1")
    rng = np.random.default_rng(seed)
    held: list[int] = []
    for label in (0, 1):
        positions = np.flatnonzero(labels == label)
        rng.shuffle(positions)
        held.extend(positions[: int(round(len(positions) * fraction))].tolist())
    mask = np.zeros(len(index), dtype=bool)
    mask[held] = True
    return index[~mask], index[mask]


def _run_folds(
    family: str,
    index: pd.Index,
    y: pd.Series,
    folds: list[tuple[pd.Index, pd.Index]],
    prepare: Callable[[pd.Index], Callable[[pd.Index], pd.DataFrame]],
    *,
    plan: str,
    seed: int,
    matrix: CostMatrix,
    fixed_threshold: float | None,
    inner_fraction: float,
) -> CVResult:
    """Fit on each fold's training rows, score its held-out rows, repeat.

    With ``fixed_threshold`` the caller supplies the operating point and the model is
    fitted on the whole training part of the fold. Without it, the threshold must be
    *chosen*, and it is chosen the way the final model's is: the fold's training rows
    are split (stratified, ``inner_fraction`` held back), the model is fitted on the
    larger part, the threshold is the cost-curve minimum on the held-back part, and
    that same model is then scored on the fold's test rows at that threshold. The test
    rows never influence their own operating point, so a fold's cost is an honest
    out-of-sample number rather than the best cost achievable on rows already seen.

    Every estimator gets ``random_state=seed``: the notebook's SEED is the only source
    of randomness, and the inner splits derive from it and the fold number.
    """
    spec = FAMILIES[family]
    y_al = pd.Series(y).reindex(index).to_numpy(dtype="int64")
    results: list[FoldResult] = []
    for number, (train_index, test_index) in enumerate(folds, start=1):
        train_labels = y_al[_positions(index, train_index)]
        if fixed_threshold is None:
            fit_index, tune_index = inner_split(
                train_index,
                train_labels,
                seed=seed + number,
                fraction=inner_fraction,
            )
        else:
            fit_index, tune_index = train_index, None
        transform = prepare(fit_index)
        model = spec.build(random_state=seed)
        model.fit(transform(fit_index), y_al[_positions(index, fit_index)])
        if tune_index is None:
            threshold = float(fixed_threshold)
        else:
            tune_proba = np.asarray(
                model.predict_proba(transform(tune_index))[:, 1], dtype="float64"
            )
            tuned: ThresholdChoice = select_threshold(
                y_al[_positions(index, tune_index)], tune_proba, matrix=matrix
            )
            threshold = tuned.threshold
        proba = np.asarray(
            model.predict_proba(transform(test_index))[:, 1], dtype="float64"
        )
        truth = y_al[_positions(index, test_index)]
        choice = _choice_at(truth, proba, threshold=threshold, matrix=matrix)
        results.append(
            FoldResult(
                fold=number,
                rows=len(test_index),
                positives=int(truth.sum()),
                threshold=threshold,
                cost_per_1000=choice.cost_per_1000,
                auc=auc_rank(truth, proba),
                counts=dict(choice.counts),
            )
        )
    costs = np.array([fold.cost_per_1000 for fold in results], dtype="float64")
    aucs = np.array([fold.auc for fold in results], dtype="float64")
    thresholds = np.array([fold.threshold for fold in results], dtype="float64")
    return CVResult(
        family=spec.name,
        plan=plan,
        matrix=matrix.name,
        folds=results,
        mean_cost=float(costs.mean()),
        std_cost=float(costs.std(ddof=1)) if costs.size > 1 else 0.0,
        worst_cost=float(costs.max()),
        mean_auc=float(aucs.mean()),
        std_auc=float(aucs.std(ddof=1)) if aucs.size > 1 else 0.0,
        threshold_mean=float(thresholds.mean()),
        threshold_std=float(thresholds.std(ddof=1)) if thresholds.size > 1 else 0.0,
    )


def _positions(index: pd.Index, subset: pd.Index) -> np.ndarray:
    """Row numbers of ``subset`` inside ``index``, for the aligned label array."""
    lookup = {value: position for position, value in enumerate(index)}
    return np.array([lookup[value] for value in subset], dtype="int64")


def _choice_at(
    truth: np.ndarray, proba: np.ndarray, *, threshold: float, matrix: CostMatrix
) -> ThresholdChoice:
    """The same report shape as ``select_threshold``, at a threshold the caller chose."""
    counts = binary_counts(truth, (proba >= threshold).astype("int64"))
    return ThresholdChoice(
        threshold=float(threshold),
        counts=dict(counts),
        cost_per_1000=cost_per_1000(counts, matrix, n=len(truth) if len(truth) else 1),
        metrics=metrics(counts),
        grid_size=1,
        ties=1,
        matrix=matrix.name,
    )


def compare(results: list[CVResult]) -> pd.DataFrame:
    """One row per candidate, best mean cost first."""
    rows = [
        {
            "family": result.family,
            "plan": result.plan,
            "matrix": result.matrix,
            "mean_cost": round(result.mean_cost, 3),
            "std_cost": round(result.std_cost, 3),
            "worst_cost": round(result.worst_cost, 3),
            "mean_auc": round(result.mean_auc, 4),
            "threshold_std": round(result.threshold_std, 4),
        }
        for result in results
    ]
    return (
        pd.DataFrame(rows)
        .sort_values("mean_cost", kind="stable")
        .reset_index(drop=True)
    )


def pick_best(results: list[CVResult]) -> CVResult:
    """Lowest mean cost; a tie goes to the smaller spread, then to the simpler family."""
    best = min(
        results, key=lambda r: (round(r.mean_cost, 6), round(r.std_cost, 6), r.family)
    )
    return best


def cost_scorer(*, threshold: float, matrix: CostMatrix):
    """A scorer callable: negative expected cost per 1,000 at one operating point.

    Passed to ``permutation_importance`` as ``scoring=`` - it accepts a callable
    ``scorer(estimator, X, y)`` directly, which is steadier across sklearn versions
    than building one with ``make_scorer`` and its renamed probability keywords. The
    sign is the convention: sklearn maximises, and cost is minimised.
    """

    def score(estimator, X, y) -> float:
        proba = np.asarray(estimator.predict_proba(X)[:, 1], dtype="float64")
        counts = binary_counts(y, (proba >= threshold).astype("int64"))
        return -cost_per_1000(counts, matrix, n=len(y))

    return score


def importances(
    model: object,
    X: pd.DataFrame,
    y: pd.Series,
    *,
    threshold: float,
    matrix: CostMatrix = CHALLENGE_MATRIX,
    seed: int,
    n_repeats: int = 10,
) -> pd.DataFrame:
    """Permutation importance for any family, so the two are comparable.

    ``feature_importances_``/``coef_`` are not on the same scale across a linear model
    and a boosted ensemble, and the challenge asks for a ranking against business
    intuition - the only honest version of that is one measurement applied to both.
    The measurement is the drop in expected cost when a column's values are shuffled,
    which is the quantity the decision is actually about, not log-loss.
    """
    result = permutation_importance(
        model,
        X,
        y,
        scoring=cost_scorer(threshold=threshold, matrix=matrix),
        n_repeats=n_repeats,
        random_state=seed,
    )
    order = np.argsort(-result.importances_mean)
    return pd.DataFrame(
        {
            "feature": [X.columns[i] for i in order],
            "importance_mean": [
                round(float(result.importances_mean[i]), 6) for i in order
            ],
            "importance_std": [
                round(float(result.importances_std[i]), 6) for i in order
            ],
        }
    )


@dataclass(frozen=True)
class Bundle:
    """Everything needed to score a fresh file, in one file on disk."""

    model: object
    family: str
    feature_names: tuple[str, ...]
    prep_stats: PrepStats
    threshold: float
    matrix: str
    trained_rows: int
    code_version: str

    def score(self, X: pd.DataFrame) -> np.ndarray:
        missing = [name for name in self.feature_names if name not in X.columns]
        if missing:
            raise KeyError(f"scoring frame is missing features: {missing}")
        return np.asarray(
            self.model.predict_proba(X[list(self.feature_names)])[:, 1], dtype="float64"
        )

    def flag(self, X: pd.DataFrame) -> np.ndarray:
        return (self.score(X) >= self.threshold).astype("int64")

    def save(self, directory: Path) -> dict[str, Path]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        model_path = directory / "model.joblib"
        meta_path = directory / "model_card.json"
        joblib.dump(self.model, model_path, compress=3)
        self.prep_stats.save(directory / "prep_stats.json")
        meta_path.write_text(
            json.dumps(
                {
                    "family": self.family,
                    "feature_names": list(self.feature_names),
                    "threshold": self.threshold,
                    "matrix": self.matrix,
                    "trained_rows": self.trained_rows,
                    "code_version": self.code_version,
                    "prep_stats_file": "prep_stats.json",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            "model": model_path,
            "card": meta_path,
            "prep": directory / "prep_stats.json",
        }

    @classmethod
    def load(cls, directory: Path) -> Bundle:
        directory = Path(directory)
        meta = json.loads((directory / "model_card.json").read_text(encoding="utf-8"))
        stats = PrepStats.load(directory / meta["prep_stats_file"])
        model = joblib.load(directory / "model.joblib")
        return cls(
            model=model,
            family=meta["family"],
            feature_names=tuple(meta["feature_names"]),
            prep_stats=stats,
            threshold=float(meta["threshold"]),
            matrix=meta["matrix"],
            trained_rows=int(meta["trained_rows"]),
            code_version=meta["code_version"],
        )


def build_bundle(
    family: str,
    X_train: pd.DataFrame,
    y_train: pd.Series,
    *,
    prep_stats: PrepStats,
    threshold: float,
    matrix: CostMatrix = CHALLENGE_MATRIX,
    seed: int,
    code_version: str = "unknown",
) -> Bundle:
    """Fit the chosen family on the whole training split, once, for the bundle."""
    spec = FAMILIES[family]
    model = spec.build(random_state=seed)
    model.fit(X_train, pd.Series(y_train).to_numpy(dtype="int64"))
    return Bundle(
        model=model,
        family=spec.name,
        feature_names=tuple(X_train.columns),
        prep_stats=prep_stats,
        threshold=float(threshold),
        matrix=matrix.name,
        trained_rows=len(X_train),
        code_version=code_version,
    )


def wrap_bundle(
    model: object,
    family: str,
    X_train: pd.DataFrame,
    *,
    prep_stats: PrepStats,
    threshold: float,
    matrix: CostMatrix = CHALLENGE_MATRIX,
    code_version: str = "unknown",
) -> Bundle:
    """Bundle an estimator that is already fitted, without refitting it.

    The threshold was chosen on validation scores of *this* model; refitting on more
    rows would give a model whose scores the threshold was never chosen for. So the
    delivered bundle is the model the threshold belongs to, and the holdout scores it.
    """
    return Bundle(
        model=model,
        family=FAMILIES[family].name,
        feature_names=tuple(X_train.columns),
        prep_stats=prep_stats,
        threshold=float(threshold),
        matrix=matrix.name,
        trained_rows=len(X_train),
        code_version=code_version,
    )
