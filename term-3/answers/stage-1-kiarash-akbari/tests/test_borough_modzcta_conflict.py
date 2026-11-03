import json
import tempfile
import unittest
from pathlib import Path

from config_helpers import make_config
from data_factory.pipeline import process
from data_factory.population import ZipArea
from helpers import make_row

INDEX = {"10466": ZipArea("10466", 72479)}


class BoroughModzctaConflictTests(unittest.TestCase):
    def test_zip_with_two_boroughs_is_reported(self):
        rows = [
            make_row(unique_key="1", borough="BRONX"),
            make_row(unique_key="2", borough="BROOKLYN"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            result = process(rows, cfg, INDEX)
            report = json.loads(
                (Path(tmp) / "curated" / "data_quality_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report["borough_modzcta_conflict_count"], 1)
            self.assertEqual(
                report["borough_modzcta_conflicts"],
                [{"modzcta": "10466", "boroughs": ["BRONX", "BROOKLYN"]}],
            )
