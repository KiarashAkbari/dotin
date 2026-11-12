"""The cost maths, pinned by hand.

Every expected value here is computed in the comment above it. If a future edit
changes what "cost per 1,000 applicants" means, this file is where it fails.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
for candidate in (
    HERE,
    HERE.parent / "src",
    HERE.parents[1] / "stage-1" / "src",
):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from credit.cost import (
    CHALLENGE_MATRIX,
    FLATTER_MATRIX,
    THRESHOLD_GRID,
    auc_rank,
    binary_counts,
    constant_baselines,
    cost_curve,
    cost_per_1000,
    matrix_sensitivity,
    metrics,
    reduction_vs_baseline,
    rule_baseline,
    select_threshold,
    total_cost,
)


class CountTests(unittest.TestCase):
    def test_confusion_counts_by_hand(self):
        # truth 1 1 0 0 / pred 1 0 1 0 -> one of each cell
        counts = binary_counts([1, 1, 0, 0], [1, 0, 1, 0])
        self.assertEqual(counts, {"tp": 1, "fp": 1, "tn": 1, "fn": 1})

    def test_probabilities_are_refused_as_labels(self):
        with self.assertRaises(ValueError):
            binary_counts([1, 0], [0.9, 0.1])

    def test_length_mismatch_is_refused(self):
        with self.assertRaises(ValueError):
            binary_counts([1, 0, 1], [1, 0])

    def test_missing_labels_are_refused(self):
        with self.assertRaises(ValueError):
            binary_counts([1, None], [1, 0])


class CostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.counts = {"tp": 1, "fp": 1, "tn": 1, "fn": 1}

    def test_the_matrix_is_the_one_the_challenge_sets(self):
        self.assertEqual(
            (CHALLENGE_MATRIX.false_negative, CHALLENGE_MATRIX.false_positive),
            (12.0, 1.0),
        )
        self.assertEqual(CHALLENGE_MATRIX.ratio, 12.0)

    def test_total_cost_by_hand(self):
        # 1 false alarm at 1 unit + 1 missed default at 12 units = 13
        self.assertEqual(total_cost(self.counts, CHALLENGE_MATRIX), 13.0)

    def test_cost_per_thousand_by_hand(self):
        # 13 units over 4 rows -> 3.25 per row -> 3250 per 1000
        self.assertEqual(cost_per_1000(self.counts, CHALLENGE_MATRIX), 3250.0)

    def test_cost_per_thousand_accepts_an_explicit_row_count(self):
        self.assertEqual(cost_per_1000(self.counts, CHALLENGE_MATRIX, n=13), 1000.0)

    def test_an_empty_frame_is_an_error_not_a_zero(self):
        with self.assertRaises(ValueError):
            cost_per_1000({"tp": 0, "fp": 0, "tn": 0, "fn": 0}, CHALLENGE_MATRIX)

    def test_metrics_are_consistent_with_the_same_counts(self):
        values = metrics(self.counts)
        self.assertEqual(values["accuracy"], 0.5)
        self.assertEqual(values["precision"], 0.5)
        self.assertEqual(values["recall"], 0.5)
        self.assertEqual(values["f1"], 0.5)
        self.assertEqual(values["positive_rate"], 0.5)
        self.assertEqual(values["flag_rate"], 0.5)
        self.assertEqual(values["undefined"], [])

    def test_undefined_ratios_are_flagged_instead_of_blurred(self):
        # nothing flagged: precision and recall have empty denominators
        values = metrics({"tp": 0, "fp": 0, "tn": 4, "fn": 0})
        self.assertEqual(values["precision"], 0.0)
        self.assertIn("precision", values["undefined"])
        self.assertIn("recall", values["undefined"])

    def test_accuracy_is_also_the_majority_ceiling_for_an_all_zero_predictor(self):
        # 9 negatives, 1 positive: predicting "safe" for everyone is 90% accurate
        values = metrics({"tp": 0, "fp": 0, "tn": 9, "fn": 1})
        self.assertEqual(values["accuracy"], 0.9)
        self.assertEqual(values["majority_class_accuracy"], 0.9)
        self.assertEqual(values["recall"], 0.0)


class ThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        # positives score 0.9 and 0.2, negatives 0.8 and 0.1
        self.y = [1, 0, 1, 0]
        self.p = [0.9, 0.8, 0.2, 0.1]

    def test_grid_spans_zero_to_one(self):
        self.assertAlmostEqual(THRESHOLD_GRID[0], 0.0)
        self.assertAlmostEqual(THRESHOLD_GRID[-1], 1.0)
        self.assertEqual(THRESHOLD_GRID.size, 201)

    def test_curve_has_one_row_per_threshold(self):
        curve = cost_curve(self.y, self.p, thresholds=[0.0, 0.5, 1.0])
        self.assertEqual(curve["threshold"].tolist(), [0.0, 0.5, 1.0])
        # t=0.0 flags all four -> tp2 fp2 -> cost 2 over 4 rows
        # t=0.5 flags 0.9 and 0.8 -> tp1 fp1 fn1 -> cost 13
        # t=1.0 flags nothing -> fn 2 -> cost 24
        self.assertEqual(curve["cost_per_1000"].tolist(), [500.0, 3250.0, 6000.0])

    def test_selected_threshold_is_the_cost_minimum(self):
        choice = select_threshold(self.y, self.p)
        # flagging {0.9, 0.8, 0.2} costs 1 false alarm; any higher threshold pays
        # a missed default (12), any lower adds another false alarm
        self.assertEqual(choice.cost_per_1000, 250.0)
        self.assertEqual(choice.counts, {"tp": 2, "fp": 1, "tn": 1, "fn": 0})

    def test_ties_go_to_the_lower_threshold(self):
        choice = select_threshold(self.y, self.p)
        # every grid point in (0.10, 0.20] flags the same three rows and pays one
        # false alarm: 0.105 through 0.200, so the choice is the cheapest of them
        self.assertEqual(choice.threshold, 0.105)
        self.assertEqual(choice.ties, 20)

    def test_selection_reports_its_own_matrix(self):
        choice = select_threshold(self.y, self.p)
        self.assertEqual(choice.matrix, "12:1")
        self.assertEqual(choice.as_dict()["counts"], choice.counts)

    def test_a_perfect_ranking_can_reach_zero_cost(self):
        # the only positive scores highest: any threshold in (0.5, 0.9] is free
        choice = select_threshold([1, 0, 0], [0.9, 0.5, 0.1])
        self.assertEqual(choice.cost_per_1000, 0.0)
        self.assertEqual(choice.threshold, 0.505)


class SensitivityTests(unittest.TestCase):
    def setUp(self) -> None:
        # one positive at 0.30, ten negatives crowded just above it: under 12:1 it
        # is cheaper to flag all eleven (10 false alarms) than to miss the default
        # (12 units); under 4:1 the balance flips and flagging nobody (4) wins.
        self.y = [1] + [0] * 10
        self.p = [0.30] + [round(0.31 + 0.01 * i, 2) for i in range(10)]

    def test_the_threshold_moves_with_the_matrix(self):
        rows = matrix_sensitivity(self.y, self.p, [CHALLENGE_MATRIX, FLATTER_MATRIX])
        self.assertEqual([row["matrix"] for row in rows], ["12:1", "4:1"])
        self.assertAlmostEqual(rows[0]["threshold"], 0.0)
        self.assertAlmostEqual(rows[1]["threshold"], 0.405)
        # 10 units over 11 rows, then 4 over 11
        self.assertAlmostEqual(rows[0]["cost_per_1000"], 10 / 11 * 1000)
        self.assertAlmostEqual(rows[1]["cost_per_1000"], 4 / 11 * 1000)

    def test_the_default_half_is_shown_as_the_thing_beaten(self):
        rows = matrix_sensitivity(self.y, self.p, [CHALLENGE_MATRIX])
        self.assertGreater(rows[0]["cost_per_1000_at_0.5"], rows[0]["cost_per_1000"])


class BaselineTests(unittest.TestCase):
    def test_constant_rules_are_the_floor(self):
        # two defaults in five rows: reject nobody -> 2*12 = 24, reject everybody
        # -> 3 false alarms = 3
        floors = constant_baselines([1, 1, 0, 0, 0])
        self.assertAlmostEqual(floors["flag_nobody"]["cost_per_1000"], 24 / 5 * 1000)
        self.assertAlmostEqual(floors["flag_everybody"]["cost_per_1000"], 3 / 5 * 1000)

    def test_a_one_column_rule_is_a_baseline_too(self):
        # flag anyone with a recorded 90-day late: catches both defaults, adds one
        # false alarm -> 1 unit of cost
        flags = [1, 1, 0, 0, 1]
        result = rule_baseline([1, 1, 0, 0, 0], flags, name="late-90-rule")
        self.assertEqual(result["counts"], {"tp": 2, "fp": 1, "tn": 2, "fn": 0})
        self.assertAlmostEqual(result["cost_per_1000"], 1 / 5 * 1000)

    def test_reduction_is_measured_against_the_floor(self):
        self.assertAlmostEqual(reduction_vs_baseline(4800.0, 3600.0), 25.0)
        with self.assertRaises(ZeroDivisionError):
            reduction_vs_baseline(0.0, 1.0)


class AucTests(unittest.TestCase):
    def test_perfect_and_reversed_rankings(self):
        self.assertEqual(auc_rank([1, 0], [0.9, 0.1]), 1.0)
        self.assertEqual(auc_rank([1, 0], [0.1, 0.9]), 0.0)

    def test_a_tie_is_half(self):
        self.assertEqual(auc_rank([1, 0], [0.5, 0.5]), 0.5)

    def test_rank_sum_by_hand(self):
        # both positives rank below the single negative -> 0.0
        self.assertEqual(auc_rank([1, 0, 1], [0.5, 0.6, 0.4]), 0.0)

    def test_one_class_is_an_error(self):
        with self.assertRaises(ValueError):
            auc_rank([0, 0], [0.1, 0.2])

    def test_it_agrees_with_scikit_learn(self):
        try:
            from sklearn.metrics import roc_auc_score
        except ImportError:  # pragma: no cover - the pin lives in requirements.txt
            self.skipTest("scikit-learn is not installed in this environment")
        y = [1, 0, 1, 1, 0, 0, 1, 0]
        p = [0.9, 0.8, 0.4, 0.3, 0.6, 0.2, 0.5, 0.1]
        self.assertAlmostEqual(auc_rank(y, p), float(roc_auc_score(y, p)), places=12)


class SanityTests(unittest.TestCase):
    def test_cost_is_scale_independent_of_split_size(self):
        counts = {"tp": 1, "fp": 1, "tn": 1, "fn": 1}
        self.assertAlmostEqual(
            cost_per_1000(counts, CHALLENGE_MATRIX),
            cost_per_1000(counts, CHALLENGE_MATRIX, n=4),
        )

    def test_no_nan_leaks_into_a_cost_report(self):
        choice = select_threshold([1, 0, 1, 0], [0.9, 0.8, 0.2, 0.1])
        self.assertFalse(math.isnan(choice.cost_per_1000))
        for value in choice.as_dict()["metrics"].values():
            self.assertFalse(isinstance(value, float) and math.isnan(value))


if __name__ == "__main__":
    unittest.main()
