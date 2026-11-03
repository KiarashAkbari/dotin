import unittest
from datetime import datetime, timezone

from data_factory.config import HttpConfig
from data_factory.http import MAX_RETRY_AFTER_SECONDS, HttpClient, _parse_retry_after

NOW = datetime(2026, 10, 21, 7, 28, 0, tzinfo=timezone.utc)
CONFIG = HttpConfig(10.0, 3, 1.5, 2.0, frozenset({429}))


class RetryAfterTests(unittest.TestCase):
    def test_seconds_form(self):
        self.assertEqual(_parse_retry_after("5"), 5.0)

    def test_http_date_form(self):
        self.assertEqual(_parse_retry_after("Wed, 21 Oct 2026 07:28:30 GMT", NOW), 30.0)

    def test_a_date_in_the_past_means_no_wait(self):
        self.assertEqual(_parse_retry_after("Wed, 21 Oct 2026 07:27:00 GMT", NOW), 0.0)

    def test_garbage_is_ignored(self):
        self.assertIsNone(_parse_retry_after("soon", NOW))
        self.assertIsNone(_parse_retry_after("-3", NOW))
        self.assertIsNone(_parse_retry_after(None, NOW))

    def test_delay_is_capped(self):
        client = HttpClient(CONFIG, session=object(), sleep=lambda seconds: None)
        self.assertEqual(client._delay(0, 3600.0), MAX_RETRY_AFTER_SECONDS)
        self.assertEqual(client._delay(0, 5.0), 5.0)
