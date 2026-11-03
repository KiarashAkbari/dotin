import tempfile
import unittest
from pathlib import Path

from data_factory.fetch_log import (
    FETCH_LOG_FILENAME,
    minimum_requests,
    read_requests_total,
    record_requests,
)


class FetchLogTests(unittest.TestCase):
    def test_requests_accumulate_across_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(record_requests(Path(tmp), 3), 3)
            self.assertEqual(record_requests(Path(tmp), 2), 5)
            self.assertEqual(read_requests_total(Path(tmp)), 5)

    def test_missing_log_reads_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_requests_total(Path(tmp)))

    def test_zero_requests_do_not_create_a_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(record_requests(Path(tmp), 0))
            self.assertFalse((Path(tmp) / FETCH_LOG_FILENAME).exists())


class MinimumRequestsTests(unittest.TestCase):
    def test_rounds_up_to_whole_pages(self):
        self.assertEqual(minimum_requests(1_083_411, 50_000), 22)
        self.assertEqual(minimum_requests(100, 50), 2)

    def test_empty_result_still_needs_one_request(self):
        self.assertEqual(minimum_requests(0, 50_000), 1)