import tempfile
import unittest
from pathlib import Path

from data_factory.outputs import sha256_file
from data_factory.profile import (
    DISTINCT_CAP,
    ColumnProfile,
    QualityProfile,
    write_profile,
)


class ColumnProfileTests(unittest.TestCase):
    def test_null_share_distinct_and_range(self):
        profile = ColumnProfile()
        for value in ("a", None, "a", "b"):
            profile.add(value)
        result = profile.as_dict()
        self.assertEqual((result["rows"], result["null_count"]), (4, 1))
        self.assertEqual(result["null_percent"], 25.0)
        self.assertEqual((result["distinct_values"], result["min"], result["max"]), (2, "a", "b"))

    def test_distinct_count_is_capped(self):
        profile = ColumnProfile()
        for value in range(DISTINCT_CAP + 1):
            profile.add(value)
        result = profile.as_dict()
        self.assertTrue(result["distinct_capped"])
        self.assertIsNone(result["distinct_values"])
        self.assertEqual((result["min"], result["max"]), (0, DISTINCT_CAP))

    def test_all_null_column_has_no_range(self):
        profile = ColumnProfile()
        profile.add(None)
        result = profile.as_dict()
        self.assertEqual((result["min"], result["max"], result["distinct_values"]), (None, None, 0))


class QualityProfileTests(unittest.TestCase):
    def test_profiles_every_requested_column(self):
        profile = QualityProfile(("x", "y"))
        profile.add({"x": 1, "y": None})
        columns = profile.as_dict()["columns"]
        self.assertEqual(set(columns), {"x", "y"})
        self.assertEqual(columns["y"]["null_count"], 1)

    def test_written_report_is_byte_identical_for_equal_input(self):
        hashes = []
        for _ in range(2):
            profile = QualityProfile(("x",))
            for value in (3, 1, 2):
                profile.add({"x": value})
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "report.json"
                write_profile(path, profile)
                hashes.append(sha256_file(path))
        self.assertEqual(hashes[0], hashes[1])