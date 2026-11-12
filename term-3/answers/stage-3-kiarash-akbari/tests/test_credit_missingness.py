"""The missingness evidence: hand-checked statistics, not adjectives."""

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

from fixtures import frame

from credit.features import TARGET
from credit.missingness import (
    co_missingness,
    missingness_profile,
    two_proportion_test,
)


class TwoProportionTests(unittest.TestCase):
    def test_a_clear_difference_by_hand(self):
        # 50% versus 10% over 100 each: pooled 0.30, se sqrt(0.3*0.7*0.02) = 0.0648
        result = two_proportion_test(50, 100, 10, 100)
        self.assertAlmostEqual(result["rate_a"], 0.5)
        self.assertAlmostEqual(result["rate_b"], 0.1)
        self.assertAlmostEqual(result["difference"], 0.4)
        self.assertAlmostEqual(result["z"], 0.4 / math.sqrt(0.0042), places=10)
        self.assertLess(result["p_value"], 1e-6)

    def test_identical_rates_give_no_evidence(self):
        result = two_proportion_test(50, 100, 50, 100)
        self.assertAlmostEqual(result["z"], 0.0)
        self.assertAlmostEqual(result["p_value"], 1.0, places=6)

    def test_an_empty_group_is_nan_not_zero(self):
        result = two_proportion_test(0, 0, 5, 20)
        for key in ("rate_a", "rate_b", "difference", "z", "p_value"):
            self.assertTrue(math.isnan(result[key]), key)

    def test_a_degenerate_pooled_rate_is_nan(self):
        # every row non-default: the test has no variance to compare
        result = two_proportion_test(0, 40, 0, 60)
        self.assertTrue(math.isnan(result["z"]))
        self.assertEqual(result["rate_a"], 0.0)
        self.assertEqual(result["rate_b"], 0.0)


def _sample_frame():
    """20 rows where income is missing on exactly the four defaults."""
    rows = []
    for index in range(20):
        default = 1 if index < 4 else 0
        rows.append(
            {
                TARGET: default,
                "monthly_income": None if default == 1 else 4000.0,
                "age": 40.0,
                "debt_ratio": None if index in (18, 19) else 0.5,
            }
        )
    return frame(rows)


class ProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = missingness_profile(
            _sample_frame(), ("monthly_income", "age", "debt_ratio")
        )

    def test_one_row_per_column_in_the_order_asked(self):
        self.assertEqual(
            list(self.table["column"]), ["monthly_income", "age", "debt_ratio"]
        )

    def test_absence_is_counted_from_nan_only(self):
        by_name = {row["column"]: row for _, row in self.table.iterrows()}
        self.assertEqual(int(by_name["monthly_income"]["missing_rows"]), 4)
        self.assertEqual(int(by_name["age"]["missing_rows"]), 0)
        self.assertEqual(int(by_name["debt_ratio"]["missing_rows"]), 2)
        self.assertAlmostEqual(float(by_name["monthly_income"]["missing_rate"]), 0.2)

    def test_a_perfect_association_is_flagged(self):
        income = self.table.iloc[0]
        self.assertAlmostEqual(float(income["default_rate_when_missing"]), 1.0)
        self.assertAlmostEqual(float(income["default_rate_otherwise"]), 0.0)
        self.assertTrue(bool(income["informative"]))

    def test_a_column_without_absence_produces_no_claim(self):
        age = self.table.iloc[1]
        self.assertFalse(bool(age["informative"]))
        self.assertTrue(math.isnan(float(age["p_value"])))

    def test_two_absent_rows_out_of_twenty_is_not_evidence(self):
        debt = self.table.iloc[2]
        self.assertFalse(bool(debt["informative"]))
        self.assertGreater(float(debt["p_value"]), 0.05)

    def test_alpha_moves_the_verdict(self):
        strict = missingness_profile(_sample_frame(), ("monthly_income",), alpha=1e-12)
        self.assertFalse(bool(strict.iloc[0]["informative"]))


class CoMissingnessTests(unittest.TestCase):
    def test_pairs_that_travel_together_sort_first(self):
        rows = []
        for index in range(12):
            both_missing = index < 3
            scattered = index in (6, 7)
            rows.append(
                {
                    TARGET: 0,
                    "monthly_income": None if both_missing else 1000.0,
                    "num_dependents": None if both_missing else 1.0,
                    "age": None if scattered else 30.0,
                }
            )
        table = co_missingness(frame(rows), ("monthly_income", "num_dependents", "age"))
        self.assertEqual(list(table.columns)[-1], "odds_ratio")
        first = table.iloc[0]
        self.assertEqual(
            {first["left"], first["right"]}, {"monthly_income", "num_dependents"}
        )
        self.assertGreater(
            float(first["odds_ratio"]), float(table.iloc[-1]["odds_ratio"])
        )

    def test_counts_add_up_to_the_frame(self):
        table = co_missingness(_sample_frame(), ("monthly_income", "debt_ratio"))
        row = table.iloc[0]
        self.assertEqual(
            int(
                row["both_missing"]
                + row["left_only"]
                + row["right_only"]
                + row["neither"]
            ),
            20,
        )

    def test_a_single_column_gives_an_empty_table_not_an_error(self):
        self.assertTrue(co_missingness(_sample_frame(), ("age",)).empty)


if __name__ == "__main__":
    unittest.main()
