"""Capacity and label arithmetic: the quota, the catch rate, and the two-family rule.

These three are the parts of the modelled decision that are arithmetic rather than
judgement, so they are pinned here while the rest of phase 5 is still a placeholder.
The brief's primary metric -- what share of negative reviews the top 10 % catches --
is defined by these lines, and it is the number the whole report is built around.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from capstone.capacity import (
    capacity_table,
    catch_rate,
    rank_then_select,
    top_share_indexes,
)
from capstone.config import load_contract
from capstone.errors import CapstoneError
from capstone.features import check_query_cost
from capstone.model import is_negative, label_definition, require_two_families
from helpers import EXAMPLE_CONFIG


class QuotaTest(unittest.TestCase):
    def test_the_quota_is_rounded_up(self) -> None:
        self.assertEqual(top_share_indexes(10, 0.10), [0])
        self.assertEqual(top_share_indexes(13, 0.10), [0, 1])
        self.assertEqual(top_share_indexes(1, 0.10), [0])
        self.assertEqual(top_share_indexes(0, 0.10), [])

    def test_a_share_outside_zero_and_one_is_a_usage_error(self) -> None:
        for share in (0.0, 1.0, 1.5, -0.1):
            with self.assertRaises(ValueError):
                top_share_indexes(10, share)

    def test_ranking_selects_the_highest_scores(self) -> None:
        scores = [0.1, 0.9, 0.5, 0.7]
        self.assertEqual(rank_then_select(scores, 0.5), [1, 3])

    def test_ties_follow_input_order_so_a_rerun_is_reproducible(self) -> None:
        scores = [0.5, 0.5, 0.5, 0.9]
        self.assertEqual(rank_then_select(scores, 0.5), [3, 0])
        self.assertEqual(rank_then_select(list(reversed(scores)), 0.5), [0, 1])

    def test_an_empty_score_list_selects_nothing(self) -> None:
        self.assertEqual(rank_then_select([], 0.1), [])


class CatchRateTest(unittest.TestCase):
    def test_the_metric_is_a_share_of_negatives(self) -> None:
        negatives = [True, False, True, True, False]
        selected = [True, False, False, False, False]
        self.assertAlmostEqual(catch_rate(negatives, selected), 1 / 3)

    def test_no_negatives_is_reported_not_invented(self) -> None:
        self.assertIsNone(catch_rate([False, False], [True, False]))

    def test_mismatched_lengths_are_a_usage_error(self) -> None:
        with self.assertRaises(ValueError):
            catch_rate([True], [True, False])

    def test_the_table_reports_extra_work_as_well_as_hits(self) -> None:
        negatives = [True, False, True, False, False]
        scores = [0.9, 0.8, 0.7, 0.6, 0.1]
        rows = capacity_table(negatives, scores, [0.2, 0.4, 0.6])
        self.assertEqual([row["reviewed"] for row in rows], [1, 2, 3])
        self.assertEqual([row["caught"] for row in rows], [1, 1, 2])
        self.assertEqual([row["extra"] for row in rows], [0, 1, 1])
        self.assertAlmostEqual(rows[0]["caught_share"], 0.5)


class LabelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config: object = load_contract(EXAMPLE_CONFIG)

    def test_one_and_two_are_negative_and_three_is_not(self) -> None:
        self.assertTrue(is_negative(1, self.config))
        self.assertTrue(is_negative(2, self.config))
        self.assertFalse(is_negative(3, self.config))
        self.assertFalse(is_negative(5, self.config))

    def test_a_score_the_source_cannot_produce_is_an_error(self) -> None:
        for score in (0, 6, -1):
            with self.assertRaises(CapstoneError):
                is_negative(score, self.config)

    def test_a_bool_is_not_a_score(self) -> None:
        with self.assertRaises(CapstoneError):
            is_negative(True, self.config)  # type: ignore[arg-type]

    def test_the_definition_is_one_line_for_the_report(self) -> None:
        self.assertEqual(label_definition(), "a review is negative at a score of 1..2")


class FamilyTest(unittest.TestCase):
    def test_two_families_pass(self) -> None:
        self.assertEqual(
            require_two_families(["gbdt.depth4", "logreg.l2"]), ["gbdt.depth4", "logreg.l2"]
        )

    def test_two_settings_of_one_family_do_not(self) -> None:
        with self.assertRaises(CapstoneError) as caught:
            require_two_families(["gbdt.depth4", "gbdt.depth6"])
        self.assertIn("settings of the family", str(caught.exception))

    def test_one_model_is_not_a_comparison(self) -> None:
        with self.assertRaises(CapstoneError):
            require_two_families(["gbdt.depth4"])

    def test_duplicates_are_a_typo_worth_naming(self) -> None:
        with self.assertRaises(CapstoneError):
            require_two_families(["gbdt.a", "logreg.b", "gbdt.a"])


class FeatureCeilingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_contract(EXAMPLE_CONFIG)

    def test_under_the_ceiling_returns_the_number_it_was_given(self) -> None:
        self.assertEqual(check_query_cost(412.5, self.config), 412.5)

    def test_over_the_ceiling_says_what_to_do(self) -> None:
        with self.assertRaises(CapstoneError) as caught:
            check_query_cost(780.0, self.config)
        message = str(caught.exception)
        self.assertIn("500 ms", message)
        self.assertIn("EXPLAIN", message)

    def test_a_negative_duration_is_not_a_measurement(self) -> None:
        with self.assertRaises(CapstoneError):
            check_query_cost(-1.0, self.config)

    def test_the_verdict_names_the_statistic_the_caller_judges(self) -> None:
        """One constant for the basis, or the docstrings and the manifest drift apart.

        They had: the module and this function both said the mean was judged while the caller
        handed over the fastest repeat, and the manifest field said "best". The refusal now prints
        the same string ``run`` records, so a future change of rule is a one-line edit that this
        test follows, and a docstring that lags behind is the failure mode it refuses to hide.
        """
        from capstone import features

        self.assertEqual(features.TIMING_BASIS, "best of the repeats")
        with self.assertRaises(CapstoneError) as caught:
            check_query_cost(780.0, self.config)
        self.assertIn(features.TIMING_BASIS, str(caught.exception))
        source = (Path(__file__).resolve().parents[1] / "src" / "capstone" / "features.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"judged_on": TIMING_BASIS,', source)
        self.assertNotIn(
            '"judged_on": "', source, "a second basis string, one edit away from lying"
        )

    def test_the_ceiling_comes_from_the_config_not_from_this_file(self) -> None:
        """Raise the measured number, lower the ceiling: the refusal has to follow the file."""
        self.assertEqual(self.config.features.feature_query_ms, 500)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capstone.toml"
            text = EXAMPLE_CONFIG.read_text(encoding="utf-8").replace(
                "feature_query_ms = 500", "feature_query_ms = 100"
            )
            path.write_text(text, encoding="utf-8")
            tighter = load_contract(path)
            with self.assertRaises(CapstoneError):
                check_query_cost(412.5, tighter)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
