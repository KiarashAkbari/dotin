"""The label, the two families, the metrics at the quota, and the ablation.

``04`` fixes the label and leaves the modelling choices open, so this module owns the parts
that are arithmetic -- which rows count as negative, how a ranked list is scored inside a
quota, what "two families" means -- and none of the judgement those choices carry. The
estimators arrive from the caller: this tree ships no scikit-learn and its offline suite is
stdlib-only by rule, so :func:`train` speaks a two-method protocol (``fit(X, y)`` and
``predict_proba(X)``, or ``decision_function(X)`` when a class has no probabilities) and any
object satisfying it counts as a family. That is not a convenience borrowed for the tests:
the numbers in ``report.md`` have to come from the student's machine and his database, and a
module that imported ``sklearn`` at the top would make the arithmetic here unrunnable, which
is how a comparison table starts getting filled in by hand.

The label is defined on the review score, not on a probability: a review of 1 or 2 is
negative, ``04``'s words, and ``model.negative_max_score`` in config keeps the number in one
place instead of in four.

``train`` refuses rather than improvising in four places, and each refusal says which decision
is missing: a config that has not decided D1/D2/D3, a frame whose label is also a feature, a
NULL where a number was asked for (decisions.md section 7 forbids zero-filling), a scorer list
that is two settings of one algorithm.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, NamedTuple

from .capacity import capacity_table, rank_then_select
from .config import NEGATIVE_MAX_SCORE, CapstoneConfig
from .errors import CapstoneError, InputError, NotReadyError
from .features import TECHNICAL_COLUMNS
from .pending import pending

#: Olist review scores run 1..5; anything else is a data bug, not a 0
REVIEW_SCORES = (1, 2, 3, 4, 5)

#: how ``sql/003``'s ``v_order_label`` spells the target, and what sits beside it
LABEL_KEY = "is_negative"
#: the columns the label view carries, refused as features because the frame must not meet them
LABEL_COLUMNS = frozenset({"is_negative", "worst_score", "review_count", "has_review"})
#: a ``YYYY-MM`` tag per row, supplied by whoever sliced the evaluation window
MONTH_KEY = "decision_month"

#: what ``sql/005`` prepends to every column the external signal contributes -- the ablation's
#: cut line, taken from the schema rather than from a list of names that would drift
EXTERNAL_PREFIX = "external_"
#: the one number of the three agregado ``4714`` offers that is neither area nor population
DENSITY_COLUMN = "external_density_2010"

#: the baseline the brief can state without a model: rank everybody at the observed rate
PRIOR_RATE = "prior_rate"

#: the view ``sql/006`` publishes the signal through. The frame itself is not touched -- ``005``'s
#: own header records why (an unverified external source must not be able to move a modelled number),
#: and the bridge is where that decision stops: a separate view, joined by ``order_id``, so
#: ``v_feature_frame`` keeps its 29 columns and its audit story.
EXTERNAL_VIEW = "capstone.v_external_feature"
#: the two counts the coverage view answers with, and nothing else -- a catalog lookup and one
#: aggregate, which is what lets a refusal name the migration instead of the row
EXTERNAL_COVERAGE_VIEW = "capstone.v_external_feature_coverage"
#: how the external columns get into a frame. ``auto`` reads them when the bridge is applied and
#: skips it when it is not; ``off`` never reads them, which is how a reviewer reproduces the
#: baseline-only half of the table; ``required`` refuses a database without ``sql/006``, because a
#: missing migration and a signal that did not help are different findings and the report cannot
#: print one word for both.
EXTERNAL_MODES = ("auto", "off", "required")


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


#: the two populations decisions D1 and D2 are about, counted where they live
DECISION_COUNTS_SQL = (
    "SELECT count(*) FILTER (WHERE order_approved_at IS NULL), "
    "count(*) FILTER (WHERE order_approved_at IS NULL AND order_status = 'canceled') "
    "FROM olist.orders"
)
D2_COUNT_SQL = "SELECT count(*) FROM capstone.v_d2_unlabelled_orders"


def decision_counts(connection: Any) -> dict[str, int]:
    """D1 and D2 as numbers: orders without ``order_approved_at`` (and how many were canceled), and
    approved orders that never got a review. Read-only; the notebook prints them so the report's
    first two decisions trace to it rather than to a log."""
    with connection.cursor() as cursor:
        cursor.execute(DECISION_COUNTS_SQL)
        no_approval, canceled = cursor.fetchone()
        cursor.execute(D2_COUNT_SQL)
        (no_review,) = cursor.fetchone()
    return {
        "no_approved_at": int(no_approval),
        "no_approved_at_canceled": int(canceled),
        "approved_without_review": int(no_review),
    }


def label_definition() -> str:
    """One line the report can quote instead of restating the label from memory."""
    return f"a review is negative at a score of 1..{NEGATIVE_MAX_SCORE}"


class LabeledFrame(NamedTuple):
    """Rows a scorer can be fitted on, with the label kept one method away.

    ``columns`` is the feature list and is *not* read off the rows' keys: the frame carries the
    order id, the decision moment, and the label's own columns, and a feature list derived from
    ``row.keys()`` would hand all three to the model. The caller names the features; this type
    only refuses the ones that must never be named (``LABEL_COLUMNS``), which is the leak gate
    of ``test_leak_gate.py`` applied to the model's input instead of to the SQL view.
    """

    rows: tuple[Mapping[str, Any], ...]
    columns: tuple[str, ...]

    @classmethod
    def of(
        cls,
        rows: Iterable[Mapping[str, Any]],
        columns: Sequence[str],
    ) -> "LabeledFrame":
        materialised = tuple(rows)
        leaked = sorted(set(columns) & LABEL_COLUMNS)
        if leaked:
            raise InputError(
                f"{leaked} are label columns of capstone.v_order_label, not features; sql/003 "
                "keeps them out of v_feature_frame on purpose, and a model that reads them is "
                "the cheating baseline of item 10.3 rather than a result"
            )
        missing = [key for key in (LABEL_KEY, "order_id") if materialised and key not in materialised[0]]
        if missing:
            raise InputError(
                f"a frame for train() must carry {missing}; the label is what the quota is "
                "scored against and the order id is what a reviewer checks a row by"
            )
        unknown = sorted(set(columns) - set(materialised[0] or {}))
        if unknown:
            raise InputError(
                f"columns {unknown} are not in the rows; name features the frame actually has"
            )
        return cls(rows=materialised, columns=tuple(columns))

    @property
    def labels(self) -> list[bool]:
        """The target, with D2's unlabelled orders reported as ``None`` rather than as ``False``.

        ``v_order_label.is_negative`` is NULL when no review exists, and ``separate_population``
        (D2, decisions.md section 2) says those rows are not the model's to label. Turning NULL into
        ``False`` here would put 768 invented positives in the denominator of every metric.
        """
        return [row[LABEL_KEY] if row[LABEL_KEY] is not None else None for row in self.rows]

    @property
    def months(self) -> list[str | None]:
        return [row.get(MONTH_KEY) for row in self.rows]

    def external_columns(self) -> tuple[str, ...]:
        return tuple(name for name in self.columns if name.startswith(EXTERNAL_PREFIX))

    def feature_sets(self) -> dict[str, tuple[str, ...]]:
        """The three lists an ablation needs, built from one frame rather than three reads.

        ``no_external`` is the candidate set minus every ``external_*`` column, ``with_external``
        is the whole list, and ``density_only`` isolates :data:`DENSITY_COLUMN` beside the
        baseline features. The first two always exist; the third is reported as absent rather
        than silently equal to ``no_external``, because "the 614 number is not in this frame" is
        a finding the report has to be able to state.
        """
        outside = tuple(name for name in self.columns if not name.startswith(EXTERNAL_PREFIX))
        sets = {"no_external": outside, "with_external": self.columns}
        if DENSITY_COLUMN in self.columns:
            sets["density_only"] = outside + (DENSITY_COLUMN,)
        return sets

    def vector(self, row: Mapping[str, Any]) -> tuple[float, ...]:
        return tuple(numeric(row, name) for name in self.columns)


def numeric(row: Mapping[str, Any], name: str) -> float:
    """One cell as a number, with the two refusals this project owes a reader.

    A NULL is refused instead of zero-filled (decisions.md section 7, the no-zero-fill rule: a missing
    row is a fact about the data, and 0.0 says "this order is close" when the truth is
    "this order has no geolocation row"). A text cell is refused because ``customer_state`` is a
    feature only after somebody encodes it, and that encoding is a decision with its own risks.
    """
    value = row[name]
    if value is None:
        raise InputError(
            f"feature {name!r} is NULL for order {row.get('order_id')!r}; drop the column, or "
            "give it an ``_unavailable`` indicator in sql/003 -- filling it in here would "
            "contradict decisions.md section 7, which is the whole reason the flags exist"
        )
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    raise InputError(
        f"feature {name!r} holds {value!r} ({type(value).__name__}) for order "
        f"{row.get('order_id')!r}; train() takes numbers, and turning a code into one is the "
        "kind of step that belongs in a migration or in decisions.md, not in a model's setter"
    )


def auc(scores: Sequence[float], labels: Sequence[bool]) -> float | None:
    """ROC AUC by the rank formula, with half credit for ties.

    ``sum(ranks of positives) - n+(n++1)/2`` over ``n+ n-`` is the Mann-Whitney statistic; it
    costs one sort instead of one pass per pair, which matters at 99,281 rows. Ties get averaged
    ranks so a scorer that returns a constant (the prior-rate baseline) lands on 0.5 and not on
    whatever a lucky comparison order produces. ``None`` when one class is absent -- on a thin
    evaluation month "undefined" and "0.5" are different sentences.
    """
    if len(scores) != len(labels):
        raise InputError(f"{len(scores)} scores for {len(labels)} labels")
    positives = sum(1 for flag in labels if flag)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    order = sorted(range(len(scores)), key=lambda index: scores[index])
    ranks: list[float] = [0.0] * len(scores)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and scores[order[end + 1]] == scores[order[start]]:
            end += 1
        average = (start + end) / 2 + 1
        for position in range(start, end + 1):
            ranks[order[position]] = average
        start = end + 1
    rank_sum = sum(ranks[index] for index, flag in enumerate(labels) if flag)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def average_precision(scores: Sequence[float], labels: Sequence[bool]) -> float | None:
    """AP as the mean precision at each hit, the rank-based form of the PR curve.

    No interpolation and no sklearn: at the 10 % quota the difference between this and a
    trapezoid over the curve is a rounding note, and the rounding note is not worth a dependency
    that cannot be imported in the environment the offline suite runs in.
    """
    positives = sum(1 for flag in labels if flag)
    if not positives or positives == len(labels):
        return None
    order = sorted(range(len(labels)), key=lambda index: (-scores[index], index))
    hits = 0
    total = 0.0
    for rank, index in enumerate(order, start=1):
        if labels[index]:
            hits += 1
            total += hits / rank
    return total / positives


def metrics_at_share(
    scores: Sequence[float], labels: Sequence[bool], share: float
) -> dict[str, Any]:
    """The confusion matrix in absolute counts at the quota, and the three rates off it.

    The brief asks for the matrix at the *operating point*, not at 0.5: with a 10 % quota the
    threshold is the row the ranking lands on. ``fn`` and ``tn`` are written even though no rate
    in this project divides by ``tn`` -- a matrix a reader cannot recompute is a decoration.
    """
    chosen = set(rank_then_select(list(scores), share))
    tp = sum(1 for index, flag in enumerate(labels) if flag and index in chosen)
    fp = len(chosen) - tp
    fn = sum(1 for flag in labels if flag) - tp
    tn = len(labels) - tp - fp - fn
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision and recall and (precision + recall)
        else None
    )
    return {
        "share": share,
        "reviewed": len(chosen),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def month_spread(
    scores: Sequence[float],
    labels: Sequence[bool],
    months: Sequence[str | None],
    share: float,
) -> list[dict[str, Any]]:
    """The quota, re-ranked inside each evaluation month (``04`` section 3's second metric, 6.6).

    Ranking once over the whole window and then slicing by month would answer "how did the
    global top 10 % land across months", which is a different question and the flattering one.
    ``capacity_table`` is reused rather than reimplemented, so a month's number is computed by
    the same three lines of arithmetic the capacity report quotes.
    """
    if len(months) != len(labels) or any(label is None for label in months):
        raise InputError(
            "the month spread needs one YYYY-MM label per row; the caller sliced the window, so "
            "this is where a frame that forgot to tag its rows shows up"
        )
    out: list[dict[str, Any]] = []
    for month in sorted({str(label) for label in months}):
        inside = [index for index, label in enumerate(months) if str(label) == month]
        rows = capacity_table(
            [labels[index] for index in inside],
            [scores[index] for index in inside],
            [share],
        )
        out.append(
            {
                "month": month,
                "rows": len(inside),
                "negatives": sum(1 for index in inside if labels[index]),
                "reviewed": rows[0]["reviewed"],
                "caught": rows[0]["caught"],
                "catch_share": rows[0]["caught_share"],
            }
        )
    return out


class Scorer(NamedTuple):
    """One row of the comparison table: a name, a kind, and something that gives scores."""

    name: str
    kind: str
    estimator: Any = None

    @classmethod
    def rule(cls, name: str) -> "Scorer":
        return cls(name=name, kind="rule")

    @classmethod
    def family(cls, name: str, estimator: Any) -> "Scorer":
        return cls(name=name, kind="family", estimator=estimator)


def rule_scores(frame: LabeledFrame, name: str, labels: list[bool | None]) -> list[float]:
    """The model-free rules of the brief's table (6.1), scored over the same rows as the models.

    ``prior_rate`` gives every order the observed negative rate: it is not a ranking, and that
    is the point -- the quota then selects by input order, which is what "no model, just the
    base rate" costs. The second baseline the report table asks for ("only delivery delay", and
    "what the team does today") is a *column* of the frame, so it is asked for by name in
    :func:`train` and ranked by :meth:`LabeledFrame` rather than hard-coded here: a delay column
    computed after the decision moment is a hindsight baseline and the report has to be able to
    say so, which it can only do if the name came from the caller.
    """
    if name != PRIOR_RATE:
        raise InputError(
            f"unknown baseline rule {name!r}; train() ships {PRIOR_RATE} and takes any other "
            "rule as an estimator, so it arrives with its own name and its own feature list"
        )
    scored = [flag for flag in labels if flag is not None]
    if not scored:
        raise InputError("prior_rate needs at least one labelled row to measure a rate against")
    rate = sum(1 for flag in scored if flag) / len(scored)
    return [rate] * len(labels)


def fit_family(scorer: Scorer, frame: LabeledFrame, labels: list[bool]) -> list[float]:
    """Fit one family on the frame's vectors and return one score per row.

    ``predict_proba`` is tried first and ``decision_function`` is the fallback, because a linear
    model in scikit-learn answers with the margin and a tree ensemble with two columns; both are
    the same ranking to this project. ``X`` is a list of tuples rather than a DataFrame on
    purpose: the frame crosses from the database as rows, and a dependency here would decide the
    report's numbers for the reader who does not have it installed.
    """
    fitted = scorer.estimator
    if not hasattr(fitted, "fit"):
        raise InputError(
            f"{scorer.name} has no fit(matrix, labels); the protocol this module asks for is fit "
            "then rank, and an unfitted scorer would leave the comparison table with a row nobody "
            "trained"
        )
    if not hasattr(fitted, "predict_proba") and not hasattr(fitted, "decision_function"):
        raise InputError(
            f"{scorer.name} has neither predict_proba nor decision_function; a fitted object this "
            "module can score has to rank rows, which is all the brief's quota asks of it"
        )
    matrix = [frame.vector(row) for row in frame.rows]
    fitted.fit(matrix, [int(flag) for flag in labels])
    if hasattr(fitted, "predict_proba"):
        rows = fitted.predict_proba(matrix)
        return [float(row[1]) for row in rows]
    return [float(value) for value in fitted.decision_function(matrix)]


def train(
    config: CapstoneConfig,
    frame: LabeledFrame | None = None,
    *,
    scorers: Sequence[Scorer] = (),
    baseline_rules: Sequence[str] = (PRIOR_RATE,),
    share: float | None = None,
    omit: Sequence[str] = (),
    on_refusal: str = "raise",
) -> dict[str, Any]:
    """Score every baseline and family, on every feature set, at the operational quota -- IN SAMPLE.

    Not the model step. Review B1 found this function fits and scores the same rows and then picks
    the "best" run on those in-sample numbers; it is kept only as the arithmetic harness its tests
    pin (a hand-countable scorer protocol), and every result it returns says ``"in_sample": True``.
    The chain's model step is :func:`capstone.system.run`: time split on ``split.boundary``,
    selection on a validation slice inside the training window, one score on the evaluation months.
    No number in report.md comes from here.

    The comparison the brief asks for is a table, so this returns one entry per (scorer, feature
    set) pair rather than "a model": the baseline and the two families are ranked by the same
    ``capacity.rank_then_select`` on the same rows, and the ablation is the difference between
    two of those entries. Nothing here claims a lift -- an ablation of the same fitted runs is
    the honest form of "did the external signal help", and it is what ``report.md``'s
    ``[external]`` block will carry.

    ``frame=None`` is the chain's call: it reads the frame from the database the config names,
    which is the step reaching its connection rather than a placeholder refusing to exist. Pass
    a frame and no database is touched, which is how the offline tests run.

    ``scorers`` empty is a refusal that names what is missing. The brief leaves "which two
    families" to the student, so an implementation that picked them would write the report's
    central comparison into library code and call it a default.

    ``on_refusal`` is what one unusable feature set does to the rest of the table. ``"raise"`` (the
    default) aborts, which is the behaviour every other caller of this module has always had;
    ``"record"`` keeps the healthy rows and attaches the refusal sentence to the row that could not
    be scored -- the shape the ablation harness needs, because a bridge that matched some
    municipalities and not others leaves NULLs the rest of the corpus cannot be scored over, and
    losing the baseline rows to that would be losing the only measured numbers in the table. Neither mode invents anything:
    ``"record"`` writes an ``error`` where a metric would have been, and
    :func:`ablation` reports ``status="refused"`` instead of a difference.
    """
    if on_refusal not in ("raise", "record"):
        raise InputError(
            f"on_refusal={on_refusal!r} is neither 'raise' nor 'record'; both are honest, an empty "
            "third option is not"
        )
    undecided = config.model.undecided
    if undecided:
        raise NotReadyError(
            f"the model cannot label rows while {', '.join(undecided)} are still 'unset': D1/D2/D3 "
            "are decisions.md's to make, and the loader keeps them visible so a pandas default "
            "cannot decide them instead",
            context={"undecided": list(undecided)},
        )
    if frame is None:
        frame = read_frame(config, omit=omit)
    if not frame.rows:
        raise InputError(
            "the frame has no rows: a comparison table over an empty frame is a table of None "
            "values, which is not the same finding as a model that does not work"
        )
    roster: list[Scorer] = [Scorer.rule(name) for name in baseline_rules] + list(scorers)
    # the brief's "two families" is checked over the whole roster, so a caller cannot satisfy it
    # by naming two baselines: a rule is not an algorithm class, and a table that counts one prior
    # and one constant as "two families" is the drift require_two_families exists to catch
    require_two_families([scorer.name for scorer in roster if scorer.kind == "family"])
    names = [scorer.name for scorer in roster]

    quota = float(config.capacity.share if share is None else share)
    raw_labels = frame.labels
    kept = [index for index, flag in enumerate(raw_labels) if flag is not None]
    labels = [bool(raw_labels[index]) for index in kept]
    if not kept:
        raise InputError(
            f"every one of {len(raw_labels)} rows is unlabelled, so no metric has a denominator; "
            "D2's separate population is reported as a count, not scored"
        )
    kept_frame = LabeledFrame(
        rows=tuple(frame.rows[index] for index in kept), columns=frame.columns
    )
    sets = frame.feature_sets()
    months = [frame.months[index] for index in kept]
    if any(name is None for name in months) and any(name is not None for name in months):
        raise InputError(
            f"{sum(1 for name in months if name is None)} of {len(months)} rows carry no "
            f"{MONTH_KEY}; a month spread over a half-tagged frame compares a month against "
            "nothing. Tag every row or none -- 6.6 is the whole point of the tag"
        )

    runs: list[dict[str, Any]] = []
    for set_name, columns in sets.items():
        sub = LabeledFrame(rows=kept_frame.rows, columns=columns)
        for scorer in roster:
            try:
                if scorer.kind == "rule":
                    scores = rule_scores(sub, scorer.name, labels)
                else:
                    scores = fit_family(scorer, sub, labels)
            except CapstoneError as exc:
                if on_refusal != "record":
                    raise
                # One row of the table refuses; the table does not vanish with it. A column that is
                # NULL everywhere (a clone with no fetch yet) and a family that cannot fit are both
                # findings, and printing the first as ``0.0`` would be the invention this stage has
                # refused since phase 0 -- so the run keeps its name, carries the sentence, and
                # leaves every metric undefined for the report to render as a blank.
                runs.append(
                    {
                        "scorer": scorer.name,
                        "kind": scorer.kind,
                        "feature_set": set_name,
                        "features": list(columns),
                        "model_run_id": f"{scorer.name}@{set_name}",
                        "error": str(exc),
                        "auc": None,
                        "average_precision": None,
                        "quota": None,
                        "rows": len(labels),
                        "unlabelled_rows": len(raw_labels) - len(labels),
                        "scores": [],
                    }
                )
                continue
            run: dict[str, Any] = {
                "scorer": scorer.name,
                "kind": scorer.kind,
                "feature_set": set_name,
                "features": list(columns),
                "model_run_id": f"{scorer.name}@{set_name}",
                "auc": auc(scores, labels),
                "average_precision": average_precision(scores, labels),
                "quota": metrics_at_share(scores, labels, quota),
                "rows": len(labels),
                "unlabelled_rows": len(raw_labels) - len(labels),
                # the ranking itself, kept: ``capacity.write_list`` and the report's appendix both
                # have to be able to say which rows the quota selected, and a metric alone cannot
                # be checked by a reviewer
                "scores": scores,
            }
            if all(name is not None for name in months):
                run["months"] = month_spread(scores, labels, months, quota)
            runs.append(run)

    scored_runs = [run for run in runs if "error" not in run]
    if not scored_runs:
        raise InputError(
            f"all {len(runs)} runs refused before scoring, so there is no table: the first refusal was "
            f"{runs[0]['error'] if runs else 'no run was attempted'}",
            context={"errors": [run.get("error", "") for run in runs]},
        )
    best = max(scored_runs, key=_rank_key)
    return {
        "in_sample": True,
        "label": label_definition(),
        "operational_share": quota,
        "rows": len(labels),
        "negatives": sum(1 for flag in labels if flag),
        "unlabelled_rows": len(raw_labels) - len(labels),
        "scorers": names,
        "feature_sets": {name: list(columns) for name, columns in sets.items()},
        "runs": runs,
        "refused_runs": [run for run in runs if "error" in run],
        "best": best,
        "ablation": ablation(runs),
        "scored_frame": [
            {
                "order_id": row["order_id"],
                "score": best["scores"][index],
                "label": int(bool(raw_labels[kept[index]])),
                "is_negative_review": int(bool(raw_labels[kept[index]])),
                "decision_moment_date": row.get(MONTH_KEY) or row.get("order_approved_at") or "",
                "model_run_id": best["model_run_id"],
            }
            for index, row in enumerate(kept_frame.rows)
        ],
    }


def external_readiness(config: CapstoneConfig) -> dict[str, Any]:
    """What this database can actually say about the external signal, before anyone fits anything.

    Three facts, each measured and each with a distinct absent case: the bridge view exists
    (``sql/006`` applied), it carries rows, and the density column has values. A caller that skips
    this and goes straight to ``train`` gets a refusal too, but a refusal naming NULLs -- which is
    the wrong sentence about a database where nothing was ever fetched. This is the one place that
    reads the difference, and it reads it from ``005``'s and ``006``'s own views rather than
    recounting them here.
    """
    from .features import connect, database_uri

    uri = database_uri(config, purpose="ask what the external signal can support")
    connection = connect(uri, purpose="ask what the external signal can support")
    try:
        cursor = connection.cursor()
        view_present = _regclass(cursor)
        coverage: dict[str, Any] | None = None
        if view_present:
            cursor.execute(
                "SELECT frame_rows, matched_rows, valued_rows, value_share "
                f"FROM {EXTERNAL_COVERAGE_VIEW}"
            )
            names = [column.name for column in cursor.description]
            row = cursor.fetchone()
            coverage = dict(zip(names, row, strict=True)) if row else None
    finally:
        connection.close()
    if not view_present:
        note = (
            "no capstone.v_external_feature: apply sql/006_external_bridge.sql, then fetch and load "
            "the signal with `capstone external`"
        )
    elif coverage is None or not coverage.get("frame_rows"):
        note = "the frame is empty, so there is nothing for the signal to join to"
    elif not coverage.get("valued_rows"):
        note = (
            f"the bridge is applied but {DENSITY_COLUMN} has no values: the fetch has not stored rows "
            "for the municipalities this frame's orders live in, so the ablation is refused and not zero"
        )
    else:
        note = (
            f"{coverage['valued_rows']} of {coverage['frame_rows']} orders can see a value "
            f"({float(coverage['value_share'] or 0):.2%}) -- an ablation restricted to those rows is "
            "an ablation over that population, and the report has to say so"
        )
    return {"view_present": view_present, "coverage": coverage, "note": note}


def run(config: CapstoneConfig) -> dict:
    """The chain step: read the frame, fit the roster on a time split, persist the scored frame.

    The work lives in :mod:`capstone.system` (pandas + scikit-learn, imported late so the offline
    arithmetic in this module keeps needing nothing): the scores go to ``capstone.order_score`` and
    the run's metrics to ``capstone.model_run``, which is the carrier ``capacity.report`` reads --
    the database, so the two steps can run as separate commands.
    """
    from .system import run as system_run

    return system_run(config)


def _rank_key(run: Mapping[str, Any]) -> tuple[float, float, str]:
    """Rank by the brief's primary metric, then by AUC, then by name.

    A ``None`` catch share -- a quota that caught nothing because there was nothing to catch --
    sorts as ``-1.0`` rather than as ``0.0`` so an undefined number never wins a comparison, and
    the name tie-break exists because ``max`` over equal keys otherwise picks by row order,
    which is not a reason to print one model as the best.
    """
    if "error" in run or run.get("quota") is None:
        # a refused row is not a model that scored nothing, and ``max`` over a table that mixes the
        # two would hand "best" to whoever happens to sort first
        return (float("-inf"), float("-inf"), run["scorer"])
    catch = run["quota"]["recall"]
    return (
        -1.0 if catch is None else catch,
        -1.0 if run["auc"] is None else run["auc"],
        run["scorer"],
    )


def ablation(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``with_external`` minus ``no_external``, scorer by scorer, on the same fitted runs.

    This is the whole of what this project says about the external signal's value until a
    forward-in-time experiment exists: the same rows, the same labels, the same quota, one extra
    block of columns. It is not a lift, and the sentence saying so is part of the return value
    so a notebook cannot drop it by accident.
    """
    by_key = {(run["scorer"], run["feature_set"]): run for run in runs}
    contrasts = []
    refused: list[dict[str, Any]] = []
    for scorer in sorted({run["scorer"] for run in runs}):
        outside = by_key.get((scorer, "no_external"))
        inside = by_key.get((scorer, "with_external"))
        if outside is None or inside is None:
            continue
        if "error" in inside:
            # the honest half of "did the signal help": say which side refused, rather than letting
            # a missing column read as a measured difference of nothing
            refused.append(
                {"scorer": scorer, "side": "with_external", "error": inside["error"]}
            )
            continue
        if "error" in outside:
            refused.append(
                {"scorer": scorer, "side": "no_external", "error": outside["error"]}
            )
            continue
        if tuple(outside["features"]) == tuple(inside["features"]):
            refused.append(
                {
                    "scorer": scorer,
                    "side": "both",
                    "error": "the two feature sets are the same list: no external column reached the "
                    "frame, so the ablation has nothing to differ over",
                }
            )
            continue
        contrasts.append(
            {
                "scorer": scorer,
                # a rule baseline reads no matrix, so its two arms are the same scores and its delta
                # is zero by construction -- printing it beside a family's delta without this field
                # would let a reader count "0.0000" as a finding about the signal. ``get`` because
                # :func:`ablation` takes any run mappings, and a caller that did not classify a
                # scorer gets ``None`` rather than a guess from this function
                "kind": outside.get("kind"),
                "catch_share_without": outside["quota"]["recall"],
                "catch_share_with": inside["quota"]["recall"],
                "delta_catch_share": _delta(outside["quota"]["recall"], inside["quota"]["recall"]),
                "auc_without": outside["auc"],
                "auc_with": inside["auc"],
                "delta_auc": _delta(outside["auc"], inside["auc"]),
            }
        )
    return {
        "contrasts": contrasts,
        # four states, because "absent", "nothing could be scored" and "some arms were scored" are
        # three different sentences about a database, and a table with both a measured contrast and a
        # refused one is the fourth -- none of them may be printed as the others
        "status": (
            "measured"
            if contrasts and not refused
            else "partial"
            if contrasts
            else "refused"
            if refused
            else "absent"
        ),
        "refused": refused,
        "density_column_present": DENSITY_COLUMN in {
            name for run in runs for name in run["features"]
        },
        "reading": (
            "an ablation of the same fitted runs at the same quota, not a measured lift: "
            "no forward-in-time experiment was run, and 616 is the 2010 census density"
        ),
    }


