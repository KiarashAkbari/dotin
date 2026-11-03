import csv
import tempfile
import unittest
from pathlib import Path

from config_helpers import make_config
from data_factory.pipeline import process
from data_factory.population import ZipArea
from helpers import make_row

INDEX = {"10466": ZipArea("10466", 72479)}


class ZipNormalizationTests(unittest.TestCase):
    def test_padded_zip_is_cleaned_and_joined_not_quarantined(self):
        row = make_row(
            unique_key="1",
            incident_zip=" 10466 ",
            created_date="2025-01-10T10:00:00.000",
            closed_date="2025-01-10T11:00:00.000",
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            result = process([row], cfg, INDEX)
            self.assertEqual((result.report.passed, result.report.quarantined), (1, 0))
            self.assertEqual((result.stats.matched, result.stats.unmatched_zip), (1, 0))
            with (Path(tmp) / "curated" / "cases.csv").open(encoding="utf-8", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(rows[0]["incident_zip"], "10466")
