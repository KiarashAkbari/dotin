"""The model layer that runs (capstone.system): time split, column policy, rule, roster, metrics.

Offline: the frame is built in memory with the columns v_feature_frame really has, NULLs and text
included -- the exact shape that made the old ``train()`` refuse (review B2).
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import date
from unittest import mock

import helpers  # noqa: F401

try:  # the model layer needs the pinned scientific stack; the suite says so instead of erroring
    import numpy as np
    import pandas as pd

    from capstone import system
    from capstone.config import load_config
    from capstone.errors import InputError

    HAVE_STACK = True
except ModuleNotFoundError:  # pragma: no cover
    HAVE_STACK = False


def synthetic_frame(rows: int = 3000, seed: int = 7):
    """A frame with v_feature_frame's kinds of columns and a label that depends on two of them."""
    rng = np.random.default_rng(seed)
    approved = pd.Timestamp("2017-01-01") + pd.to_timedelta(rng.integers(0, 600, rows), unit="D")
    n_items = rng.integers(1, 5, rows)
    freight = rng.random(rows)
    state = rng.choice(["SP", "RJ", "MG", "BA", None], rows, p=[0.4, 0.2, 0.2, 0.15, 0.05])
    risk = 0.08 + 0.08 * (n_items > 2) + 0.1 * freight + 0.05 * (state == "BA")
    label = (rng.random(rows) < risk).astype(float)
    label[rng.random(rows) < 0.02] = np.nan  # D2: no review
    days_since = np.where(rng.random(rows) < 0.9, np.nan, rng.integers(1, 300, rows))
    frame = pd.DataFrame(
        {
            "order_id": [f"o{i:06d}" for i in range(rows)],
            "customer_id": [f"c{i:06d}" for i in range(rows)],
            "order_approved_at": approved,
            "n_items": n_items.astype(float),
            "freight_share": freight,
            "days_since_prior_order": days_since,
            "promise_days": rng.integers(5, 40, rows).astype(float),
            "pays_voucher": (rng.random(rows) < 0.1).astype(float),
            "customer_state": pd.Series(state, dtype=object),
            "seller_state": pd.Series(rng.choice(["SP", "PR"], rows), dtype=object),
            "product_category_name_english": pd.Series(
                rng.choice(["toys", "bed_bath_table", None], rows), dtype=object
            ),
            "is_negative": label,
            "has_review": (~np.isnan(label)).astype(float),
        }
    )
    frame["decision_month"] = frame["order_approved_at"].dt.strftime("%Y-%m")
    return frame.sort_values(["order_approved_at", "order_id"]).reset_index(drop=True)


@unittest.skipUnless(HAVE_STACK, "numpy/pandas/scikit-learn are not installed")
class TimeSplitTests(unittest.TestCase):
    def test_the_boundary_day_is_training_and_the_next_day_is_evaluation(self) -> None:
        frame = synthetic_frame()
        split = system.time_split(frame, date(2018, 2, 28), 3)
        approved = frame["order_approved_at"]
        self.assertTrue((approved[split.train] < pd.Timestamp("2018-03-01")).all())
        self.assertTrue((approved[split.evaluation] >= pd.Timestamp("2018-03-01")).all())
        self.assertEqual(int(split.train.sum() + split.evaluation.sum()), len(frame))

    def test_validation_is_the_last_months_of_training_and_never_evaluation(self) -> None:
        frame = synthetic_frame()
        split = system.time_split(frame, date(2018, 2, 28), 3)
        self.assertEqual(split.validation_start, date(2017, 12, 1))
        self.assertFalse((split.validation & split.evaluation).any())
        self.assertFalse((split.fit & split.validation).any())
        self.assertTrue((split.validation <= split.train).all())

    def test_month_arithmetic_crosses_the_year(self) -> None:
        self.assertEqual(system.month_start_back(date(2018, 5, 31), 3), date(2018, 3, 1))
        self.assertEqual(system.month_start_back(date(2018, 1, 15), 2), date(2017, 12, 1))

    def test_a_window_without_negatives_is_refused(self) -> None:
        frame = synthetic_frame()
        frame.loc[frame["order_approved_at"] > pd.Timestamp("2018-02-28"), "is_negative"] = 0.0
        with self.assertRaises(InputError):
            system.time_split(frame, date(2018, 2, 28), 3)


