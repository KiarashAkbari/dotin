"""Capacity arithmetic: rank, cut at a share, count what was caught.

The brief's decision is a quota, not a probability threshold: the team can follow up
on 10 % of orders, so the system ranks and takes the top share. Keeping that in one
module means the 5 / 10 / 20 table, the secondary metric on the last month, and the
capacity paragraph of ``report.md`` all come from the same three lines of arithmetic.

The functions are pure and take plain sequences, so a notebook can hand them lists and
a test can hand them literals.
"""

from __future__ import annotations

from math import ceil

from .config import CapstoneConfig
from .pending import pending


@pending(phase=5, checklist=("6.3", "6.4", "3.7", "3.8"), step="capacity-list")
def report(config: CapstoneConfig, scores: list[float], negatives: list[bool]) -> list[dict]:
    """Write the 5 / 10 / 20 table the report and the notebooks both quote from.

    Phase 5 owns the body: it must join ``capacity_table`` with the secondary metric on
    the last evaluation month (3.8) and with the gap to the cheating model (4.5).
    """
    raise AssertionError("placeholder")


def top_share_indexes(size: int, share: float) -> list[int]:
    """Indexes of the top ``share`` of ``size`` rows, ties broken by input order.

    ``ceil`` rather than ``round``: a team with capacity for 10 % of 13 orders can
    still reach one more order, and dropping the remainder would quietly shrink the
    quota the report claims to have measured.
    """
    if size < 0:
        raise ValueError("size cannot be negative")
    if not 0.0 < share < 1.0:
        raise ValueError(f"share must be between 0 and 1, got {share}")
    if size == 0:
        return []
    return list(range(min(size, max(1, ceil(size * share)))))


def rank_then_select(scores: list[float], share: float) -> list[int]:
    """Indexes of the highest-scoring rows inside the quota, descending by score.

    Input order decides ties on purpose: it is the order the model produced, so a
    re-run with the same seed selects the same rows, and the selection is a function of
    the ranking alone rather than of a sort's stability.
    """
    if not scores:
        return []
    if not 0.0 < share < 1.0:
        raise ValueError(f"share must be between 0 and 1, got {share}")
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    return order[: max(1, ceil(len(scores) * share))]


def catch_rate(negatives: list[bool], selected: list[bool]) -> float | None:
    """Share of negative reviews the selected rows caught -- the brief's main metric.

    ``None`` means the denominator was empty. It is reported rather than turned into
    0.0 or 1.0, because on a thin evaluation month "no negatives to catch" and "caught
    nothing" are different findings and the report has to be able to tell them apart.
    """
    if len(negatives) != len(selected):
        raise ValueError(
            f"negatives has {len(negatives)} rows, selected has {len(selected)}; they must be "
            "the same frame"
        )
    total = sum(1 for flag in negatives if flag)
    if not total:
        return None
    caught = sum(
        1 for negative, chosen in zip(negatives, selected, strict=True) if negative and chosen
    )
    return caught / total


def capacity_table(negatives: list[bool], scores: list[float], shares: list[float]) -> list[dict]:
    """The 5 / 10 / 20 table: quota, rows reviewed, negatives caught, extra work.

    ``extra`` is the number of selected rows that were not negative -- the follow-up the
    team spends for free, which is the half of the trade-off a recall-only table hides.
    """
    if len(negatives) != len(scores):
        raise ValueError("negatives and scores must line up row by row")
    rows: list[dict] = []
    for share in shares:
        chosen = rank_then_select(scores, share)
        hits = sum(1 for index in chosen if negatives[index])
        total_negative = sum(1 for flag in negatives if flag)
        rows.append(
            {
                "share": share,
                "reviewed": len(chosen),
                "caught": hits,
                "caught_share": (hits / total_negative) if total_negative else None,
                "extra": len(chosen) - hits,
            }
        )
    return rows
