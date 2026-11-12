"""The split, and the boundary that makes leakage impossible by construction.

Three sets, decided before any statistic is computed:

``train``       everything that is allowed to *fit* anything - imputation values,
                scaling, model weights, the threshold search grid
``validation``  only ever scored, never fitted on; the cost curve that picks the
                threshold is read here
``holdout``     the file the model is expected to meet once, at the end

The split is stratified on the target and driven by one seeded generator, so two
runs of the notebook get the same rows. Doing the permutation with numpy rather than
``train_test_split`` is a deliberate choice: it is 15 lines, it is the same on every
sklearn version, and the property the report cares about (equal positive rate in
each part, disjoint ids) is easy to assert on it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .columns import ID_COLUMN as _CSV_ID
from .columns import VIEW_TARGET_COLUMN, db_for

ROW_ID = db_for(_CSV_ID)

FRACTIONS = (0.6, 0.2, 0.2)


@dataclass(frozen=True)
class Splits:
    train: pd.DataFrame
    validation: pd.DataFrame
    holdout: pd.DataFrame

    @property
    def sizes(self) -> dict[str, int]:
        return {
            "train": len(self.train),
            "validation": len(self.validation),
            "holdout": len(self.holdout),
        }


def _stratified_partition(
    labels: np.ndarray, rng: np.random.Generator, fractions=tuple[float, ...]
) -> list[np.ndarray]:
    """Indices of each part, each class cut in the same proportions.

    Within a class the order is a seeded permutation, then sliced: the first slice
    is train, the next validation, the last holdout. No row can be in two parts
    because every index is dealt exactly once.
    """
    cuts = np.cumsum(fractions) / float(sum(fractions))
    parts: list[np.ndarray] = [np.array([], dtype=np.int64) for _ in fractions]
    for label in np.unique(labels):
        idx = np.flatnonzero(labels == label)
        idx = rng.permutation(idx)
        start = 0
        for position, cut in enumerate(cuts):
            stop = len(idx) if position == len(cuts) - 1 else int(round(len(idx) * cut))  # noqa: RUF046 - np.float64 round() is an int, and the cast documents that
            parts[position] = np.concatenate([parts[position], idx[start:stop]])
            start = stop
    return [np.sort(part) for part in parts]


def stratified_split(
    frame: pd.DataFrame,
    *,
    seed: int,
    fractions: tuple[float, ...] = FRACTIONS,
    target: str = VIEW_TARGET_COLUMN,
) -> Splits:
    """Deterministic train/validation/holdout split with a fixed seed."""
    if len(fractions) != 3:
        raise ValueError("expected exactly three fractions: train, validation, holdout")
    if any(f <= 0 for f in fractions):
        raise ValueError("fractions must all be positive")
    if frame[target].isna().any():
        raise ValueError("the target must be present in every row before splitting")
    rng = np.random.default_rng(seed)
    labels = frame[target].to_numpy(dtype=int)
    train_idx, val_idx, holdout_idx = _stratified_partition(labels, rng, fractions)
    return Splits(
        train=frame.iloc[train_idx].reset_index(drop=True),
        validation=frame.iloc[val_idx].reset_index(drop=True),
        holdout=frame.iloc[holdout_idx].reset_index(drop=True),
    )


def stratified_folds(
    frame: pd.DataFrame,
    *,
    seed: int,
    k: int = 5,
    target: str = VIEW_TARGET_COLUMN,
) -> list[tuple[pd.Index, pd.Index]]:
    """``k`` stratified folds over the rows given, as (train_index, test_index) pairs.

    Fold assignment is one seeded permutation per class modulo ``k``, so every row
    is a test row exactly once and the folds are stable across the two model
    families being compared. The holdout must never be passed in here: the frames
    that reach cross-validation are train+validation.
    """
    if k < 2:
        raise ValueError("k must be >= 2")
    rng = np.random.default_rng(seed)
    labels = frame[target].to_numpy(dtype=int)
    fold_of = np.empty(len(frame), dtype=int)
    for label in np.unique(labels):
        idx = rng.permutation(np.flatnonzero(labels == label))
        fold_of[idx] = np.arange(len(idx)) % k
    index = frame.index
    return [(index[fold_of != f], index[fold_of == f]) for f in range(k)]


def boundary_audit(splits: Splits, *, id_column: str = ROW_ID) -> pd.DataFrame:
    """The numbers that show the boundary is real, for the notebook to print.

    Overlap must be zero and the positive rate must be close in all three parts:
    a split that hides a rate difference is a leak in disguise.
    """
    rows: list[dict[str, object]] = []
    seen: dict[str, set] = {}
    for name in ("train", "validation", "holdout"):
        frame = getattr(splits, name)
        ids = set(frame[id_column].astype("int64").tolist()) if len(frame) else set()
        overlap = len(ids & seen.get("train", set())) if name != "train" else 0
        target = frame[VIEW_TARGET_COLUMN]
        rows.append(
            {
                "split": name,
                "rows": len(frame),
                "positives": int(target.sum()) if len(frame) else 0,
                "positive_rate": round(float(target.mean()), 6) if len(frame) else None,
                "overlap_with_train": overlap,
            }
        )
        if name == "train":
            seen["train"] = ids
    return pd.DataFrame(rows)


def overlap_with_train(
    frame: pd.DataFrame, train: pd.DataFrame, *, id_column: str = ROW_ID
) -> int:
    """Count ids shared with the training split - the leakage check for any new frame."""
    return len(
        set(frame[id_column].astype("int64")) & set(train[id_column].astype("int64"))
    )