@unittest.skipUnless(HAVE_STACK, "numpy/pandas/scikit-learn are not installed")
class ColumnPolicyTests(unittest.TestCase):
    def test_text_and_nulls_are_accepted_by_both_families(self) -> None:
        frame = synthetic_frame()
        numeric, categorical = system.feature_columns(frame, external=False)
        self.assertEqual(categorical, list(system.CATEGORICAL))
        self.assertNotIn("is_negative", numeric)
        self.assertNotIn("order_id", numeric)
        for name in ("logreg.c0_1", "hgb.d3"):
            _, scores = system.fit_score(name, frame, frame, numeric, categorical, 1)
            self.assertEqual(len(scores), len(frame))
            self.assertFalse(np.isnan(scores).any())

    def test_a_category_never_seen_in_training_does_not_crash_scoring(self) -> None:
        frame = synthetic_frame()
        numeric, categorical = system.feature_columns(frame, external=False)
        fresh = frame.head(5).copy()
        fresh["customer_state"] = "AC"
        pipeline, _ = system.fit_score("hgb.d3", frame, frame, numeric, categorical, 1)
        self.assertEqual(len(pipeline.predict_proba(fresh[numeric + categorical])), 5)

    def test_the_external_column_is_only_used_when_asked(self) -> None:
        frame = synthetic_frame().assign(external_density_2010=50.0)
        self.assertNotIn(system.EXTERNAL_COLUMN, system.feature_columns(frame, external=False)[0])
        self.assertIn(system.EXTERNAL_COLUMN, system.feature_columns(frame, external=True)[0])
        with self.assertRaises(InputError):
            system.feature_columns(synthetic_frame(), external=True)

    def test_an_unknown_family_is_refused_by_name(self) -> None:
        with self.assertRaises(InputError):
            system.make_estimator("forest.big", 0)
        with self.assertRaises(InputError):
            system.make_estimator("hgb.d99", 0)


@unittest.skipUnless(HAVE_STACK, "numpy/pandas/scikit-learn are not installed")
class MetricTests(unittest.TestCase):
    def test_evaluate_reports_the_brief_s_numbers(self) -> None:
        frame = synthetic_frame()
        scores = frame["n_items"].to_numpy() + frame["freight_share"].to_numpy()
        out = system.evaluate(frame, scores, share=0.1, shares=[0.05, 0.1, 0.2])
        self.assertEqual([row["share"] for row in out["capacity"]], [0.05, 0.1, 0.2])
        point = out["operating_point"]
        self.assertEqual(point["tp"] + point["fp"] + point["fn"] + point["tn"], out["labelled"])
        self.assertGreater(out["auc"], 0.5)
        self.assertEqual(sum(row["labelled"] for row in out["months"]), out["labelled"])
        self.assertEqual(out["rows"], len(frame))

    def test_the_rule_is_chosen_on_validation_rows_only(self) -> None:
        frame = synthetic_frame()
        split = system.time_split(frame, date(2018, 2, 28), 3)
        seen: list[int] = []
        real = system.recall_at

        def spy(rows, scores, share):
            seen.append(len(rows))
            return real(rows, scores, share)

        with mock.patch.object(system, "recall_at", side_effect=spy):
            system.choose_rule(frame, split, 0.1)
        self.assertTrue(seen)
        self.assertEqual(set(seen), {int(split.validation.sum())})

    def test_psi_is_zero_on_identical_samples_and_grows_with_a_shift(self) -> None:
        values = np.arange(1000, dtype=float)
        self.assertAlmostEqual(system.psi(values, values), 0.0, places=6)
        self.assertGreater(system.psi(values, values + 500), 0.25)
        self.assertAlmostEqual(system.category_psi(["a", "b"] * 5, ["a", "b"] * 5), 0.0)


@unittest.skipUnless(HAVE_STACK, "numpy/pandas/scikit-learn are not installed")
class ExperimentTests(unittest.TestCase):
    def config(self):
        return load_config(helpers.DEFAULT_CONFIG)

    def test_selection_never_sees_the_evaluation_months(self) -> None:
        frame = synthetic_frame()
        config = self.config()
        boundary = date(2018, 2, 28)
        fitted_windows: list[tuple[str, str]] = []
        real = system.fit_score

        def spy(name, fit_rows, score_rows, *args):
            fitted_windows.append(
                (str(fit_rows["order_approved_at"].max()), str(score_rows["order_approved_at"].min()))
            )
            return real(name, fit_rows, score_rows, *args)

        config = dataclasses.replace(config, split=dataclasses.replace(config.split, boundary=boundary))
        with mock.patch.object(system, "fit_score", side_effect=spy):
            result = system.experiment(frame, config, with_drift=False)
        cut = pd.Timestamp("2018-03-01")
        for last_fit, _ in fitted_windows:
            self.assertLess(pd.Timestamp(last_fit), cut, "no model is ever fitted on evaluation rows")
        self.assertIn(result["chosen_model"], [row["model"] for row in result["selection"]])
        families = {row["family"] for row in result["selection"]}
        self.assertGreaterEqual(len(families), 2)
        self.assertEqual(result["split"]["boundary"], "2018-02-28")


if __name__ == "__main__":
    unittest.main()


class FeatureHypothesisTableTest(unittest.TestCase):
    """`04` asks for one hypothesis per feature: the table must name exactly what the model reads."""

    def test_every_model_feature_has_one_hypothesis_row(self) -> None:
        import csv
        import json
        from pathlib import Path

        stage = Path(__file__).resolve().parents[1]
        with (stage / "docs" / "feature_hypotheses.csv").open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        named = [row["feature"] for row in rows]
        self.assertEqual(len(named), len(set(named)), "a feature listed twice")
        for row in rows:
            self.assertTrue(row["hypothesis"].strip(), f"{row['feature']} has no hypothesis")
        run = json.loads((stage / "docs" / "evidence" / "model-run.json").read_text(encoding="utf-8"))
        used = set(run["features"]["numeric"]) | set(run["features"]["categorical"])
        self.assertEqual(set(named), used | {"external_density_2010"})
