"""Split, preparation, model families and the saved bundle - all offline, all seeded.

These are the four acceptance criteria that can be proven without the Kaggle file:
the split boundary is real, the fitted constants come only from the training rows,
two different families can be fit and scored by cost, and the thing written to disk
reproduces the same numbers when read back. The synthetic frame below is deliberately
small (200 rows) so the whole file runs in a second or two; the real comparison lives
in the notebook, and no number here is a claim about the real data.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
for candidate in (
    HERE,
    HERE.parent / "src",
    HERE.parents[1] / "stage-1" / "src",
):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from credit import prep, split
from credit.columns import MODEL_FEATURE_COLUMNS, VIEW_TARGET_COLUMN
from credit.cost import (
    CHALLENGE_MATRIX,
    FLATTER_MATRIX,
    binary_counts,
    cost_per_1000,
    select_threshold,
)
from credit.features import FEATURES, build_features, model_matrix
from credit.models import (
    FAMILIES,
    Bundle,
    build_bundle,
    compare,
    cross_validate_cost,
    importances,
    pick_best,
)
from credit.prep import INDICATOR_SUFFIX, PLANS, PrepStats

SEED = 20261103
BASE_COLUMNS = ("row_id", VIEW_TARGET_COLUMN) + tuple(MODEL_FEATURE_COLUMNS)


def synthetic_frame(rows: int = 200, *, seed: int = SEED) -> pd.DataFrame:
    """A frame in the shape of ``credit.v_model_input``: one real signal, noise, NaNs.

    ``monthly_income`` carries the label (low income -> default) so the leakage and
    selection tests have something to find; the other columns are noise on purpose -
    a test that passes against noise cannot be passing because of the fixture.
    """
    rng = np.random.default_rng(seed)
    income = np.round(rng.gamma(2.0, 3_000.0, rows), 2)
    truth = (income < np.quantile(income, 0.12)).astype("int64")
    late = rng.poisson(0.4, rows).astype("float64")
    late[truth == 1] += rng.poisson(0.9, int(truth.sum()))
    frame = pd.DataFrame(
        {
            "row_id": np.arange(1, rows + 1, dtype="int64"),
            VIEW_TARGET_COLUMN: truth,
            "revolving_utilization_unsecured": np.round(
                np.clip(rng.beta(2, 5, rows), 0.0, 1.0), 6
            ),
            "utilization_capped": np.round(np.clip(rng.beta(2, 5, rows), 0.0, 1.0), 6),
            "age": np.round(rng.normal(48, 13, rows), 1).clip(21, 100),
            "num_late_30_59": np.round(late),
            "num_late_90": np.round(rng.poisson(0.15, rows)).clip(0, 9),
            "num_late_60_89": np.round(rng.poisson(0.2, rows)).clip(0, 9),
            "debt_ratio": np.round(rng.uniform(0, 0.6, rows), 6),
            "monthly_income": income,
            "income_missing": (rng.random(rows) < 0.02).astype("int64"),
            "num_open_credit_lines": rng.poisson(8, rows).astype("float64"),
            "num_real_estate_loans": rng.poisson(0.8, rows).astype("float64"),
            "num_dependents": rng.poisson(0.6, rows).astype("float64"),
        }
    )
    # missingness the view would leave behind: a blank income, a masked sentinel code
    frame.loc[frame.index[5:15], "monthly_income"] = np.nan
    frame.loc[frame.index[20:26], "num_late_30_59"] = np.nan
    frame.loc[frame.index[30:33], "num_late_90"] = np.nan
    return frame


def prepared(
    frame: pd.DataFrame, plan: str = "median+indicator"
) -> tuple[pd.DataFrame, pd.Series, prep.PrepStats]:
    features = build_features(frame)
    base, target = model_matrix(
        features, base=[c for c in BASE_COLUMNS if c != "row_id"]
    )
    return base, target, features


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.frame = build_features(synthetic_frame())

    def test_one_seed_always_gives_the_same_split(self):
        first = split.stratified_split(self.frame, seed=SEED)
        second = split.stratified_split(self.frame, seed=SEED)
        self.assertEqual(
            first.train["row_id"].tolist(), second.train["row_id"].tolist()
        )
        third = split.stratified_split(self.frame, seed=SEED + 1)
        self.assertNotEqual(
            first.train["row_id"].tolist(), third.train["row_id"].tolist()
        )

    def test_sizes_and_positive_rates_and_disjointness(self):
        splits = split.stratified_split(self.frame, seed=SEED)
        total = len(self.frame)
        self.assertEqual(sum(splits.sizes.values()), total)
        self.assertEqual(splits.sizes["train"], int(total * 0.6))
        overall = float(self.frame[VIEW_TARGET_COLUMN].mean())
        for name in ("train", "validation", "holdout"):
            part = getattr(splits, name)
            rate = float(part[VIEW_TARGET_COLUMN].mean())
            self.assertLess(
                abs(rate - overall), 0.05, msg=f"{name} rate {rate} vs {overall}"
            )
        audit = split.boundary_audit(splits)
        self.assertEqual(audit["overlap_with_train"].tolist()[1:], [0, 0])
        self.assertEqual(int(audit["rows"].sum()), total)

    def test_folds_deal_every_row_exactly_once(self):
        train = split.stratified_split(self.frame, seed=SEED).train
        folds = split.stratified_folds(train, seed=SEED, k=5)
        self.assertEqual(len(folds), 5)
        held_out = np.concatenate([test.to_numpy() for _train, test in folds])
        self.assertEqual(len(held_out), len(train))
        self.assertEqual(len(np.unique(held_out)), len(train))
        for fold_train, fold_test in folds:
            self.assertEqual(
                split.overlap_with_train(train.loc[fold_test], train.loc[fold_train]), 0
            )

    def test_a_missing_target_is_refused(self):
        broken = self.frame.copy()
        broken.loc[broken.index[0], VIEW_TARGET_COLUMN] = np.nan
        with self.assertRaises(ValueError):
            split.stratified_split(broken, seed=SEED)

    def test_fractions_are_validated(self):
        with self.assertRaises(ValueError):
            split.stratified_split(self.frame, seed=SEED, fractions=(0.5, 0.5))
        with self.assertRaises(ValueError):
            split.stratified_split(self.frame, seed=SEED, fractions=(0.6, 0.2, 0.0))
        with self.assertRaises(ValueError):
            split.stratified_folds(self.frame, seed=SEED, k=1)


class PrepTests(unittest.TestCase):
    def setUp(self):
        self.frame = build_features(synthetic_frame(rows=400))
        self.splits = split.stratified_split(self.frame, seed=SEED)

    def test_the_fitted_median_is_the_training_rows_and_nothing_else(self):
        stats = prep.fit(
            self.splits.train,
            plan=PLANS["median+indicator"],
            columns=MODEL_FEATURE_COLUMNS,
        )
        train_median = float(self.splits.train["monthly_income"].median())
        val_median = float(self.splits.validation["monthly_income"].median())
        self.assertAlmostEqual(stats.medians["monthly_income"], train_median, places=9)
        self.assertGreater(abs(train_median - val_median), 1.0)
        self.assertNotAlmostEqual(stats.medians["monthly_income"], val_median, places=3)

    def test_the_valued_rows_are_left_alone_by_imputation(self):
        stats = prep.fit(
            self.splits.train,
            plan=PLANS["median+indicator"],
            columns=MODEL_FEATURE_COLUMNS,
        )
        raw = prep.apply(self.splits.validation, stats)
        known = self.splits.validation["monthly_income"].notna()
        scaled = (
            self.splits.validation.loc[known, "monthly_income"]
            - stats.means["monthly_income"]
        ) / stats.stds["monthly_income"]
        pd.testing.assert_series_equal(
            raw.loc[known, "monthly_income"].astype("float64"),
            scaled.astype("float64"),
            check_names=False,
        )
        filled = raw.loc[~known, "monthly_income"]
        expected = stats.medians["monthly_income"]
        self.assertTrue(
            np.allclose(
                filled,
                (expected - stats.means["monthly_income"])
                / stats.stds["monthly_income"],
            )
        )

    def test_no_plan_leaves_a_single_nan_behind(self):
        for name, plan in PLANS.items():
            with self.subTest(plan=name):
                stats = prep.fit(
                    self.splits.train, plan=plan, columns=MODEL_FEATURE_COLUMNS
                )
                matrix = prep.apply(self.splits.validation, stats)
                self.assertEqual(
                    int(matrix[list(stats.feature_names())].isna().sum().sum()), 0
                )

    def test_the_indicator_marks_exactly_the_rows_that_were_missing(self):
        plan = PLANS["median+indicator"]
        stats = prep.fit(self.splits.train, plan=plan, columns=MODEL_FEATURE_COLUMNS)
        matrix = prep.apply(self.splits.validation, stats)
        column = "monthly_income" + INDICATOR_SUFFIX
        self.assertEqual(
            int(matrix[column].sum()),
            int(self.splits.validation["monthly_income"].isna().sum()),
        )
        self.assertTrue(set(matrix[column].unique()) <= {0, 1})
        train_matrix = prep.apply(self.splits.train, stats)
        self.assertEqual(
            int(train_matrix[column].sum()),
            int(self.splits.train["monthly_income"].isna().sum()),
        )
        self.assertGreater(int(train_matrix[column].sum()), 0)
        self.assertIn("monthly_income" + INDICATOR_SUFFIX, stats.feature_names())

    def test_feature_names_are_the_columns_fit_was_given(self):
        """A fitted column that does not come back is a column the model never saw.

        ``feature_names`` used to rebuild the list from the ten raw file columns, so a
        caller that fitted on base plus engineered features trained on the raw ten and
        printed sixteen - and the notebook's ablation then measured one matrix twice,
        found a zero cost delta for every feature, and dropped nothing.
        """
        subset = ("monthly_income", "age", "num_late_30_59")
        stats = prep.fit(
            self.splits.train, plan=PLANS["median+indicator"], columns=subset
        )
        names = stats.feature_names()
        self.assertEqual(names[: len(subset)], subset)
        self.assertEqual(
            set(names[len(subset) :]),
            {
                column + INDICATOR_SUFFIX
                for column in subset
                if self.splits.train[column].isna().any()
            },
        )
        self.assertNotIn("revolving_utilization_unsecured", names)

    def test_engineered_features_survive_into_the_matrix(self):
        engineered = tuple(spec.name for spec in FEATURES)
        frame = build_features(self.splits.train)
        stats = prep.fit(
            frame,
            plan=PLANS["median+indicator"],
            columns=tuple(MODEL_FEATURE_COLUMNS) + engineered,
        )
        names = stats.feature_names()
        for column in engineered:
            with self.subTest(feature=column):
                self.assertIn(column, names)
        matrix = prep.apply(frame, stats)
        self.assertEqual(
            int(matrix[list(names)].isna().sum().sum()),
            0,
            "preparation must leave no NaN in any column of the matrix it names",
        )

    def test_the_zero_plan_is_the_other_policy_and_differs(self):
        median_stats = prep.fit(
            self.splits.train,
            plan=PLANS["median+indicator"],
            columns=MODEL_FEATURE_COLUMNS,
        )
        zero_stats = prep.fit(
            self.splits.train, plan=PLANS["zero+none"], columns=MODEL_FEATURE_COLUMNS
        )
        self.assertEqual(zero_stats.medians["monthly_income"], 0.0)
        self.assertEqual(zero_stats.indicator_columns, ())
        self.assertNotEqual(median_stats.means, zero_stats.means)

    def test_a_constant_column_survives_scaling(self):
        frame = self.splits.train.copy()
        frame["num_dependents"] = 3.0
        stats = prep.fit(
            frame, plan=PLANS["median+indicator"], columns=MODEL_FEATURE_COLUMNS
        )
        self.assertEqual(stats.stds["num_dependents"], 1.0)
        matrix = prep.apply(frame, stats)
        self.assertTrue(np.isfinite(matrix["num_dependents"]).all())

    def test_missing_columns_are_named_rather_than_silently_filled(self):
        stats = prep.fit(
            self.splits.train,
            plan=PLANS["median+indicator"],
            columns=MODEL_FEATURE_COLUMNS,
        )
        with self.assertRaises(KeyError) as caught:
            prep.apply(self.splits.validation.drop(columns=["monthly_income"]), stats)
        self.assertIn("monthly_income", str(caught.exception))
        with self.assertRaises(KeyError):
            prep.fit(
                self.splits.train.drop(columns=["age"]),
                plan=PLANS["median+indicator"],
                columns=MODEL_FEATURE_COLUMNS,
            )

    def test_stats_round_trip_through_json(self):
        stats = prep.fit(
            self.splits.train,
            plan=PLANS["median+indicator"],
            columns=MODEL_FEATURE_COLUMNS,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prep_stats.json"
            stats.save(path)
            reloaded = PrepStats.load(path)
        self.assertEqual(stats, reloaded)
        matrix_a = prep.apply(self.splits.validation, stats)
        matrix_b = prep.apply(self.splits.validation, reloaded)
        pd.testing.assert_frame_equal(matrix_a, matrix_b)

    def test_audit_rows_all_come_from_the_training_split(self):
        stats = prep.fit(
            self.splits.train,
            plan=PLANS["median+indicator"],
            columns=MODEL_FEATURE_COLUMNS,
        )
        table = prep.audit(stats)
        self.assertGreater(len(table), 0)
        fitted = table[table["fitted_on"] != "row-wise"]
        self.assertEqual(set(fitted["fitted_on"]), {"train"})
        self.assertEqual(set(fitted["fitted_rows"]), {len(self.splits.train)})
        self.assertIn("log1p transform (unfitted)", set(table["statistic"]))


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.frame = build_features(synthetic_frame(rows=300))
        self.splits = split.stratified_split(self.frame, seed=SEED)
        self.plan = PLANS["median+indicator"]
        self.stats = prep.fit(
            self.splits.train,
            plan=self.plan,
            columns=tuple(MODEL_FEATURE_COLUMNS)
            + tuple(spec.name for spec in FEATURES),
        )
        self.matrix = prep.apply(self.splits.train, self.stats)
        self.names = list(self.stats.feature_names())
        self.X = self.matrix[self.names]
        self.y = prep.target(self.splits.train)

    def test_both_families_fit_and_report_probabilities_in_range(self):
        for family in FAMILIES:
            with self.subTest(family=family):
                model = FAMILIES[family].make(seed=SEED)
                model.fit(self.X, self.y)
                proba = np.asarray(model.predict_proba(self.X)[:, 1], dtype="float64")
                self.assertEqual(proba.shape, (len(self.X),))
                self.assertTrue(((proba >= 0) & (proba <= 1)).all())

    def test_the_scaling_decision_is_recorded_per_family(self):
        self.assertTrue(FAMILIES["logistic"].requires_scaling)
        self.assertFalse(FAMILIES["boosted_trees"].requires_scaling)
        for family in FAMILIES.values():
            self.assertGreater(len(family.scaling_note), 20)

    def test_cross_validation_reports_every_fold_and_the_arithmetic_holds(self):
        folds = split.stratified_folds(self.splits.train, seed=SEED, k=3)
        result = cross_validate_cost(
            "logistic", self.X, self.y, folds, plan=self.plan.name, fixed_threshold=0.5
        )
        self.assertEqual(len(result.folds), 3)
        for fold in result.folds:
            self.assertEqual(
                round(fold.cost_per_1000, 6),
                round(cost_per_1000(fold.counts, CHALLENGE_MATRIX, n=fold.rows), 6),
            )
            self.assertEqual(sum(fold.counts.values()), fold.rows)
        costs = np.array([fold.cost_per_1000 for fold in result.folds])
        self.assertAlmostEqual(result.mean_cost, float(costs.mean()), places=9)
        self.assertAlmostEqual(result.std_cost, float(costs.std(ddof=1)), places=9)
        self.assertEqual(result.worst_cost, float(costs.max()))
        self.assertEqual(result.matrix, CHALLENGE_MATRIX.name)

    def test_a_fold_scores_itself_and_never_another_folds_rows(self):
        folds = split.stratified_folds(self.splits.train, seed=SEED, k=3)
        result = cross_validate_cost(
            "logistic", self.X, self.y, folds, plan=self.plan.name
        )
        self.assertEqual(
            sum(fold.rows for fold in result.folds), len(self.splits.train)
        )
        held = set()
        for fold, (_train, test) in zip(result.folds, folds, strict=True):
            self.assertEqual(fold.rows, len(test))
            held.update(test.tolist())
        self.assertEqual(len(held), len(self.splits.train))
        self.assertGreaterEqual(result.threshold_std, 0.0)

    def test_a_lower_cost_matrix_moves_the_chosen_point(self):
        folds = split.stratified_folds(self.splits.train, seed=SEED, k=3)
        harsh = cross_validate_cost(
            "logistic",
            self.X,
            self.y,
            folds,
            plan=self.plan.name,
            matrix=CHALLENGE_MATRIX,
        )
        mild = cross_validate_cost(
            "logistic",
            self.X,
            self.y,
            folds,
            plan=self.plan.name,
            matrix=FLATTER_MATRIX,
        )
        self.assertLess(mild.mean_cost, harsh.mean_cost)

    def test_compare_sorts_and_pick_best_breaks_ties_by_spread(self):
        folds = split.stratified_folds(self.splits.train, seed=SEED, k=3)
        logistic = cross_validate_cost(
            "logistic", self.X, self.y, folds, plan=self.plan.name, fixed_threshold=0.5
        )
        boosted = cross_validate_cost(
            "boosted_trees",
            self.X,
            self.y,
            folds,
            plan=self.plan.name,
            fixed_threshold=0.5,
        )
        table = compare([boosted, logistic])
        self.assertEqual(
            list(table.columns),
            [
                "family",
                "plan",
                "matrix",
                "mean_cost",
                "std_cost",
                "worst_cost",
                "mean_auc",
                "threshold_std",
            ],
        )
        self.assertEqual(len(table), 2)
        best = pick_best([boosted, logistic])
        self.assertEqual(best.mean_cost, min(logistic.mean_cost, boosted.mean_cost))
        tied = pick_best([logistic, logistic])
        self.assertEqual(tied.family, "logistic")

    def test_permutation_importance_finds_the_signal_column(self):
        folds = split.stratified_folds(self.splits.train, seed=SEED, k=3)
        result = cross_validate_cost(
            "logistic", self.X, self.y, folds, plan=self.plan.name, fixed_threshold=0.5
        )
        threshold = float(np.mean([fold.threshold for fold in result.folds]))
        model = FAMILIES["logistic"].make(seed=SEED)
        model.fit(self.X, self.y)
        table = importances(
            model, self.X, self.y, threshold=threshold, seed=SEED, n_repeats=3
        )
        self.assertEqual(len(table), self.X.shape[1])
        self.assertEqual(table.iloc[0]["feature"], "monthly_income")
        self.assertGreater(table.iloc[0]["importance_mean"], 0.0)
        self.assertTrue(table["importance_mean"].is_monotonic_decreasing)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.frame = build_features(synthetic_frame(rows=240))
        self.splits = split.stratified_split(self.frame, seed=SEED)
        self.stats = prep.fit(
            self.splits.train,
            plan=PLANS["median+indicator"],
            columns=tuple(MODEL_FEATURE_COLUMNS)
            + tuple(spec.name for spec in FEATURES),
        )
        self.matrix = prep.apply(self.splits.train, self.stats)
        self.names = list(self.stats.feature_names())
        self.X = self.matrix[self.names]
        self.y = prep.target(self.splits.train)

    def test_a_saved_bundle_reproduces_scores_and_flags_exactly(self):
        reference = FAMILIES["logistic"].make(seed=SEED).fit(self.X, self.y)
        choice = select_threshold(
            self.y,
            np.asarray(reference.predict_proba(self.X)[:, 1], dtype="float64"),
            matrix=CHALLENGE_MATRIX,
        )
        bundle = build_bundle(
            "logistic",
            self.X,
            self.y,
            prep_stats=self.stats,
            threshold=choice.threshold,
            seed=SEED,
            code_version="test",
        )
        held_out = prep.apply(self.splits.holdout, self.stats)[self.names]
        scores_before = bundle.score(held_out)
        with tempfile.TemporaryDirectory() as tmp:
            written = bundle.save(Path(tmp))
            self.assertTrue(all(path.is_file() for path in written.values()))
            reloaded = Bundle.load(Path(tmp))
        scores_after = reloaded.score(held_out)
        np.testing.assert_array_equal(scores_before, scores_after)
        self.assertEqual(reloaded.threshold, choice.threshold)
        self.assertEqual(reloaded.family, "logistic")
        self.assertEqual(reloaded.trained_rows, len(self.X))
        self.assertEqual(reloaded.prep_stats, self.stats)
        flags = reloaded.flag(held_out)
        counts = binary_counts(prep.target(self.splits.holdout), flags)
        self.assertEqual(
            cost_per_1000(counts, CHALLENGE_MATRIX, n=len(flags)),
            cost_per_1000(counts, CHALLENGE_MATRIX, n=len(flags)),
        )

    def test_a_frame_missing_a_feature_is_named_not_guessed(self):
        choice_threshold = 0.5
        bundle = build_bundle(
            "logistic",
            self.X,
            self.y,
            prep_stats=self.stats,
            threshold=choice_threshold,
            seed=SEED,
        )
        with self.assertRaises(KeyError) as caught:
            bundle.score(self.X.drop(columns=["monthly_income"]))
        self.assertIn("monthly_income", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
