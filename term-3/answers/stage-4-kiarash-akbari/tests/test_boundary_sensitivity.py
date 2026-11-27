"""``scripts/boundary_sensitivity.py``: only the boundary moves, and the table says what the JSON says."""

from __future__ import annotations

import importlib.util
import io
import unittest
from datetime import date
from unittest import mock

import helpers
from capstone.config import load_config

SCRIPT = helpers.STAGE_ROOT / "scripts" / "boundary_sensitivity.py"


def load_script():
    spec = importlib.util.spec_from_file_location("boundary_sensitivity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fake_result(recall_10: float) -> dict:
    capacity = [
        {"share": 0.05, "caught_share": 0.1},
        {"share": 0.1, "caught_share": recall_10},
        {"share": 0.2, "caught_share": 0.3},
    ]
    return {
        "chosen_model": "hgb.d3",
        "evaluation": {
            "hgb.d3": {
                "labelled": 90,
                "negatives": 10,
                "auc": 0.6,
                "capacity": capacity,
                "months": [{"month": "2018-08", "recall": recall_10}],
            }
        },
        "rule_baseline": {"chosen": {"column": "n_items"}, "evaluation": {"capacity": capacity}},
        "split": {
            "train": {"rows": 900, "negatives": 100},
            "validation": {"months": ["2018-05"]},
            "evaluation": {"rows": 100},
        },
        "drift_vs_model": {"last_full_month": {"month": "2018-08", "recall": recall_10}},
    }


class BoundarySensitivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = load_script()

    def test_only_the_boundary_changes(self) -> None:
        config = load_config(helpers.DEFAULT_CONFIG)
        moved = self.script.with_boundary(config, date(2018, 7, 31))
        self.assertEqual(moved.split.boundary, date(2018, 7, 31))
        self.assertNotEqual(config.split.boundary, moved.split.boundary)
        self.assertEqual(moved.model, config.model)
        self.assertEqual(moved.capacity, config.capacity)
        self.assertEqual(moved.split.last_month_as_secondary, config.split.last_month_as_secondary)

    def test_the_default_set_starts_with_the_decided_boundary(self) -> None:
        config = load_config(helpers.DEFAULT_CONFIG)
        self.assertEqual(self.script.DEFAULT_BOUNDARIES[0], config.split.boundary.isoformat())
        self.assertIn("2018-07-31", self.script.DEFAULT_BOUNDARIES, "the boundary decisions.md moved away from")

    def test_the_table_carries_the_row_numbers(self) -> None:
        row = self.script.summary_row("2018-07-31", fake_result(0.25))
        self.assertEqual(row["recall_10"], 0.25)
        self.assertEqual(row["rule"], "n_items")
        text = self.script.render([row], ["# head"])
        self.assertIn("| 2018-07-31 | 900 (100) | 100 / 90 (10) | 2018-08 | hgb.d3 |", text)
        self.assertIn("0.2500", text)

    def test_a_bad_tag_or_date_exits_2_before_any_connection(self) -> None:
        for argv in (["--tag", "../x"], ["--boundary", "2018-13-01"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(self.script.main(argv), 2)


if __name__ == "__main__":
    unittest.main()
