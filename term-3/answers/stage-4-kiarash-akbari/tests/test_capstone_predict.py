"""The prediction path's gate: the fresh-order bundle, its schema, and its refusals (offline)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401

from capstone import predict
from capstone.config import load_config
from capstone.errors import InputError, NotReadyError


def write_bundle(folder: Path, config, *, skip: tuple[str, ...] = (), break_table: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    from data_factory.database import COLUMN_ALIASES

    for table, name in {**predict.BUNDLE_REQUIRED, **predict.BUNDLE_OPTIONAL}.items():
        if table in skip:
            continue
        columns = [COLUMN_ALIASES.get(c, c) for c in predict.expected_columns(config, table)]
        if table == break_table:
            columns = columns[:-1] + ["surprise"]
        (folder / name).write_text(",".join(columns) + "\n", encoding="utf-8")
    return folder


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(helpers.DEFAULT_CONFIG)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_folder_and_its_orders_file_name_the_same_bundle(self) -> None:
        folder = write_bundle(self.root / "b", self.config)
        by_folder = predict.bundle_files(folder)
        by_file = predict.bundle_files(folder / predict.BUNDLE_REQUIRED["orders"])
        self.assertEqual(by_folder, by_file)
        self.assertIn("products", by_folder)

    def test_a_missing_required_file_is_named(self) -> None:
        folder = write_bundle(self.root / "b", self.config, skip=("payments",))
        with self.assertRaises(InputError) as caught:
            predict.bundle_files(folder)
        self.assertIn("olist_order_payments_dataset.csv", str(caught.exception))

    def test_products_and_sellers_are_optional(self) -> None:
        folder = write_bundle(self.root / "b", self.config, skip=("products", "sellers"))
        self.assertNotIn("products", predict.bundle_files(folder))

    def test_the_kaggle_spelling_is_accepted_because_stage_1_accepts_it(self) -> None:
        folder = write_bundle(self.root / "b", self.config)
        header = (folder / "olist_products_dataset.csv").read_text(encoding="utf-8")
        self.assertIn("lenght", header)
        predict.validate_bundle(predict.bundle_files(folder), self.config)

    def test_a_wrong_column_is_refused_by_file_and_column(self) -> None:
        folder = write_bundle(self.root / "b", self.config, break_table="order_items")
        with self.assertRaises(InputError) as caught:
            predict.validate_bundle(predict.bundle_files(folder), self.config)
        message = str(caught.exception)
        self.assertIn("olist_order_items_dataset.csv", message)
        self.assertIn("surprise", message)

    def test_reviews_in_a_bundle_are_never_read(self) -> None:
        folder = write_bundle(self.root / "b", self.config)
        (folder / "olist_order_reviews_dataset.csv").write_text("anything\n", encoding="utf-8")
        self.assertNotIn("order_reviews", predict.bundle_files(folder))

    def test_scoring_without_a_trained_model_says_run_the_chain(self) -> None:
        import dataclasses

        folder = write_bundle(self.root / "b", self.config)
        config = dataclasses.replace(
            self.config,
            features=dataclasses.replace(self.config.features, curated_dir=self.root / "empty"),
        )
        with self.assertRaises(NotReadyError) as caught:
            predict.score_file(folder, config)
        self.assertIn("run_chain", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
