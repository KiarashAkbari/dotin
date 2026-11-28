"""The scoring path: a fresh, unlabeled file goes in, scores and flags come out.

Spec section 9 asks for a script that takes a new file with the same schema and
returns predictions, and for a test that the prediction path works on a sample
input. A fresh file has no outcome yet - Kaggle's ``cs-test.csv`` ships the label
column empty - so the main case here is the unlabeled one. The database part is
replaced by a frame shaped like ``credit.v_staging_model_input``; the live round
trip runs only when ``TEST_DATABASE_URL`` points at a database where 010/011 exist.
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
for candidate in (
    HERE,
    HERE.parent / "src",
    HERE.parents[1] / "stage-1-kiarash-akbari" / "src",
):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from data_factory.errors import QuarantineThresholdError  # noqa: E402

from credit import predict, prep, split  # noqa: E402
from credit.columns import (  # noqa: E402
    MODEL_FEATURE_COLUMNS,
    TARGET_COLUMN,
    VIEW_TARGET_COLUMN,
)
from credit.config import parse_credit_config  # noqa: E402
from credit.features import FEATURE_NAMES, build_features  # noqa: E402
from credit.load import collect_records  # noqa: E402
from credit.models import Bundle, build_bundle  # noqa: E402
from credit.validate import CreditReasonCode, parse_row  # noqa: E402
from fixtures import row, write_csv  # noqa: E402
from test_credit_models import SEED, synthetic_frame  # noqa: E402


def unlabeled_copy(source: Path, target: Path) -> Path:
    """The same file with the label column emptied, the way cs-test.csv ships."""
    with source.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    position = rows[0].index(TARGET_COLUMN)
    for line in rows[1:]:
        line[position] = ""
    with target.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    return target


def small_config(base: Path, csv_path: Path):
    return parse_credit_config(
        {
            "csv_path": str(csv_path),
            "sql_migrations_dir": str(base / "sql"),
            "curated_dir": str(base / "curated"),
            "raw_pages_dir": str(base / "pages"),
            "quarantine_dir": str(base / "quarantine"),
            "quarantine_min_rows": 10,
        },
        base=base,
    )


def small_bundle() -> Bundle:
    """A logistic bundle fitted on synthetic rows, threshold fixed at 0.2."""
    frame = build_features(synthetic_frame(rows=300))
    splits = split.stratified_split(frame, seed=SEED)
    columns = tuple(MODEL_FEATURE_COLUMNS) + FEATURE_NAMES
    stats = prep.fit(splits.train, plan=prep.PLANS["median+indicator"], columns=columns)
    X = prep.apply(splits.train, stats)[list(stats.feature_names())]
    return build_bundle(
        "logistic",
        X,
        prep.target(splits.train),
        prep_stats=stats,
        threshold=0.2,
        seed=SEED,
        code_version="test",
    )


class LabelOptionalParsingTests(unittest.TestCase):
    def test_a_blank_label_is_a_defect_when_training(self):
        parsed = parse_row(row(Id="1", SeriousDlqin2yrs=""))
        self.assertEqual(parsed.reasons, (CreditReasonCode.MISSING_REQUIRED_FIELD,))

    def test_a_blank_label_is_normal_when_scoring(self):
        parsed = parse_row(row(Id="1", SeriousDlqin2yrs=""), label_required=False)
        self.assertTrue(parsed.is_valid)
        self.assertIsNone(parsed.record[VIEW_TARGET_COLUMN])

    def test_a_present_label_is_still_checked_when_scoring(self):
        parsed = parse_row(row(Id="1", SeriousDlqin2yrs="2"), label_required=False)
        self.assertEqual(parsed.reasons, (CreditReasonCode.VALUE_OUT_OF_DOMAIN,))

    def test_the_id_stays_required_when_scoring(self):
        parsed = parse_row(row(Id="", SeriousDlqin2yrs=""), label_required=False)
        self.assertEqual(parsed.reasons, (CreditReasonCode.MISSING_REQUIRED_FIELD,))


class UnlabeledFileTests(unittest.TestCase):
    def test_an_unlabeled_file_is_quarantined_by_the_training_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fresh = unlabeled_copy(
                write_csv(base / "labeled.csv", rows=60), base / "fresh.csv"
            )
            with self.assertRaises(QuarantineThresholdError):
                collect_records(small_config(base, fresh))

    def test_an_unlabeled_file_passes_the_scoring_path_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fresh = unlabeled_copy(
                write_csv(base / "labeled.csv", rows=60), base / "fresh.csv"
            )
            records, report, _fetch = collect_records(
                small_config(base, fresh), label_required=False
            )
        self.assertEqual(report.quarantined, 0)
        self.assertEqual(len(records), 60)
        self.assertTrue(all(r[VIEW_TARGET_COLUMN] is None for r in records))


class ScoreStagedTests(unittest.TestCase):
    """``score_staged`` on a frame shaped like the staging view, with a real bundle."""

    @classmethod
    def setUpClass(cls):
        cls.bundle = small_bundle()
        cls.fresh = synthetic_frame(rows=50, seed=SEED + 1)

    def score(self, frame: pd.DataFrame) -> pd.DataFrame:
        with mock.patch.object(predict, "load_frame", return_value=frame):
            return predict.score_staged(object(), self.bundle)

    def test_an_unlabeled_frame_is_scored_and_flagged_at_the_saved_threshold(self):
        fresh = self.fresh.assign(**{VIEW_TARGET_COLUMN: np.nan})
        scored = self.score(fresh)
        self.assertEqual(len(scored), len(fresh))
        self.assertEqual(scored["row_id"].tolist(), fresh["row_id"].tolist())
        self.assertTrue(scored["score"].between(0.0, 1.0).all())
        expected = (scored["score"] >= 0.2).astype("int64")
        self.assertEqual(scored["flag"].tolist(), expected.tolist())
        self.assertNotIn(VIEW_TARGET_COLUMN, scored.columns)
        self.assertEqual(predict.cost_of(scored), (None, None))

    def test_a_labeled_frame_also_reports_its_cost(self):
        scored = self.score(self.fresh)
        cost, counts = predict.cost_of(scored)
        self.assertIsNotNone(cost)
        self.assertEqual(sum(counts.values()), len(self.fresh))

    def test_a_saved_bundle_scores_the_same_after_reload(self):
        fresh = self.fresh.assign(**{VIEW_TARGET_COLUMN: np.nan})
        before = self.score(fresh)
        with tempfile.TemporaryDirectory() as tmp:
            self.bundle.save(Path(tmp))
            reloaded = Bundle.load(Path(tmp))
        with mock.patch.object(predict, "load_frame", return_value=fresh):
            after = predict.score_staged(object(), reloaded)
        np.testing.assert_array_equal(before["score"], after["score"])

    def test_an_empty_staging_table_is_an_error_not_an_empty_answer(self):
        from data_factory.errors import DataError

        with self.assertRaises(DataError):
            self.score(self.fresh.iloc[0:0])


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "needs TEST_DATABASE_URL")
class LiveScoringTests(unittest.TestCase):
    """Stage an unlabeled file into ``credit.staging_application`` and score it back."""

    def setUp(self):
        import sqlalchemy as sa

        self.engine = sa.create_engine(os.environ["TEST_DATABASE_URL"])
        with self.engine.connect() as connection:
            exists = connection.execute(
                sa.text("SELECT to_regclass('credit.v_staging_model_input')")
            ).scalar_one()
        if exists is None:
            self.engine.dispose()
            self.skipTest("apply sql/010 and sql/011 to the test database first")

    def tearDown(self):
        self.engine.dispose()

    def test_an_unlabeled_file_round_trips_through_the_staging_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fresh = unlabeled_copy(
                write_csv(base / "labeled.csv", rows=40), base / "fresh.csv"
            )
            result = predict.score_file(
                self.engine, small_config(base, fresh), fresh, small_bundle()
            )
        self.assertEqual(result.rows_staged, 40)
        self.assertEqual(result.rows_rejected, 0)
        self.assertEqual(len(result.scored), 40)
        self.assertIsNone(result.cost_per_1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
