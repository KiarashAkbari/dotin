import unittest
from zoneinfo import ZoneInfo

from data_factory.clean import JoinStats, clean_record, clean_stream
from data_factory.contract import ReasonCode
from data_factory.errors import DataError
from data_factory.population import ZipArea
from data_factory.quality import ClassifiedRecord
from helpers import make_row

NY = ZoneInfo("America/New_York")
INDEX = {
    "10466": ZipArea("10466", 72479),
    "10001": ZipArea("10001", 23072),
    "99998": ZipArea("99999", None),
}


def clean(**overrides):
    stats = JoinStats()
    return clean_record(make_row(**overrides), NY, INDEX, stats), stats


class CleanRecordTests(unittest.TestCase):
    def test_matched_zip_gets_area_and_population(self):
        out, stats = clean(incident_zip="10466")
        self.assertEqual((out["modzcta"], out["population"]), ("10466", 72479))
        self.assertEqual(stats.matched, 1)

    def test_missing_zip_keeps_row_without_area(self):
        out, stats = clean(incident_zip=None)
        self.assertEqual((out["incident_zip"], out["modzcta"], out["population"]),
                         (None, None, None))
        self.assertEqual(stats.missing_zip, 1)

    def test_unmatched_zip_keeps_row_and_is_counted(self):
        out, stats = clean(incident_zip="12345")
        self.assertIsNone(out["modzcta"])
        self.assertEqual(stats.unmatched_zip, 1)
        self.assertEqual(stats.unmatched_by_zip["12345"], 1)

    def test_area_without_population_is_counted(self):
        out, stats = clean(incident_zip="99998")
        self.assertEqual((out["modzcta"], out["population"]), ("99999", None))
        self.assertEqual((stats.matched, stats.null_population), (1, 1))

    def test_duration_is_seconds_between_created_and_closed(self):
        out, _ = clean(
            created_date="2025-01-03T00:00:00.000", closed_date="2025-01-03T01:30:00.000"
        )
        self.assertEqual(out["duration_seconds"], 5400)
        self.assertFalse(out["is_ambiguous"])
        self.assertEqual(out["created_at"], "2025-01-03T00:00:00")

    def test_open_case_has_no_closing_time_or_duration(self):
        out, _ = clean(status="Open", closed_date=None)
        self.assertEqual((out["closed_at"], out["duration_seconds"]), (None, None))

    def test_fall_back_inversion_gets_positive_duration_and_flag(self):
        out, stats = clean(
            created_date="2025-11-02T01:04:03.000", closed_date="2025-11-02T01:03:22.000"
        )
        self.assertEqual(out["duration_seconds"], 3559)
        self.assertTrue(out["is_ambiguous"])
        self.assertEqual(stats.ambiguous, 1)

    def test_closing_order_violation_after_validation_is_a_data_error(self):
        with self.assertRaises(DataError):
            clean(
                created_date="2025-01-03T01:00:00.000",
                closed_date="2025-01-03T00:59:55.000",
            )


class CleanStreamTests(unittest.TestCase):
    def test_quarantined_records_are_skipped_and_rows_balance(self):
        records = [
            ClassifiedRecord(make_row(unique_key="1", incident_zip="10466"), ()),
            ClassifiedRecord(make_row(unique_key="2"), (ReasonCode.BAD_ZIP_FORMAT,)),
            ClassifiedRecord(make_row(unique_key="3", incident_zip=None), ()),
            ClassifiedRecord(make_row(unique_key="4", incident_zip="12345"), ()),
        ]
        stats = JoinStats()
        out = list(clean_stream(records, NY, INDEX, stats))
        self.assertEqual([r["unique_key"] for r in out], ["1", "3", "4"])
        self.assertEqual((stats.rows_in, stats.rows_out), (3, 3))
        self.assertEqual(
            stats.matched + stats.missing_zip + stats.unmatched_zip, stats.rows_out
        )