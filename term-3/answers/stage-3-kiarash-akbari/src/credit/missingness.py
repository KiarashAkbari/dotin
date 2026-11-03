"""Does an absent value mean something? The answer is a number, per column.

The challenge asks whether missingness is random, and demands that the answer
change the policy if it should. So this module produces the evidence and stops:
it reports the association between absence and the label, and between absence and
the other columns, and the policy lives in ``prep`` where it can be argued.

No scipy here. A two-proportion z test needs only ``math.erf``, and a chi-square
over the whole pattern would be a single number where the per-column story is the
useful one.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd

from .features import TARGET, numeric

#: below this p-value the missingness of a column is called informative
DEFAULT_ALPHA = 0.05


def _normal_two_sided_p(z: float) -> float:
    """Two-sided p from the standard normal, via the error function."""
    return math.erfc(abs(z) / math.sqrt(2.0))


def two_proportion_test(
    successes_a: int, total_a: int, successes_b: int, total_b: int
) -> dict[str, float]:
    """Pooled two-proportion z test, reported with both rates.

    Empty groups give ``nan`` rather than a zero: a rate nobody can read is not
    evidence of anything, and inventing a ``p = 1`` would answer the question the
    challenge asked.
    """
    if total_a <= 0 or total_b <= 0:
        return {
            "rate_a": math.nan,
            "rate_b": math.nan,
            "difference": math.nan,
            "z": math.nan,
            "p_value": math.nan,
        }
    rate_a = successes_a / total_a
    rate_b = successes_b / total_b
    pooled = (successes_a + successes_b) / (total_a + total_b)
    spread = pooled * (1.0 - pooled) * (1.0 / total_a + 1.0 / total_b)
    if spread <= 0:
        return {
            "rate_a": rate_a,
            "rate_b": rate_b,
            "difference": rate_a - rate_b,
            "z": math.nan,
            "p_value": math.nan,
        }
    z = (rate_a - rate_b) / math.sqrt(spread)
    return {
        "rate_a": rate_a,
        "rate_b": rate_b,
        "difference": rate_a - rate_b,
        "z": z,
        "p_value": _normal_two_sided_p(z),
    }


def missingness_profile(
    frame: pd.DataFrame,
    columns: Sequence[str],
    *,
    target: str = TARGET,
    alpha: float = DEFAULT_ALPHA,
) -> pd.DataFrame:
    """Per column: how often it is absent, and whether absence tracks the label.

    ``informative`` is the only column this table is read for. It says the
    default rate among missing rows differs from the rest at ``alpha`` - which
    licenses an indicator column and an explicit imputation policy, and makes
    "we dropped the rows" look as arbitrary as it is.
    """
    label = numeric(frame, target).to_numpy(dtype="float64")
    rows: list[dict[str, object]] = []
    for column in columns:
        values = numeric(frame, column).to_numpy(dtype="float64")
        # both sides are numpy arrays here: pandas treats a raw ndarray used to
        # index a Series as positional, which would mask the wrong rows
        missing = np.isnan(values)
        total = int(missing.sum())
        present = int((~missing).sum())
        test = two_proportion_test(
            int(label[missing].sum()), total, int(label[~missing].sum()), present
        )
        rows.append(
            {
                "column": column,
                "missing_rows": total,
                "missing_rate": total / len(frame) if len(frame) else math.nan,
                "default_rate_when_missing": test["rate_a"],
                "default_rate_otherwise": test["rate_b"],
                "difference": test["difference"],
                "z": test["z"],
                "p_value": test["p_value"],
                "informative": bool(test["p_value"] == test["p_value"] and test["p_value"] < alpha),
            }
        )
    return pd.DataFrame(rows)


def co_missingness(frame: pd.DataFrame, columns: Sequence[str], *, top: int = 6) -> pd.DataFrame:
    """Do absences arrive together? A pair that travels is one mechanism, not two.

    Relevant because it decides whether one imputation policy can cover both
    columns or whether each needs its own argument.
    """
    mask = {
        column: np.isnan(numeric(frame, column).to_numpy(dtype="float64")) for column in columns
    }
    rows: list[dict[str, object]] = []
    names = list(columns)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            both = int((mask[left] & mask[right]).sum())
            only_left = int((mask[left] & ~mask[right]).sum())
            only_right = int((~mask[left] & mask[right]).sum())
            neither = int((~mask[left] & ~mask[right]).sum())
            total = both + only_left + only_right + neither
            # Haldane-Anscombe correction: a zero cell would make the ratio
            # infinite, and "infinite" is not a number anyone can read in a table
            odds_ratio = (both + 0.5) * (neither + 0.5) / ((only_left + 0.5) * (only_right + 0.5))
            rows.append(
                {
                    "left": left,
                    "right": right,
                    "both_missing": both,
                    "left_only": only_left,
                    "right_only": only_right,
                    "neither": neither,
                    "share_both_missing": both / total if total else math.nan,
                    "odds_ratio": odds_ratio,
                }
            )
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("odds_ratio", ascending=False, ignore_index=True).head(top)
