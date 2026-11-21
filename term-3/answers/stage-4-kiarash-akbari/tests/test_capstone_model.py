"""Phase 5's arithmetic: the metrics at the quota, the ablation, and the refusals around them.

Stdlib only, on purpose and not by accident: :mod:`capstone.model` speaks a two-method estimator
protocol so these tests can hand it a five-line scorer and check the numbers by hand, while the
student's run hands it scikit-learn and gets a real comparison table. A test that imported
``sklearn`` here would either skip quietly on a machine without it -- reporting an untested module
as tested -- or tie the offline suite to a dependency it has never had.

The hand-computed figures below are small on purpose: a metric that needs a spreadsheet to check
is not pinned, it is described.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capstone.config import load_contract
from capstone.errors import CapstoneError, InputError, NotReadyError
from capstone.model import (
    DENSITY_COLUMN,
    LABEL_KEY,
    MONTH_KEY,
    PRIOR_RATE,
    LabeledFrame,
    Scorer,
    ablation,
    auc,
    average_precision,
    fit_family,
    metrics_at_share,
    month_spread,
    numeric,
    read_frame,
    rule_scores,
    train,
)
from helpers import EXAMPLE_CONFIG

DECIDED_KEYS = ("no_review", "no_approved_at", "multi_review")


def decided_config(root: Path):
    """The example contract with D1/D2/D3 written down, and nothing else changed.

    Written here rather than read from a fixture file, because the point of the helper is that the
    only thing ``train`` needed was the three label policies -- a fixture that also carried a
    database uri or a family list would hide those decisions behind somebody else's file.
    """
    text = EXAMPLE_CONFIG.read_text(encoding="utf-8")
    for key in DECIDED_KEYS:
        text = text.replace(f'{key} = "unset"', f'{key} = "drop_before_training"')
    path = root / "capstone.toml"
    path.write_text(text, encoding="utf-8")
    return load_contract(path)


class PerfectScorer:
    """Puts 1.0 on every negative -- the ceiling a real model is measured against."""

    def fit(self, matrix, labels):
        self.seen = list(zip(matrix, labels, strict=True))
        return self

    def predict_proba(self, matrix):
        return [[0.0, 1.0] if flag else [1.0, 0.0] for _, flag in self.seen]


class InverseScorer:
    """Puts its margin upside down, and answers through ``decision_function`` instead."""

    def fit(self, matrix, labels):
        self.order = list(labels)
        return self

    def decision_function(self, matrix):
        return [-1.0 if flag else 1.0 for flag in self.order]


def rows(count: int, *, negative_every: int = 2, **extra) -> list[dict]:
    """A frame with ``count`` orders, every ``negative_every``-th one labelled negative."""
    out = []
    for index in range(count):
        row = {
            "order_id": f"o{index}",
            "promise_days": float(index),
            "freight_share": float(index % 3),
            MONTH_KEY: f"2018-0{index % 2 + 6}",
            LABEL_KEY: bool(index % negative_every == 0),
        }
        row.update(extra)
        out.append(row)
    return out


class FrameTests(unittest.TestCase):
    def test_the_label_columns_are_refused_as_features(self) -> None:
        with self.assertRaises(InputError) as caught:
            LabeledFrame.of(rows(4), ["promise_days", "worst_score"])
        self.assertIn("v_order_label", str(caught.exception))

    def test_a_frame_without_an_id_or_a_label_is_refused_by_name(self) -> None:
        with self.assertRaises(InputError) as caught:
            LabeledFrame.of([{"promise_days": 1.0}], ["promise_days"])
        self.assertIn("order_id", str(caught.exception))
        self.assertIn("is_negative", str(caught.exception))

    def test_a_column_nobody_has_is_refused_rather_than_dropped(self) -> None:
        with self.assertRaises(InputError) as caught:
            LabeledFrame.of(rows(3), ["promise_days", "shipping_tonnage"])
        self.assertIn("shipping_tonnage", str(caught.exception))

    def test_a_missing_number_refuses_and_text_refuses(self) -> None:
        with self.assertRaises(InputError) as caught:
            numeric({"order_id": "o", "weight": None}, "weight")
        self.assertIn(
            "section 7", str(caught.exception), msg="the refusal must route to the no-zero-fill rule"
        )
        with self.assertRaises(InputError):
            numeric({"order_id": "o", "state": "SP"}, "state")
        self.assertEqual(numeric({"order_id": "o", "flag": True}, "flag"), 1.0)

    def test_the_only_shipped_rule_is_the_base_rate_and_it_is_a_constant(self) -> None:
        frame = LabeledFrame.of(rows(6, negative_every=3), ["promise_days"])
        labels = frame.labels
        scores = rule_scores(frame, PRIOR_RATE, labels)
        self.assertEqual(set(scores), {2 / 6})
        with self.assertRaises(InputError):
            rule_scores(frame, "what_the_team_does_today", labels)


class FeatureSetTests(unittest.TestCase):
    def test_the_cut_line_follows_the_prefix_not_a_list_of_names(self) -> None:
        frame = LabeledFrame.of(
            rows(4, external_matched=True, **{DENSITY_COLUMN: 1.5}),
            ("promise_days", "freight_share", "external_matched", DENSITY_COLUMN),
        )
        sets = frame.feature_sets()
        self.assertEqual(sets["no_external"], ("promise_days", "freight_share"))
        self.assertEqual(
            sets["with_external"],
            ("promise_days", "freight_share", "external_matched", DENSITY_COLUMN),
        )
        self.assertEqual(sets["density_only"], ("promise_days", "freight_share", DENSITY_COLUMN))

    def test_density_only_appears_when_the_number_is_actually_in_the_frame(self) -> None:
        frame = LabeledFrame.of(rows(4, external_matched=True), ("promise_days", "external_matched"))
        self.assertNotIn("density_only", frame.feature_sets())

    def test_a_frame_with_no_external_columns_says_so_by_its_sets(self) -> None:
        frame = LabeledFrame.of(rows(4), ("promise_days", "freight_share"))
        sets = frame.feature_sets()
        self.assertEqual(sets["no_external"], sets["with_external"])
        self.assertEqual(frame.external_columns(), ())

    def test_the_ablation_is_a_difference_and_keeps_its_qualifier(self) -> None:
        runs = [
            {
                "scorer": "linear.perfect",
                "feature_set": "no_external",
                "features": ["promise_days"],
                "auc": 0.6,
                "quota": {"recall": 0.5},
            },
            {
                "scorer": "linear.perfect",
                "feature_set": "with_external",
                "features": ["promise_days", "external_matched"],
                "auc": 0.8,
                "quota": {"recall": 0.75},
            },
        ]
        result = ablation(runs)
        contrast = result["contrasts"][0]
        self.assertAlmostEqual(contrast["delta_auc"], 0.2)
        self.assertAlmostEqual(contrast["delta_catch_share"], 0.25)
        self.assertIn("not a measured lift", result["reading"])
        self.assertFalse(result["density_column_present"])

    def test_a_missing_half_of_a_pair_is_not_invented(self) -> None:
        result = ablation(
            [
                {
                    "scorer": "a.b",
                    "feature_set": "no_external",
                    "features": [],
                    "auc": 0.5,
                    "quota": {"recall": 0.1},
                }
            ]
        )
        self.assertEqual(result["contrasts"], [])


class MetricTests(unittest.TestCase):
    def test_separation_scores_one_and_reversal_zero(self) -> None:
        labels = [True, True, False, False]
        self.assertEqual(auc([0.9, 0.8, 0.2, 0.1], labels), 1.0)
        self.assertEqual(auc([0.1, 0.2, 0.8, 0.9], labels), 0.0)

    def test_a_constant_scorer_lands_on_one_half(self) -> None:
        self.assertEqual(auc([0.3] * 6, [True, False, True, False, True, False]), 0.5)

    def test_ties_get_half_credit(self) -> None:
        self.assertEqual(auc([0.5, 0.5], [True, False]), 0.5)

    def test_one_class_is_none_and_not_zero(self) -> None:
        self.assertIsNone(auc([0.1, 0.2], [False, False]))
        self.assertIsNone(average_precision([0.1, 0.2], [False, False]))

    def test_mismatched_lengths_are_a_usage_error(self) -> None:
        with self.assertRaises(InputError):
            auc([0.1, 0.2, 0.3], [True, False])

    def test_average_precision_is_the_mean_of_precisions_at_hits(self) -> None:
        scores = [0.9, 0.8, 0.7, 0.6]
        labels = [True, False, True, False]
        self.assertAlmostEqual(average_precision(scores, labels), (1.0 + 2 / 3) / 2)

    def test_the_confusion_matrix_is_absolute_counts_at_the_quota(self) -> None:
        labels = [True, True, True, True, False, False, False, False, False, False]
        scores = [0.9, 0.8, 0.2, 0.1, 0.7, 0.6, 0.5, 0.4, 0.3, 0.05]
        at_ten = metrics_at_share(scores, labels, 0.10)
        self.assertEqual(at_ten["reviewed"], 1)
        self.assertEqual((at_ten["tp"], at_ten["fp"], at_ten["fn"], at_ten["tn"]), (1, 0, 3, 6))
        self.assertEqual(at_ten["precision"], 1.0)
        self.assertAlmostEqual(at_ten["recall"], 0.25)
        at_fifty = metrics_at_share(scores, labels, 0.50)
        self.assertEqual((at_fifty["tp"], at_fifty["fp"]), (2, 3))
        self.assertAlmostEqual(at_fifty["f1"], 2 * 0.4 * 0.5 / 0.9)

    def test_an_empty_denominator_is_reported_not_zeroed(self) -> None:
        at = metrics_at_share([0.1, 0.2], [False, False], 0.5)
        self.assertEqual(at["tp"], 0)
        self.assertIsNone(at["recall"])
        self.assertEqual(at["tn"], 1)

    def test_the_month_spread_reranks_inside_the_month(self) -> None:
        scores = [0.4, 0.9, 0.1, 0.2]
        labels = [True, False, False, True]
        months = ["2018-06", "2018-06", "2018-07", "2018-07"]
        by = {row["month"]: row for row in month_spread(scores, labels, months, 0.5)}
        # in 2018-06 the stronger score is the non-negative row, so the quota catches nothing…
        self.assertEqual(by["2018-06"]["caught"], 0)
        # …and 2018-07's one-row quota is the negative row, because the ranking is per month
        self.assertEqual(by["2018-07"]["caught"], 1)
        self.assertEqual(by["2018-07"]["catch_share"], 1.0)
        self.assertEqual(by["2018-07"]["negatives"], 1)

    def test_a_half_tagged_frame_is_a_refusal(self) -> None:
        with self.assertRaises(InputError):
            month_spread([0.1, 0.2], [True, False], ["2018-06", None], 0.5)


class TrainTests(unittest.TestCase):
    def frame(self, count: int = 12) -> LabeledFrame:
        return LabeledFrame.of(rows(count), ("promise_days", "freight_share"))

    def two_families(self) -> list[Scorer]:
        return [
            Scorer.family("linear.perfect", PerfectScorer()),
            Scorer.family("tree.inverse", InverseScorer()),
        ]

    def test_undecided_label_policies_stop_the_step_before_any_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            undecided = load_contract(EXAMPLE_CONFIG)
            with self.assertRaises(NotReadyError) as caught:
                train(undecided, self.frame(), scorers=self.two_families())
            self.assertIn("model.no_review", str(caught.exception))
            self.assertIn("unset", str(caught.exception))

    def test_two_families_are_required_and_a_rule_is_not_a_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            with self.assertRaises(CapstoneError) as caught:
                train(config, self.frame(), scorers=[Scorer.family("gbdt.a", PerfectScorer())])
            self.assertIn("at least two model families", str(caught.exception))
            with self.assertRaises(CapstoneError) as caught:
                train(
                    config,
                    self.frame(),
                    scorers=[
                        Scorer.family("gbdt.depth3", PerfectScorer()),
                        Scorer.family("gbdt.depth5", InverseScorer()),
                    ],
                )
            self.assertIn("not two families", str(caught.exception))
            with self.assertRaises(CapstoneError):
                train(config, self.frame(), baseline_rules=(PRIOR_RATE,))

    def test_every_run_is_scored_on_every_feature_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            frame = LabeledFrame.of(
                rows(8, external_matched=True, **{DENSITY_COLUMN: 2.5}),
                ("promise_days", "freight_share", "external_matched", DENSITY_COLUMN),
            )
            result = train(config, frame, scorers=self.two_families())
            pairs = {(run["scorer"], run["feature_set"]) for run in result["runs"]}
            # three scorers -- the two families and the prior_rate baseline -- over three feature
            # sets: the baseline is a row of the same table, so it belongs in the same loop
            self.assertEqual(len(pairs), 3 * 3, msg="two families and a baseline, three sets each")
            self.assertEqual(result["rows"], 8)
            self.assertEqual(result["negatives"], 4)
            self.assertEqual(result["label"], "a review is negative at a score of 1..2")
            perfect = next(run for run in result["runs"] if run["scorer"] == "linear.perfect")
            self.assertEqual(perfect["auc"], 1.0)
            self.assertEqual(perfect["quota"]["fp"], 0)
            self.assertIn("prior_rate", {run["scorer"] for run in result["runs"]})
            self.assertTrue(result["ablation"]["density_column_present"])
            for run in result["runs"]:
                self.assertEqual(len(run["months"]), 2, msg="two evaluation months were tagged")

    def test_the_best_run_is_the_one_that_catches_more(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            result = train(
                config,
                self.frame(),
                scorers=[
                    Scorer.family("linear.inverse", InverseScorer()),
                    Scorer.family("tree.perfect", PerfectScorer()),
                ],
            )
            self.assertEqual(result["best"]["scorer"], "tree.perfect")
            self.assertEqual(result["best"]["kind"], "family")
            self.assertIn("not a measured lift", result["ablation"]["reading"])

    def test_an_untagged_frame_reports_no_months_rather_than_inventing_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            materialised = [{k: v for k, v in row.items() if k != MONTH_KEY} for row in rows(6)]
            result = train(
                config,
                LabeledFrame.of(materialised, ("promise_days", "freight_share")),
                scorers=self.two_families(),
            )
            self.assertTrue(all("months" not in run for run in result["runs"]))

    def test_unlabelled_rows_are_counted_and_not_labelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            materialised = rows(6, negative_every=3)
            materialised[4][LABEL_KEY] = None  # D2: no review, so no label
            result = train(
                config,
                LabeledFrame.of(materialised, ("promise_days", "freight_share")),
                scorers=self.two_families(),
            )
            self.assertEqual(result["unlabelled_rows"], 1)
            self.assertEqual(result["rows"], 5)
            self.assertEqual(result["best"]["unlabelled_rows"], 1)

    def test_a_frame_with_nothing_labelled_is_a_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            materialised = rows(4)
            for row in materialised:
                row[LABEL_KEY] = None
            with self.assertRaises(InputError) as caught:
                train(
                    config,
                    LabeledFrame.of(materialised, ("promise_days", "freight_share")),
                    scorers=self.two_families(),
                )
            self.assertIn("denominator", str(caught.exception))

    def test_an_empty_frame_refuses_before_a_table_of_none_is_printed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            frame = LabeledFrame(rows=(), columns=("promise_days",))
            with self.assertRaises(InputError):
                train(config, frame, scorers=self.two_families())

    def test_a_null_feature_refuses_before_it_is_zero_filled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            materialised = rows(4)
            materialised[2]["promise_days"] = None
            with self.assertRaises(InputError) as caught:
                train(
                    config,
                    LabeledFrame.of(materialised, ("promise_days", "freight_share")),
                    scorers=self.two_families(),
                )
            self.assertIn("promise_days", str(caught.exception))

    def test_a_feature_set_can_be_narrowed_without_editing_the_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            result = train(
                config,
                LabeledFrame.of(rows(4), ("promise_days", "freight_share")),
                scorers=self.two_families(),
                share=0.5,
            )
            self.assertEqual(result["operational_share"], 0.5)
            self.assertEqual(result["best"]["quota"]["reviewed"], 2)

    def test_the_scored_frame_carries_the_six_columns_a_list_needs(self) -> None:
        """Phase 5's output has to be phase 6's input, by name and by order.

        The literal is the header ``capacity.LIST_HEADER`` will declare when the list writer arrives
        (it is not in this tree yet -- ``capacity.py`` ships the table arithmetic, not the file), and
        the test names the six columns rather than skipping the seam: without it the two modules can
        each be green while the chain stays unbuildable, which is exactly what the ``capacity-list``
        placeholder exists to keep visible. When the writer lands, this becomes a comparison against
        ``LIST_HEADER[:6]`` and the file-writing half of the check comes with it.
        """
        config = decided_config(Path(tempfile.mkdtemp()))
        result = train(config, self.frame(), scorers=self.two_families())
        scored = result["scored_frame"]
        self.assertEqual(
            list(scored[0]),
            ["order_id", "score", "label", "is_negative_review", "decision_moment_date", "model_run_id"],
        )
        self.assertEqual(len(scored), result["rows"])

    def test_scores_travel_with_the_run_so_the_list_can_be_rebuilt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            result = train(config, self.frame(), scorers=self.two_families())
            for run in result["runs"]:
                self.assertEqual(len(run["scores"]), result["rows"])
                self.assertEqual(run["model_run_id"], f"{run['scorer']}@{run['feature_set']}")


class FamilyProtocolTests(unittest.TestCase):
    def test_predict_proba_is_used_and_decision_function_is_the_fallback(self) -> None:
        frame = LabeledFrame.of(rows(4), ("promise_days", "freight_share"))
        labels = [bool(row[LABEL_KEY]) for row in frame.rows]
        perfect = fit_family(Scorer.family("toy.perfect", PerfectScorer()), frame, labels)
        self.assertEqual(perfect, [1.0, 0.0, 1.0, 0.0])
        inverse = fit_family(Scorer.family("toy.inverse", InverseScorer()), frame, labels)
        self.assertEqual(inverse, [-1.0, 1.0, -1.0, 1.0])
        self.assertEqual(auc(perfect, labels), 1.0)
        self.assertEqual(auc(inverse, labels), 0.0)

    def test_a_scorer_missing_fit_is_named_before_it_is_called(self) -> None:
        frame = LabeledFrame.of(rows(4), ("promise_days", "freight_share"))
        with self.assertRaises(InputError) as caught:
            fit_family(Scorer.family("toy.none", object()), frame, [True, False, True, False])
        self.assertIn("fit(matrix, labels)", str(caught.exception))

    def test_a_scorer_that_cannot_rank_names_both_methods_it_lacks(self) -> None:
        """The refusal lists the protocol, because "it has fit" is the misleading half.

        A fitted object that only answers ``predict`` is the common case here -- someone hands
        ``LogisticRegression`` a wrapper that returns classes -- and a message that names one
        missing method would send them to the wrong line of the sklearn docs.
        """

        class OnlyFit:
            def fit(self, matrix, labels):
                return self

        frame = LabeledFrame.of(rows(4), ("promise_days", "freight_share"))
        with self.assertRaises(InputError) as caught:
            fit_family(Scorer.family("toy.unranked", OnlyFit()), frame, [True, False, True, False])
        self.assertIn("predict_proba", str(caught.exception))
        self.assertIn("decision_function", str(caught.exception))


class ReadFrameTests(unittest.TestCase):
    """``read_frame`` is the one place the frame and the label are allowed to meet."""

    COLUMNS = [
        "order_id",
        "promise_days",
        "freight_share",
        "is_negative",
        "worst_score",
        "review_count",
        "has_review",
        MONTH_KEY,
    ]

    def setUp(self) -> None:
        from capstone import features

        self.features = features
        self.original_connect = features.connect
        self.sql: list[str] = []
        self.closed: list[bool] = []

    def tearDown(self) -> None:
        self.features.connect = self.original_connect

    def feed(self, values: list[tuple]) -> None:
        from capstone import features

        description = [type("Column", (), {"name": name})() for name in self.COLUMNS]
        test = self

        class Cursor:
            def execute(self, sql):
                test.sql.append(sql)

            def fetchall(self):
                return values

            @property
            def description(self):
                return description

        class Connection:
            def cursor(self):
                return Cursor()

            def close(self):
                test.closed.append(True)

        features.connect = lambda uri, **kwargs: Connection()

    def config(self, root: Path):
        config = decided_config(root)
        return config

    def test_the_query_names_both_views_and_the_purpose_says_which_step_needs_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            self.feed([("o1", 1.0, 0.0, True, 2, 1, True, "2018-06")])
            with mock.patch.dict(os.environ, {"CAPSTONE_DATABASE_URI": "postgresql+psycopg://x/y"}):
                frame = read_frame(config)
            sql = self.sql[0]
            self.assertIn("capstone.v_feature_frame", sql)
            self.assertIn("capstone.v_order_label", sql)
            self.assertNotIn("v_decision_moment", sql)
            self.assertEqual(frame.columns, ("promise_days", "freight_share"))
            self.assertEqual(frame.labels, [True])
            self.assertEqual(frame.months, ["2018-06"])
            self.assertEqual(self.closed, [True], msg="a frame reader that leaks connections is a bug")

    def test_an_omit_list_that_names_nothing_in_the_frame_is_a_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            self.feed([("o1", 1.0, 0.0, True, 2, 1, True, "2018-06")])
            with mock.patch.dict(os.environ, {"CAPSTONE_DATABASE_URI": "postgresql+psycopg://x/y"}):
                with self.assertRaises(InputError) as caught:
                    read_frame(config, omit=["product_lumens"])
            self.assertIn("product_lumens", str(caught.exception))

    def test_a_dropped_column_stays_dropped_and_the_rest_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            self.feed([("o1", 1.0, 0.0, True, 2, 1, True, "2018-06")])
            with mock.patch.dict(os.environ, {"CAPSTONE_DATABASE_URI": "postgresql+psycopg://x/y"}):
                frame = read_frame(config, omit=["freight_share"])
            self.assertEqual(frame.columns, ("promise_days",))

    def test_an_empty_join_says_which_step_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            self.feed([])
            with mock.patch.dict(os.environ, {"CAPSTONE_DATABASE_URI": "postgresql+psycopg://x/y"}):
                with self.assertRaises(NotReadyError) as caught:
                    read_frame(config)
            self.assertIn("capstone features", str(caught.exception))

    def test_without_a_uri_the_refusal_is_about_the_variable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = decided_config(Path(tmp))
            environment = {k: v for k, v in os.environ.items() if k != "CAPSTONE_DATABASE_URI"}
            with mock.patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(CapstoneError) as caught:
                    read_frame(config)
            self.assertIn("CAPSTONE_DATABASE_URI", str(caught.exception))
            self.assertIn("model step", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