def _delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    return after - before


def _regclass(cursor: Any) -> bool:
    """Does ``capstone.v_external_feature`` exist here? One statement, and it reads the catalog.

    The relation name is a module constant, so it is written into the SQL rather than bound: a
    placeholder here would make the answer depend on which driver's paramstyle the caller has, and
    the question is not about the driver. Anything the user could steer is a parameter -- this is a
    name this file owns.
    """
    cursor.execute(f"SELECT to_regclass('{EXTERNAL_VIEW}') IS NOT NULL")
    return bool(cursor.fetchone()[0])


def read_frame(
    config: CapstoneConfig,
    *,
    omit: Sequence[str] = (),
    external: str = "auto",
    require_matched: bool = False,
) -> LabeledFrame:
    """The frame and its label, read in one query, for the chain step that has no argument.

    ``omit`` is where a column that the model cannot take goes: a feature with NULLs is refused by
    :func:`numeric` (decisions.md section 7), so the caller who decides to drop it says so here, and the
    list lands in the run's ``features`` field where the report can print what was left out. A
    default of ``()`` means nothing is dropped and the refusal is heard instead.

    ``v_feature_frame`` carries the features and ``v_order_label`` the target; the join is
    written here rather than as a new view because a view that fuses them would undo sql/003's
    separation -- the reason ``test_leak_gate.py`` can promise a feature query cannot inherit
    the label. This function is the one place allowed to put them side by side, and it is what
    ``train(config)`` reaches for.

    ``external`` decides whether ``capstone.v_external_feature`` rides along (see
    :data:`EXTERNAL_MODES`). Under ``auto`` one extra statement asks the catalog whether the view
    exists: it is a ``to_regclass`` lookup and not a data read, and it is what lets the no-``006``
    case answer with an empty ``external_*`` set instead of a driver error about a missing relation
    -- which is the state of every tree before this one's patch is applied, and of a fresh clone
    where ``006`` arrived but no fetch ever stored a row. ``require_matched`` then restricts the rows to the orders whose municipality the signal
    actually reached, and it restricts *every* feature set at once: an ablation that compared the
    matched rows to all the rows would be measuring the population, not the column.
    """
    from .features import connect, database_uri

    if external not in EXTERNAL_MODES:
        raise InputError(
            f"external={external!r} is none of {', '.join(EXTERNAL_MODES)}; the mode is the one thing "
            "that decides whether the ablation has two columns to differ over"
        )
    if require_matched and external == "off":
        raise InputError(
            "require_matched without an external join would filter on a column the frame never "
            "reads -- the refusal is the point: that combination means 'matched rows of nothing'"
        )
    select = [
        "f.*",
        "l.is_negative",
        "l.worst_score",
        "l.review_count",
        "l.has_review",
        "to_char(f.order_approved_at, 'YYYY-MM') AS decision_month",
    ]
    joins = ["JOIN capstone.v_order_label AS l USING (order_id)"]
    where: list[str] = []
    uri = database_uri(config, purpose="read the frame for the model step")
    connection = connect(uri, purpose="read the frame and its label for the model step")
    try:
        cursor = connection.cursor()
        bridge = external != "off"
        if external == "auto":
            # one statement, and it reads the catalog: "no sql/006 here" is an answer, not an error
            bridge = _regclass(cursor)
        elif external == "required":
            if not _regclass(cursor):
                raise NotReadyError(
                    f"{EXTERNAL_VIEW} is not in this database, and external='required' means the "
                    "ablation has to run: apply sql/006_external_bridge.sql (it needs the table "
                    "sql/005 created and a fetch that stored rows)",
                    context={"view": EXTERNAL_VIEW, "mode": external},
                )
        if bridge:
            select += [
                "x.external_matched",
                "x.external_ibge_code",
                "x.external_indicator_count",
                f"x.{DENSITY_COLUMN}",
            ]
            joins.append(f"LEFT JOIN {EXTERNAL_VIEW} AS x USING (order_id)")
            if require_matched:
                where.append("x.external_matched")
        sql = (
            "SELECT " + ", ".join(select) + " FROM capstone.v_feature_frame AS f " + " ".join(joins)
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY f.order_id"
        cursor.execute(sql)
        names = [column.name for column in cursor.description]
        rows = [dict(zip(names, values, strict=True)) for values in cursor.fetchall()]
    finally:
        connection.close()
    if not rows:
        raise NotReadyError(
            "capstone.v_feature_frame joined to capstone.v_order_label returned no rows: the "
            "frame exists only after `capstone features` has run against a loaded olist schema",
            context={"rows": 0},
        )
    dropped = set(omit)
    unknown = sorted(dropped - set(names))
    if unknown:
        skipped = sorted(name for name in unknown if name.startswith(EXTERNAL_PREFIX))
        hint = (
            f" -- the external columns are not in this read at all (external={external!r}), so "
            "there is nothing here to omit"
            if skipped
            else ""
        )
        raise InputError(
            f"omit names {unknown}, which the frame does not have; the frame's own columns are "
            f"the only ones worth dropping, and a typo here would drop a feature nobody chose{hint}"
        )
    columns = tuple(
        name
        for name in names
        if name not in TECHNICAL_COLUMNS
        and name not in LABEL_COLUMNS
        and name != MONTH_KEY
        and name not in dropped
    )
    return LabeledFrame.of(rows, columns)
