import unittest
from zoneinfo import ZoneInfo

from data_factory.contract import ReasonCode
from data_factory.quality import QualityReport, classify_records
from helpers import make_row

NY = ZoneInfo("America/New_York")


def classify(rows):
    report = QualityReport()
    return list(classify_records(rows, report, NY)), report


class ClassifyRecordsTests(unittest.TestCase):
    def test_valid_rows_pass_and_keep_order(self):
        rows = [make_row(unique_key=str(i)) for i in range(3)]
        results, report = classify(rows)
        self.assertTrue(all(r.is_valid for r in results))
        self.assertEqual([r.row for r in results], rows)
        self.assertEqual((report.total, report.passed, report.quarantined), (3, 3, 0))

    def test_invalid_row_is_quarantined_with_reason(self):
        results, report = classify([make_row(borough="ATLANTIS")])
        self.assertEqual(results[0].reasons, (ReasonCode.VALUE_OUT_OF_DOMAIN,))
        self.assertEqual(report.quarantined, 1)

    def test_second_occurrence_of_a_key_is_duplicate(self):
        results, report = classify([make_row(), make_row()])
        self.assertEqual(results[0].reasons, ())
        self.assertEqual(results[1].reasons, (ReasonCode.DUPLICATE_UNIQUE_KEY,))
        self.assertEqual(report.passed, 1)

    def test_valid_row_after_invalid_one_with_same_key_passes(self):
        invalid = make_row(borough="ATLANTIS")
        valid = make_row()
        results, _ = classify([invalid, valid])
        self.assertEqual(results[0].reasons, (ReasonCode.VALUE_OUT_OF_DOMAIN,))
        self.assertEqual(results[1].reasons, ())

    def test_row_with_two_reasons_counts_once_as_quarantined(self):
        row = make_row(borough="ATLANTIS", incident_zip="N/A")
        _, report = classify([row])
        self.assertEqual(report.quarantined, 1)
        self.assertEqual(sum(report.by_reason.values()), 2)

    def test_ratio_counts_distinct_quarantined_rows(self):
        rows = [make_row(unique_key=str(i)) for i in range(3)]
        rows.append(make_row(unique_key="x", borough="ATLANTIS", incident_zip="N/A"))
        _, report = classify(rows)
        self.assertEqual(report.quarantine_ratio, 0.25)

    def test_ratio_of_empty_stream_is_zero(self):
        _, report = classify([])
        self.assertEqual(report.quarantine_ratio, 0.0)

    def test_report_is_updated_only_as_stream_is_consumed(self):
        report = QualityReport()
        stream = classify_records(
            [make_row(), make_row(unique_key="2")], report, NY
        )
        self.assertEqual(report.total, 0)
        next(stream)
        self.assertEqual(report.total, 1)