"""Preparation whose every number comes from the training split, by construction.

The split between this module and the SQL view is the one the challenge asks to be
justified in writing:

* the view does **row-wise, unfitted** work - sentinel codes to NULL, capping
  utilization at 1.0, the income-not-reported flag, the sum of delinquency columns.
  Each is a decision about what a value *means*, needs no statistics, and must
  therefore apply identically to any file with the same schema;
* this module does **fitted** work - medians for imputation, means and spreads for
  scaling. A fitted number cannot live in a view, because a view has no idea which
  rows are being held out: bake the median into the view and the validation rows
  contribute to their own imputation value.

So :func:`fit` is only ever called on a training frame, and :func:`apply` is the
only way anything else is prepared. :func:`audit` prints the resulting constants with
the row count that produced them, which is what makes the boundary inspectable rather
than asserted.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .columns import (
    COLUMNS,
    ID_COLUMN,
    MODEL_FEATURE_COLUMNS,
    VIEW_TARGET_COLUMN,
    db_for,
)
from .features import numeric

#: every non-key, non-target column can be NULL after the view masks sentinels
NULLABLE_COLUMNS: tuple[str, ...] = tuple(
    spec.db_name for spec in COLUMNS if spec.nullable and spec.csv_name != ID_COLUMN
)
INDICATOR_SUFFIX = "_was_missing"


@dataclass(frozen=True)
class PrepPlan:
    """What to do, as opposed to what the numbers are.

    ``impute`` names the statistic that fills a NULL; ``zero`` is the policy that
    treats an unreported count as an absence of events rather than an average
    applicant. ``indicator`` adds the flag column that lets a model tell the two
    apart - the reason it is usually worth its columns is that missingness here is
    informative, which :mod:`credit.missingness` measures.
    """

    name: str
    impute: str = "median"
    indicator: bool = True
    log_columns: tuple[str, ...] = ()
    scale: bool = True
    justification: str = ""


LOG_COUNTS: tuple[str, ...] = tuple(
    db_for(name)
    for name in (
        "NumberOfTime30-59DaysPastDueNotWorse",
        "NumberOfTimes90DaysLate",
        "NumberOfOpenCreditLinesAndLoans",
    )
)

PLANS: dict[str, PrepPlan] = {
    "median+indicator": PrepPlan(
        name="median+indicator",
        impute="median",
        indicator=True,
        log_columns=LOG_COUNTS,
        scale=True,
        justification=(
            "median for the skewed counts, a flag for every imputed column, log1p on the "
            "heavy tails, z-scaling because the linear model's penalty is in coefficient units"
        ),
    ),
    "zero+none": PrepPlan(
        name="zero+none",
        impute="zero",
        indicator=False,
        log_columns=(),
        scale=False,
        justification=(
            "an unreported late count read as no recorded lateness, no flags, no scaling - "
            "the smallest amount of bookkeeping that still produces a usable matrix"
        ),
    ),
    "median+no-indicator": PrepPlan(
        name="median+no-indicator",
        impute="median",
        indicator=False,
        log_columns=LOG_COUNTS,
        scale=True,
        justification="the same imputation without the flags, to price the flags themselves",
    ),
}


def _median_or_zero(values: np.ndarray, impute: str) -> float:
    finite = values[np.isfinite(values)]
    if impute == "zero":
        return 0.0
    if finite.size == 0:
        # a column with nothing to measure has to fall back somewhere; zero is the
        # same choice the plan makes for unreported counts
        return 0.0
    return float(np.median(finite))


@dataclass(frozen=True)
class PrepStats:
    """The fitted constants and nothing else - what a fresh file needs to be usable."""

    plan: str
    medians: dict[str, float] = field(default_factory=dict)
    means: dict[str, float] = field(default_factory=dict)
    stds: dict[str, float] = field(default_factory=dict)
    indicator_columns: tuple[str, ...] = ()
    log_columns: tuple[str, ...] = ()
    fitted_rows: int = 0
    fitted_on: str = "train"

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["indicator_columns"] = list(self.indicator_columns)
        payload["log_columns"] = list(self.log_columns)
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> PrepStats:
        return cls(
            plan=str(payload["plan"]),
            medians={k: float(v) for k, v in payload["medians"].items()},
            means={k: float(v) for k, v in payload["means"].items()},
            stds={k: float(v) for k, v in payload["stds"].items()},
            indicator_columns=tuple(payload["indicator_columns"]),
            log_columns=tuple(payload["log_columns"]),
            fitted_rows=int(payload["fitted_rows"]),
            fitted_on=str(payload.get("fitted_on", "train")),
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # key order is data here - it is the fitted column order - so the json is
        # written without sort_keys and read back the same way
        path.write_text(json.dumps(self.as_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> PrepStats:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def feature_names(self, base: tuple[str, ...] | None = None) -> tuple[str, ...]:
        """The columns the model matrix must contain, in fitted order.

        The default is the list ``fit()`` was given - ``medians`` is keyed by exactly
        those columns - because a caller that fits on base plus engineered features
        has to get all of them back. Rebuilding the list from the raw file columns
        instead was how a notebook trained a model on ten columns while printing six.
        """
        names = list(self.medians if base is None else base)
        names += [column + INDICATOR_SUFFIX for column in self.indicator_columns]
        return tuple(dict.fromkeys(names))


def fit(
    train: pd.DataFrame,
    *,
    plan: PrepPlan,
    columns: tuple[str, ...] = MODEL_FEATURE_COLUMNS,
) -> PrepStats:
    """Compute every constant from ``train`` alone. This is the whole boundary.

    ``columns`` is the full model matrix, engineered features included: pass the
    list the model will actually see, or the linear model gets some columns scaled
    and others left in their original units.
    """
    missing = [column for column in columns if column not in train.columns]
    if missing:
        raise KeyError(f"training frame is missing columns: {missing}")
    medians = {
        column: _median_or_zero(
            numeric(train, column).to_numpy(dtype="float64"), plan.impute
        )
        for column in columns
    }
    imputed = {column: train[column].fillna(medians[column]) for column in columns}
    # a plan names the columns it wants on a log scale, but a caller may fit a subset
    # (an ablation, a smaller matrix); taking the intersection here is what keeps the
    # transform and the returned stats describing the same columns
    logs = tuple(column for column in plan.log_columns if column in columns)
    for column in logs:
        imputed[column] = np.log1p(imputed[column].clip(lower=0.0))
    means: dict[str, float] = {}
    stds: dict[str, float] = {}
    if plan.scale:
        for column, values in imputed.items():
            array = np.asarray(values, dtype="float64")
            means[column] = float(array.mean()) if array.size else 0.0
            spread = float(array.std(ddof=1)) if array.size > 1 else 0.0
            # a constant column would divide by zero and turn the model into NaN;
            # std 1 means "do nothing to this column", which is the honest answer
            stds[column] = spread if spread > 0 else 1.0
    indicators = (
        tuple(column for column in columns if train[column].isna().any())
        if plan.indicator
        else ()
    )
    return PrepStats(
        plan=plan.name,
        medians=medians,
        means=means,
        stds=stds,
        indicator_columns=indicators,
        log_columns=logs,
        fitted_rows=len(train),
    )


def apply(frame: pd.DataFrame, stats: PrepStats) -> pd.DataFrame:
    """Prepare any frame with constants that came from somewhere else.

    The function physically cannot see another split's rows, which is the property
    the report leans on: the same call prepares the validation rows, the holdout,
    and a fresh file a month from now.
    """
    out = frame.copy()
    for column, value in stats.medians.items():
        if column not in out.columns:
            raise KeyError(f"prepared frame is missing column {column!r}")
        if stats.indicator_columns and column in stats.indicator_columns:
            out[column + INDICATOR_SUFFIX] = out[column].isna().astype("int64")
        out[column] = out[column].astype("float64").fillna(value)
    for column in stats.log_columns:
        out[column] = np.log1p(out[column].clip(lower=0.0))
    for column, mean in stats.means.items():
        out[column] = (out[column] - mean) / stats.stds[column]
    return out


def target(frame: pd.DataFrame) -> pd.Series:
    return frame[VIEW_TARGET_COLUMN].astype("int64")


def audit(stats: PrepStats) -> pd.DataFrame:
    """One row per fitted constant, with what it cost to know it.

    ``fitted_rows`` is the size of the split that produced the number; a statistic
    fitted on 15,000 rows is a weaker claim than one fitted on 90,000, and the table
    makes that visible without a paragraph.
    """
    rows: list[dict[str, object]] = []
    for column, value in stats.medians.items():
        rows.append(
            {
                "column": column,
                "statistic": f"{stats.plan} imputation value",
                "value": round(value, 6),
                "fitted_on": stats.fitted_on,
                "fitted_rows": stats.fitted_rows,
            }
        )
    for column, mean in stats.means.items():
        rows.append(
            {
                "column": column,
                "statistic": "scaling mean",
                "value": round(mean, 6),
                "fitted_on": stats.fitted_on,
                "fitted_rows": stats.fitted_rows,
            }
        )
    for column, std in stats.stds.items():
        rows.append(
            {
                "column": column,
                "statistic": "scaling std",
                "value": round(std, 6),
                "fitted_on": stats.fitted_on,
                "fitted_rows": stats.fitted_rows,
            }
        )
    for column in stats.indicator_columns:
        rows.append(
            {
                "column": column + INDICATOR_SUFFIX,
                "statistic": "missingness indicator (unfitted)",
                "value": None,
                "fitted_on": "row-wise",
                "fitted_rows": None,
            }
        )
    for column in stats.log_columns:
        rows.append(
            {
                "column": column,
                "statistic": "log1p transform (unfitted)",
                "value": None,
                "fitted_on": "row-wise",
                "fitted_rows": None,
            }
        )
    return pd.DataFrame(rows)


def remaining_missing(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.DataFrame:
    """Proof that the preparation left no NaN behind, per column."""
    return pd.DataFrame(
        {
            "column": list(columns),
            "nan_after_prep": [int(frame[column].isna().sum()) for column in columns],
        }
    )
