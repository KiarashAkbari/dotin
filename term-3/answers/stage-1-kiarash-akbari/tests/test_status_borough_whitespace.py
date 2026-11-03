import csv
import tempfile
import unittest
from pathlib import Path

from config_helpers import make_config
from data_factory.pipeline import process
from data_factory.population import ZipArea
from helpers import make_row

INDEX = {"10466": ZipArea("10466", 72479)}


class StatusBoroughWhitespaceTests(unittest.TestCase):
    def _run(self, rows, *, max_ratio: float = 0.5):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), max_ratio=max_ratio)
            result = process(rows, cfg, INDEX)
            with (Path(tmp) / "curated" / "cases.csv").open(encoding="utf-8", newline="") as fh:
                rows_out = list(csv.DictReader(fh))
            return result, rows_out

    def test_padded_status_with_no_closed_date_is_quarantined(self):
        bad = make_row(unique_key="bad", status="Closed ", closed_date=None)
        good = make_row(unique_key="good")
        result, rows_out = self._run([bad, good], max_ratio=0.999)
        self.assertEqual((result.report.passed, result.report.quarantined), (1, 1))
        self.assertEqual(
            result.report.by_reason.get("CLOSED_WITHOUT_CLOSED_DATE", 0), 1
        )
        self.assertEqual(len(rows_out), 1)
        self.assertEqual(rows_out[0]["unique_key"], "good")

    def test_padded_borough_passes_validation(self):
        row = make_row(borough=" BRONX ")
        result, rows_out = self._run([row])
        self.assertEqual((result.report.passed, result.report.quarantined), (1, 0))
        self.assertEqual(rows_out[0]["borough"], "BRONX")

    def test_padded_created_date_passes_validation(self):
        row = make_row(created_date=" 2025-01-03T00:57:07.000")
        result, _ = self._run([row])
        self.assertEqual((result.report.passed, result.report.quarantined), (1, 0))

    def test_clean_output_strips_borough(self):
        row = make_row(borough=" BRONX ")
        result, rows_out = self._run([row])
        self.assertEqual(rows_out[0]["borough"], "BRONX")