"""The end-of-analysis numbers, pinned by hand.

Holdout reporting, the per-band fairness view and the failure concentration are the
parts a reader cannot re-derive from a printed table, so every assertion below is a
number computed on paper from a four-row frame. If the cost arithmetic, the band
boundaries or the "which rows count as flagged" convention changes, a test fails here
rather than a report quietly quoting a different quantity.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[0]
for candidate in (
    HERE,
    ROOT / "src",
    ROOT.parents[1] / "stage-1" / "src",
):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from credit import cost, evaluate

TINY = pd.DataFrame(
    {
        "row_id": [1, 2, 3, 4],
        "serious_dlqin_2yrs": [1, 1, 0, 0],
        "age": [24.0, 40.0, 70.0, np.nan],
    }
)
SCORES = [0.9, 0.2, 0.8, 0.1]


class HoldoutReportTests(unittest.TestCase):
    def setUp(self):
        # one default caught, one missed, one clean applicant wrongly flagged
        self.report = evaluate.holdout_report(
            SCORES,
            [1, 1, 0, 0],
            threshold=0.5,
            family="logistic",
            cv=(3000.0, 100.0),
            sensitivity_matrix=cost.FLATTER_MATRIX,
        )

    def test_counts_and_cost_at_the_threshold(self):
        self.assertEqual(self.report.counts, {"tp": 1, "fp": 1, "tn": 1, "fn": 1})
        # (1 fp * 1 + 1 fn * 12) / 4 rows * 1000
        self.assertAlmostEqual(self.report.cost_per_1000, 3250.0, places=6)
        self.assertEqual(self.report.rows, 4)
        self.assertEqual(self.report.positives, 2)
        self.assertEqual(self.report.matrix, cost.CHALLENGE_MATRIX.name)

    def test_baselines_and_the_acceptance_bar_are_measured_against_the_same_rows(self):
        # flag-nobody misses both defaults: 2 * 12 / 4 * 1000
        self.assertAlmostEqual(self.report.baselines["flag-nobody"], 6000.0, places=6)
        # flag-everybody: 2 tp (cost 0) + 2 fp (cost 1 each) = 2 / 4 * 1000
        self.assertAlmostEqual(self.report.baselines["flag-everybody"], 500.0, places=6)
        self.assertAlmostEqual(
            self.report.reduction_vs_flagging_nobody_pct, 45.833333, places=4
        )
        self.assertGreater(self.report.reduction_vs_flagging_nobody_pct, 25.0)

    def test_the_cv_gap_is_part_of_the_same_record(self):
        payload = self.report.as_dict()
        self.assertEqual(payload["cv_mean_cost"], 3000.0)
        self.assertEqual(payload["cv_std_cost"], 100.0)
        self.assertEqual(payload["cv_holdout_gap"], 250.0)

    def test_auc_on_a_two_and_two_ranking(self):
        # three of the four positive/negative pairs are ordered correctly
        self.assertAlmostEqual(self.report.auc, 0.75, places=9)

    def test_mismatched_lengths_are_refused(self):
        with self.assertRaises(ValueError):
            evaluate.holdout_report(
                [0.5, 0.5], [1, 0, 1], threshold=0.5, family="logistic"
            )

    def test_sensitivity_reports_both_matrices(self):
        rows = self.report.sensitivity
        self.assertEqual(len(rows), 2)
        text = json.dumps(rows, default=str)
        self.assertIn("12:1", text)
        self.assertIn("4:1", text)


class BandTests(unittest.TestCase):
    def test_band_edges_are_half_open_and_reported_separately(self):
        bands = evaluate.define_bands(
            pd.DataFrame({"age": [24.0, 25.0, 34.9, 65.0, np.nan]})
        )
        self.assertEqual(
            bands.tolist(), ["0-25", "25-35", "25-35", "65+", evaluate.MISSING_AGE_BAND]
        )

    def test_a_missing_age_is_its_own_group_not_the_youngest_one(self):
        frame = pd.DataFrame({"age": [np.nan, np.nan]})
        self.assertEqual(set(evaluate.define_bands(frame)), {evaluate.MISSING_AGE_BAND})

    def test_subgroup_cost_is_the_same_arithmetic_as_the_global_cost(self):
        flags = (np.asarray(SCORES) >= 0.5).astype("int64")
        frame = TINY.assign(age_band=evaluate.define_bands(TINY))
        table = evaluate.subgroup_table(frame, [1, 1, 0, 0], flags, SCORES)
        self.assertEqual(
            set(table["age_band"]), {"0-25", "35-45", "65+", evaluate.MISSING_AGE_BAND}
        )
        # one row each: caught, missed, false alarm, correct pass
        costs = dict(zip(table["age_band"], table["cost_per_1000"], strict=True))
        self.assertAlmostEqual(costs["0-25"], 0.0, places=6)
        self.assertAlmostEqual(costs["35-45"], 12_000.0, places=6)
        self.assertAlmostEqual(costs["65+"], 1_000.0, places=6)
        self.assertAlmostEqual(costs[evaluate.MISSING_AGE_BAND], 0.0, places=6)
        self.assertEqual(int(table["rows"].sum()), 4)
        self.assertAlmostEqual(float(table["positive_rate"].sum()), 2.0, places=6)

    def test_flag_rate_and_positive_rate_are_reported_side_by_side(self):
        table = evaluate.subgroup_table(
            TINY, [1, 1, 0, 0], (np.asarray(SCORES) >= 0.5).astype("int64"), SCORES
        )
        self.assertIn("flag_rate", table.columns)
        self.assertIn("positive_rate", table.columns)
        self.assertLessEqual(table["flag_rate"].max(), 1.0)


class FailureTests(unittest.TestCase):
    def test_lift_and_the_misses_below_the_threshold(self):
        concentration = evaluate.failure_concentration(
            [0.9, 0.8, 0.2, 0.1], [1, 1, 0, 0], threshold=0.5, slice_share=0.5
        )
        self.assertEqual(concentration["slice_rows"], 2)
        self.assertEqual(concentration["slice_positives"], 2)
        # the top half holds 100% of the defaults against a 50% base rate: lift = 2
        self.assertAlmostEqual(concentration["lift_in_top_slice"], 2.0, places=6)
        self.assertEqual(concentration["defaults_below_threshold"], 0)
        self.assertEqual(concentration["flagged_rows"], 2)

    def test_where_it_fails_returns_three_sentsences_with_numbers(self):
        concentration = evaluate.failure_concentration(
            SCORES, [1, 1, 0, 0], threshold=0.5
        )
        frame = TINY.assign(age_band=evaluate.define_bands(TINY))
        table = evaluate.subgroup_table(
            frame, [1, 1, 0, 0], (np.asarray(SCORES) >= 0.5).astype("int64"), SCORES
        )
        lines = evaluate.where_it_fails(concentration, table)
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertRegex(line, r"\d", msg=line)
        self.assertTrue(line.startswith("3."), msg=lines[-1])

    def test_an_empty_frame_is_refused_rather_than_reporting_zero(self):
        with self.assertRaises(ValueError):
            evaluate.failure_concentration([], [], threshold=0.5)


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.report = evaluate.holdout_report(
            SCORES, [1, 1, 0, 0], threshold=0.5, family="logistic"
        )

    def test_metrics_table_covers_each_metric_with_its_limitation(self):
        table = evaluate.metrics_table(self.report)
        for key in evaluate.METRIC_LIMITATIONS:
            self.assertIn(key, table)
        self.assertIn("| metric | value |", table)
        self.assertEqual(len(table.splitlines()), 2 + len(evaluate.METRIC_LIMITATIONS))

    def test_accuracy_is_reported_as_the_misleading_number_it_is(self):
        table = evaluate.metrics_table(self.report)
        accuracy_note = next(
            line for line in table.splitlines() if line.startswith("| accuracy")
        )
        self.assertIn("12:1", accuracy_note)

    def test_the_model_card_carries_the_numbers_and_no_adjectives(self):
        card = evaluate.model_card(
            self.report,
            dataset_line="cs-training.csv sha256 abc",
            preparation_line="median+indicator",
            feature_lines=["monthly_income", "age"],
            trained_rows=90_000,
            code_version="git:test",
            intended_use="triage into a review queue",
            excluded_uses=["pricing decided by the model alone"],
        )
        for fragment in (
            "3250.00",
            "12:1",
            "0.5000",
            "90000",
            "cs-training.csv",
            "## Model card",
        ):
            self.assertIn(fragment, card, msg=fragment)
        self.assertGreater(
            len(card.splitlines()), 20, "a card is half a page, not a line"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
