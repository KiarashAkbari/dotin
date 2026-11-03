import unittest

import pandas as pd

from data_factory.analysis import area_rates, median_by, monthly_median, tukey_fences


def frame(rows):
    return pd.DataFrame(rows)


class AnalysisTests(unittest.TestCase):
    def test_tukey_fences_flag_a_single_extreme_value(self):
        values = pd.Series([1.0] * 20 + [1000.0])
        result = tukey_fences(values)
        self.assertEqual((result["above"], result["below"], result["total"]), (1, 0, 21))

    def test_median_by_drops_small_groups(self):
        data = frame(
            {"borough": ["a", "a", "a", "b"], "duration_hours": [1.0, 2.0, 3.0, 9.0]}
        )
        table = median_by(data, "borough", min_size=2)
        self.assertEqual(list(table.index), ["a"])
        self.assertEqual(table.loc["a", "median"], 2.0)

    def test_monthly_median(self):
        data = frame(
            {
                "month": ["2025-01", "2025-01", "2025-02"],
                "duration_hours": [1.0, 3.0, 5.0],
            }
        )
        result = monthly_median(data)
        self.assertEqual((result["2025-01"], result["2025-02"]), (2.0, 5.0))

    def test_area_rates_use_population_and_skip_unknown_areas(self):
        data = frame(
            {
                "modzcta": ["1", "1", "2", None],
                "population": [1000.0, 1000.0, 250.0, None],
                "duration_hours": [1.0, 1.0, 1.0, 1.0],
            }
        )
        rates = area_rates(data)
        self.assertEqual(list(rates.index), ["2", "1"])
        self.assertEqual(rates.loc["2", "per_10k"], 40.0)
        self.assertEqual(rates.loc["1", "per_10k"], 20.0)