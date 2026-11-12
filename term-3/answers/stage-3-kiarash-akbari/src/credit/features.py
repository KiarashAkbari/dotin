"""Engineered features, each carrying the decision-time question it must survive.

The challenge requires a leakage audit per feature, so the audit is part of the
data structure rather than prose written afterwards: a spec that cannot claim its
information exists before the loan is granted is not added to ``FEATURES``.

Two conventions keep the Python path and the database view honest:

* ``mask_sentinels`` runs first, so a code never enters a sum. The view in
  ``sql/010_credit_schema.sql`` applies the same rule in SQL; the notebook checks
  the two agree instead of assuming it.
* Every fill is a constant declared in code (``dependents missing means none
  recorded, treat as zero``), never a statistic computed on the frame in front of
  us. Anything fitted - imputation values, scaling - lives in the prep layer and
  is fitted on the training split only.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pandas as pd

from .columns import LATE_PAYMENT_COLUMNS, SENTINEL_CODES

#: the label, under the name the view returns
TARGET = "serious_dlqin_2yrs"
SENTINEL_VALUES: tuple[int, ...] = tuple(sorted(SENTINEL_CODES))


def numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    """A column as floats, with anything unusable becoming NaN rather than a guess."""
    return pd.to_numeric(frame[column], errors="coerce")


def mask_sentinels(
    frame: pd.DataFrame,
    columns: Sequence[str] = LATE_PAYMENT_COLUMNS,
    codes: Sequence[int] = SENTINEL_VALUES,
) -> pd.DataFrame:
    """Replace documented codes with NaN in the given columns.

    The stored columns are never edited: the raw table keeps what the file said,
    and only the analysis frame loses the codes, so the spike stays countable.
    """
    out = frame.copy()
    for column in columns:
        values = numeric(out, column)
        out[column] = values.where(~values.isin(list(codes)))
    return out


def sentinel_spike_counts(
    frame: pd.DataFrame,
    columns: Sequence[str] = LATE_PAYMENT_COLUMNS,
    codes: Sequence[int] = SENTINEL_VALUES,
    *,
    target: str = TARGET,
) -> pd.DataFrame:
    """How many rows carry each code, and what their default rate is.

    This is the evidence for calling a code an artifact instead of a very bad
    month: a code whose rows behave like ordinary rows is a count, and a code
    whose rows behave unlike everything else is a data-entry marker.
    """
    rows: list[dict[str, object]] = []
    overall = float(numeric(frame, target).mean()) if target in frame else float("nan")
    total = len(frame)
    for column in columns:
        values = numeric(frame, column)
        label = numeric(frame, target) if target in frame else None
        for code in codes:
            hit = values == code
            count = int(hit.sum())
            rate = count / total if total else float("nan")
            rows.append(
                {
                    "column": column,
                    "code": int(code),
                    "rows": count,
                    "share_of_file": rate,
                    "default_rate_with_code": float(label[hit].mean())
                    if label is not None and count
                    else float("nan"),
                    "default_rate_without_code": float(label[~hit].mean())
                    if label is not None and (~hit).any()
                    else float("nan"),
                    "base_default_rate": overall,
                }
            )
    return pd.DataFrame(rows)


@dataclass(frozen=True)
class FeatureSpec:
    """One engineered column plus the reasoning that justifies it."""

    name: str
    hypothesis: str
    source_columns: tuple[str, ...]
    decision_time_available: bool
    availability_note: str
    transform: Callable[[pd.DataFrame], pd.Series] = field(repr=False)
    #: a feature whose inclusion must be argued from a measurement, not from taste
    requires_evidence: str | None = None
    #: what would have made this leakage, even if this version does not
    leakage_boundary: str = ""


def _delinquency_total(frame: pd.DataFrame) -> pd.Series:
    values = frame[list(LATE_PAYMENT_COLUMNS)].apply(pd.to_numeric, errors="coerce")
    # min_count=1: a row with every bucket missing stays missing, it is not a zero
    return values.sum(axis=1, min_count=1)


def _utilization_excess(frame: pd.DataFrame) -> pd.Series:
    values = numeric(frame, "revolving_utilization_unsecured")
    return (values - 1.0).clip(lower=0.0)


def _income_per_dependent(frame: pd.DataFrame) -> pd.Series:
    income = numeric(frame, "monthly_income")
    dependents = numeric(frame, "num_dependents")
    # unrecorded dependents use the declared constant; a *negative* count is not
    # an absence and not a zero, it is junk, so the feature stays missing
    usable = dependents.isna() | (dependents >= 0)
    return (income / (1.0 + dependents.fillna(0.0))).where(usable)


def _overcommitted(frame: pd.DataFrame) -> pd.Series:
    ratio = numeric(frame, "debt_ratio")
    return (ratio > 1.0).astype("float64").where(ratio.notna())


def _exposure_total(frame: pd.DataFrame) -> pd.Series:
    values = frame[["num_open_credit_lines", "num_real_estate_loans"]].apply(
        pd.to_numeric, errors="coerce"
    )
    return values.sum(axis=1, min_count=1)


def _income_not_reported(frame: pd.DataFrame) -> pd.Series:
    return numeric(frame, "monthly_income").isna().astype("float64")


FEATURES: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        name="delinquency_events_total",
        hypothesis=(
            "any recorded past-due event matters, not only the bucket a model happens to "
            "be given first; the three buckets measure one underlying behaviour"
        ),
        source_columns=tuple(LATE_PAYMENT_COLUMNS),
        decision_time_available=True,
        availability_note="credit bureau history, on file before the decision is made",
        transform=_delinquency_total,
        leakage_boundary="would become leakage if the window included months after origination",
    ),
    FeatureSpec(
        name="utilization_over_limit",
        hypothesis=(
            "reported utilisation above 100% is not a measurement error to discard but a "
            "stress signal: the borrower owes more than the lines they were given"
        ),
        source_columns=("revolving_utilization_unsecured",),
        decision_time_available=True,
        availability_note="bureau utilisation ratio at the report date",
        transform=_utilization_excess,
        leakage_boundary="nothing after origination is used; the raw ratio is kept too, so "
        "dropping this feature is reversible",
    ),
    FeatureSpec(
        name="income_per_dependent",
        hypothesis="income alone ignores burden; income per head of household is the ability to pay",
        source_columns=("monthly_income", "num_dependents"),
        decision_time_available=True,
        availability_note="both fields come from the application form",
        transform=_income_per_dependent,
        leakage_boundary="the constant fill for unrecorded dependents is declared in code, not "
        "estimated from the evaluation frame",
    ),
    FeatureSpec(
        name="overcommitted",
        hypothesis=(
            "debt ratio past 1.0 means scheduled payments exceed income; the flag survives the "
            "ratio's long tail, which a linear model would fit as noise"
        ),
        source_columns=("debt_ratio",),
        decision_time_available=True,
        availability_note="monthly obligations declared on the application",
        transform=_overcommitted,
        leakage_boundary="uses only the declared ratio, never an observed later payment series",
    ),
    FeatureSpec(
        name="recent_exposure_total",
        hypothesis="a thin file - few lines of any kind - predicts both default and missing data",
        source_columns=("num_open_credit_lines", "num_real_estate_loans"),
        decision_time_available=True,
        availability_note="bureau counts at the report date",
        transform=_exposure_total,
        leakage_boundary="counts, not ages of accounts; no post-decision activity",
    ),
    FeatureSpec(
        name="income_not_reported",
        hypothesis="if missingness carries information, the absence itself is a feature",
        source_columns=("monthly_income",),
        decision_time_available=True,
        availability_note="a field left blank on the form is visible at decision time",
        transform=_income_not_reported,
        requires_evidence="missing_income_may_be_informative",
        leakage_boundary="a presence indicator cannot leak an outcome, but it must not be kept "
        "if the missingness test shows a random pattern",
    ),
)

FEATURE_NAMES: tuple[str, ...] = tuple(spec.name for spec in FEATURES)
#: features that must be argued from a measurement before they are used
GATED_FEATURES: dict[str, str] = {
    spec.name: spec.requires_evidence for spec in FEATURES if spec.requires_evidence
}


def build_features(
    frame: pd.DataFrame,
    *,
    specs: Sequence[FeatureSpec] = FEATURES,
    mask_codes: bool = True,
) -> pd.DataFrame:
    """Add every engineered column, in declaration order.

    Idempotent: running it twice over its own output gives the same frame, which
    is what lets the notebook rebuild the matrix after re-splitting without
    keeping a second copy around.
    """
    out = mask_sentinels(frame) if mask_codes else frame.copy()
    for spec in specs:
        out[spec.name] = spec.transform(out)
    return out


def drop_feature(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    """Remove one engineered column - the ablation the challenge asks for."""
    if name not in frame.columns:
        raise KeyError(f"{name!r} is not on this frame")
    return frame.drop(columns=[name])


def model_matrix(
    frame: pd.DataFrame,
    *,
    base: Sequence[str],
    specs: Sequence[FeatureSpec] = FEATURES,
    drop: Sequence[str] = (),
) -> tuple[pd.DataFrame, pd.Series]:
    """Features and label for one fit, with the ablation set applied."""
    removed = set(drop)
    unknown = removed - {spec.name for spec in specs} - set(base)
    if unknown:
        raise KeyError(f"cannot drop unknown columns: {sorted(unknown)}")
    keep = [name for name in base if name not in removed]
    keep += [spec.name for spec in specs if spec.name not in removed]
    matrix = frame[keep]
    return matrix, frame[TARGET]


def audit_table(specs: Sequence[FeatureSpec] = FEATURES) -> pd.DataFrame:
    """The per-feature audit, as a table the notebook can print verbatim."""
    return pd.DataFrame(
        [
            {
                "feature": spec.name,
                "built from": ", ".join(spec.source_columns),
                "hypothesis": spec.hypothesis,
                "available before the decision": "yes"
                if spec.decision_time_available
                else "NO",
                "why": spec.availability_note,
                "boundary that keeps it honest": spec.leakage_boundary,
                "needs evidence": spec.requires_evidence or "-",
            }
            for spec in specs
        ]
    )
