"""Feature maths and the audit structure the challenge grades.

Each expected value is written next to the input that produces it, so a silent
change in a definition - summing over a code, filling a blank with zero - fails
here rather than in a metric table.
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

from fixtures import frame

from credit.columns import MODEL_FEATURE_COLUMNS
from credit.features import (
    FEATURE_NAMES,
    FEATURES,
    GATED_FEATURES,
    SENTINEL_VALUES,
    TARGET,
    audit_table,
    build_features,
    drop_feature,
    mask_sentinels,
    model_matrix,
    sentinel_spike_counts,
)


def one_row(**values: object):
    return frame([{**values}])


class SentinelTests(unittest.TestCase):
    def test_codes_become_nan_but_real_counts_stay(self):
        source = one_row(
            serious_dlqin_2yrs=0,
            num_late_30_59=5.0,
            num_late_90=96.0,
            num_late_60_89=98.0,
        )
        masked = mask_sentinels(source)
        self.assertEqual(float(masked["num_late_30_59"].iloc[0]), 5.0)
        self.assertTrue(math.isnan(float(masked["num_late_90"].iloc[0])))
        self.assertTrue(math.isnan(float(masked["num_late_60_89"].iloc[0])))

    def test_the_input_frame_is_not_modified(self):
        # the raw table keeps the file's values; only the analysis frame loses them
        source = one_row(num_late_90=96.0, serious_dlqin_2yrs=0)
        mask_sentinels(source)
        self.assertEqual(float(source["num_late_90"].iloc[0]), 96.0)

    def test_spike_counts_measure_the_share_and_the_label(self):
        source = frame(
            [
                {
                    TARGET: 0,
                    "num_late_90": 96.0,
                    "num_late_30_59": 0.0,
                    "num_late_60_89": 0.0,
                },
                {
                    TARGET: 0,
                    "num_late_90": 96.0,
                    "num_late_30_59": 0.0,
                    "num_late_60_89": 0.0,
                },
                {
                    TARGET: 1,
                    "num_late_90": 0.0,
                    "num_late_30_59": 0.0,
                    "num_late_60_89": 0.0,
                },
                {
                    TARGET: 0,
                    "num_late_90": 1.0,
                    "num_late_30_59": 0.0,
                    "num_late_60_89": 0.0,
                },
            ]
        )
        table = sentinel_spike_counts(source, columns=("num_late_90",), codes=(96,))
        self.assertEqual(len(table), 1)
        row = table.iloc[0]
        self.assertEqual(int(row["rows"]), 2)
        self.assertAlmostEqual(float(row["share_of_file"]), 0.5)
        # both code rows are non-defaults: that is the shape of an artifact, not a count
        self.assertAlmostEqual(float(row["default_rate_with_code"]), 0.0)
        self.assertAlmostEqual(float(row["default_rate_without_code"]), 0.5)

    def test_codes_are_the_documented_pair(self):
        self.assertEqual(SENTINEL_VALUES, (96, 98))


class FeatureMathTests(unittest.TestCase):
    def test_delinquency_total_sums_the_three_buckets(self):
        built = build_features(
            one_row(
                serious_dlqin_2yrs=1,
                num_late_30_59=1.0,
                num_late_60_89=2.0,
                num_late_90=0.0,
            )
        )
        self.assertAlmostEqual(float(built["delinquency_events_total"].iloc[0]), 3.0)

    def test_delinquency_total_never_sums_a_code(self):
        built = build_features(
            one_row(
                serious_dlqin_2yrs=0,
                num_late_30_59=98.0,
                num_late_60_89=1.0,
                num_late_90=0.0,
            )
        )
        self.assertAlmostEqual(float(built["delinquency_events_total"].iloc[0]), 1.0)

    def test_an_all_missing_row_stays_missing(self):
        # min_count=1: absence of history is not the same as no history
        built = build_features(
            one_row(
                serious_dlqin_2yrs=0,
                num_late_30_59=None,
                num_late_60_89=None,
                num_late_90=None,
            )
        )
        self.assertTrue(math.isnan(float(built["delinquency_events_total"].iloc[0])))

    def test_utilization_excess_only_counts_the_part_above_the_limit(self):
        built = build_features(
            frame(
                [
                    {TARGET: 1, "revolving_utilization_unsecured": 1.5},
                    {TARGET: 0, "revolving_utilization_unsecured": 0.4},
                    {TARGET: 0, "revolving_utilization_unsecured": None},
                ]
            )
        )
        values = [built["utilization_over_limit"].iloc[i] for i in range(3)]
        self.assertAlmostEqual(float(values[0]), 0.5)
        self.assertEqual(float(values[1]), 0.0)
        self.assertTrue(math.isnan(float(values[2])))

    def test_income_is_shared_with_the_borrower_too(self):
        built = build_features(one_row(monthly_income=3000.0, num_dependents=2.0))
        self.assertAlmostEqual(float(built["income_per_dependent"].iloc[0]), 1000.0)

    def test_unrecorded_dependents_use_the_declared_constant(self):
        built = build_features(one_row(monthly_income=3000.0, num_dependents=None))
        self.assertAlmostEqual(float(built["income_per_dependent"].iloc[0]), 3000.0)

    def test_a_negative_dependent_count_yields_no_value(self):
        built = build_features(one_row(monthly_income=3000.0, num_dependents=-1.0))
        self.assertTrue(math.isnan(float(built["income_per_dependent"].iloc[0])))

    def test_overcommitted_is_strictly_past_the_ratio_of_one(self):
        built = build_features(
            frame([{TARGET: 1, "debt_ratio": v} for v in (1.2, 1.0, 0.5, None)])
        )
        values = [built["overcommitted"].iloc[i] for i in range(4)]
        self.assertEqual(
            [float(v) if not math.isnan(float(v)) else None for v in values],
            [1.0, 0.0, 0.0, None],
        )

    def test_exposure_total_adds_credit_lines_and_property_loans(self):
        built = build_features(
            one_row(num_open_credit_lines=3.0, num_real_estate_loans=2.0)
        )
        self.assertAlmostEqual(float(built["recent_exposure_total"].iloc[0]), 5.0)

    def test_missing_income_becomes_a_visible_indicator(self):
        built = build_features(
            frame(
                [
                    {TARGET: 1, "monthly_income": None},
                    {TARGET: 0, "monthly_income": 500.0},
                ]
            )
        )
        self.assertEqual(float(built["income_not_reported"].iloc[0]), 1.0)
        self.assertEqual(float(built["income_not_reported"].iloc[1]), 0.0)


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = one_row(
            serious_dlqin_2yrs=0,
            revolving_utilization_unsecured=0.5,
            age=45.0,
            num_late_30_59=0.0,
            debt_ratio=0.2,
            monthly_income=5000.0,
            num_open_credit_lines=7.0,
            num_late_90=0.0,
            num_real_estate_loans=1.0,
            num_late_60_89=0.0,
            num_dependents=2.0,
        )

    def test_every_feature_is_built_in_declaration_order(self):
        built = build_features(self.source)
        for name in FEATURE_NAMES:
            self.assertIn(name, built.columns)
        self.assertEqual(
            list(built.columns)[-len(FEATURE_NAMES) :], list(FEATURE_NAMES)
        )

    def test_building_twice_changes_nothing(self):
        once = build_features(self.source)
        twice = build_features(once)
        self.assertEqual(list(once.columns), list(twice.columns))
        for name in FEATURE_NAMES:
            self.assertEqual(float(once[name].iloc[0]), float(twice[name].iloc[0]))

    def test_the_label_survives_untouched(self):
        built = build_features(one_row(serious_dlqin_2yrs=1))
        self.assertEqual(int(built["serious_dlqin_2yrs"].iloc[0]), 1)

    def test_dropping_a_feature_returns_the_base_frame(self):
        built = build_features(self.source)
        ablated = drop_feature(built, "overcommitted")
        self.assertNotIn("overcommitted", ablated.columns)
        self.assertIn("delinquency_events_total", ablated.columns)

    def test_dropping_something_absent_is_an_error(self):
        with self.assertRaises(KeyError):
            drop_feature(self.source, "not_a_feature")

    def test_model_matrix_applies_the_ablation(self):
        built = build_features(self.source)
        full, _ = model_matrix(built, base=MODEL_FEATURE_COLUMNS)
        ablated, _ = model_matrix(
            built, base=MODEL_FEATURE_COLUMNS, drop=["overcommitted"]
        )
        self.assertEqual(len(ablated.columns), len(full.columns) - 1)
        self.assertNotIn("overcommitted", ablated.columns)

    def test_model_matrix_columns_are_base_plus_features(self):
        built = build_features(self.source)
        matrix, label = model_matrix(built, base=MODEL_FEATURE_COLUMNS)
        self.assertEqual(
            len(matrix.columns), len(MODEL_FEATURE_COLUMNS) + len(FEATURES)
        )
        self.assertNotIn("serious_dlqin_2yrs", matrix.columns)
        self.assertEqual(int(label.iloc[0]), 0)

    def test_model_matrix_rejects_an_unknown_ablation(self):
        built = build_features(self.source)
        with self.assertRaises(KeyError):
            model_matrix(built, base=MODEL_FEATURE_COLUMNS, drop=["made_up"])


class AuditTests(unittest.TestCase):
    def test_there_are_at_least_four_engineered_features(self):
        self.assertGreaterEqual(len(FEATURES), 4)

    def test_no_feature_admits_being_unavailable_at_decision_time(self):
        self.assertTrue(all(spec.decision_time_available for spec in FEATURES))

    def test_the_audit_table_renders_every_column_it_promises(self):
        table = audit_table()
        self.assertEqual(len(table), len(FEATURES))
        for column in (
            "feature",
            "built from",
            "hypothesis",
            "available before the decision",
            "why",
            "boundary that keeps it honest",
        ):
            self.assertIn(column, table.columns)

    def test_every_feature_states_what_would_have_made_it_leakage(self):
        self.assertTrue(all(spec.leakage_boundary for spec in FEATURES))

    def test_the_gated_feature_names_the_measurement_it_waits_for(self):
        self.assertEqual(
            GATED_FEATURES, {"income_not_reported": "missing_income_may_be_informative"}
        )

    def test_feature_names_are_unique_and_not_raw_columns(self):
        self.assertEqual(len(set(FEATURE_NAMES)), len(FEATURE_NAMES))
        for name in FEATURE_NAMES:
            self.assertNotIn(name, MODEL_FEATURE_COLUMNS)


if __name__ == "__main__":
    unittest.main()
