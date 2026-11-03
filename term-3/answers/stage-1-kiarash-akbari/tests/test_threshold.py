import unittest

from data_factory.errors import DataError, QuarantineThresholdError
from data_factory.exit_codes import ExitCode
from data_factory.quality import QualityReport, enforce_threshold

MAX_RATIO = 0.01
MIN_ROWS = 1000


def report(total, quarantined):
    return QualityReport(total=total, passed=total - quarantined, quarantined=quarantined)


def check(rep):
    enforce_threshold(rep, max_ratio=MAX_RATIO, min_rows=MIN_ROWS)


class EnforceThresholdTests(unittest.TestCase):
    def test_ratio_exactly_at_limit_passes(self):
        check(report(2000, 20))

    def test_ratio_over_limit_fails(self):
        with self.assertRaises(QuarantineThresholdError):
            check(report(2000, 21))

    def test_small_run_ignores_ratio(self):
        check(report(500, 20))

    def test_nothing_passed_fails_even_for_small_run(self):
        with self.assertRaises(QuarantineThresholdError):
            check(report(5, 5))

    def test_empty_report_fails(self):
        with self.assertRaises(DataError):
            check(report(0, 0))

    def test_error_uses_data_exit_code(self):
        self.assertTrue(issubclass(QuarantineThresholdError, DataError))
        self.assertEqual(QuarantineThresholdError.exit_code, ExitCode.DATA)

    def test_error_context_has_counts(self):
        with self.assertRaises(QuarantineThresholdError) as ctx:
            check(report(2000, 21))
        self.assertEqual(ctx.exception.context["quarantined"], 21)
        self.assertEqual(ctx.exception.context["total"], 2000)