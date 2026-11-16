"""The label, the two families, and nothing else.

``04`` fixes the label and leaves the modelling choices open, so this module owns the
part that is arithmetic -- which rows count as negative -- and the part the grader can
check without running a model: that "two families" means two families. The estimator
code itself belongs to ROADMAP phase 5 and is a placeholder here.

The label is defined on the review score, not on a probability: a review of 1 or 2 is
negative, ``04``'s words, and ``model.negative_max_score`` in config keeps the number
in one place instead of in four.
"""

from __future__ import annotations

from .config import NEGATIVE_MAX_SCORE, CapstoneConfig
from .errors import CapstoneError
from .pending import pending

#: Olist review scores run 1..5; anything else is a data bug, not a 0
REVIEW_SCORES = (1, 2, 3, 4, 5)


def is_negative(score: int, config: CapstoneConfig) -> bool:
    """Is this review a negative one, per the brief?"""
    if isinstance(score, bool) or not isinstance(score, int):
        raise CapstoneError(f"review_score must be an integer, got {score!r}")
    if score not in REVIEW_SCORES:
        raise CapstoneError(
            f"review_score {score} is outside 1..5; a value the source cannot produce must be "
            "quarantined, not labelled (stage 1's contract, still true here)"
        )
    return score <= config.model.negative_max_score


def require_two_families(names: list[str]) -> list[str]:
    """Refuse a model list where "two families" is two settings of one model.

    Names are expected as ``family.variant`` -- ``gbdt.depth3``, ``logreg.l2`` -- which
    is how a comparison table usually drifts into counting the same algorithm twice.
    """
    if len(names) < 2:
        raise CapstoneError(
            f"the brief asks for at least two model families, got {names or 'none'}"
        )
    families = {name.split(".")[0] for name in names}
    if len(families) < 2:
        raise CapstoneError(
            f"{names} are {len(names)} settings of the family {min(families)!r}, not two "
            "families; pick a second algorithm class and keep the comparison honest"
        )
    if len(set(names)) != len(names):
        raise CapstoneError(f"a model appears twice in {names}")
    return names


def label_definition() -> str:
    """One line the report can quote instead of restating the label from memory."""
    return f"a review is negative at a score of 1..{NEGATIVE_MAX_SCORE}"


@pending(phase=5, checklist=("6.1", "6.2", "6.5", "6.6"), step="model")
def train(config: CapstoneConfig, frame: object) -> dict:
    """Fit the baseline rule and at least two families, and return their metrics.

    ROADMAP phase 5 owns the body. It must also produce the rule-based baseline for the
    operations team with a real measured number (6.1), the confusion matrix in absolute
    counts at the operating point (6.5), and the spread across evaluation months (6.6).
    """
    raise AssertionError("placeholder")
