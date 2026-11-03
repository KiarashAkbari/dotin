import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest import mock

from data_factory.config import SourceConfig
from data_factory.errors import FetchError
from data_factory.fetch import (
    SocrataSource, build_params, build_where, fetch_all, query_fingerprint,
)
from data_factory.store import RawPageStore
from zoneinfo import ZoneInfo


def make_cfg(**overrides) -> SourceConfig:
    base = dict(
        url="https://x.test/r.json", agency="NYPD",
        complaint_types=("Noise - Residential", "Noise - Park"),
        columns=("unique_key", "created_date"),
        start_date=date(2025, 1, 1), end_date=date(2025, 6, 30),
        page_size=10, app_token_env="TEST_TOKEN",
        timezone=ZoneInfo("America/New_York")
    )
    base.update(overrides)
    return SourceConfig(**base)


class FakeSource:
    def __init__(self, total, page_size=10, fail_at_offset=None):
        self.total = total
        self.page_size = page_size
        self.fail_at_offset = fail_at_offset
        self.calls = []

    def fetch_page(self, offset):
        self.calls.append(offset)
        if offset == self.fail_at_offset:
            raise FetchError("boom")
        stop = min(offset + self.page_size, self.total)
        return [{"unique_key": str(i)} for i in range(offset, stop)]


class OversizedSource:
    page_size = 10

    def fetch_page(self, offset):
        return [{"i": i} for i in range(11)]


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_json(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        return self.payload


class FetchAllTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = RawPageStore(Path(tmp.name) / "raw")

    def test_fetches_until_short_page(self):
        source = FakeSource(total=25)
        result = fetch_all(source, self.store)
        self.assertEqual(source.calls, [0, 10, 20])
        self.assertEqual((result.pages_total, result.rows_fetched), (3, 25))
        self.assertEqual(self.store.completed_pages(), 3)

    def test_exact_multiple_ends_with_empty_page(self):
        source = FakeSource(total=20)
        result = fetch_all(source, self.store)
        self.assertEqual(source.calls, [0, 10, 20])
        self.assertEqual(self.store.row_count(2), 0)
        self.assertEqual(result.rows_fetched, 20)

    def test_second_run_fetches_nothing(self):
        fetch_all(FakeSource(total=25), self.store)
        again = FakeSource(total=25)
        result = fetch_all(again, self.store)
        self.assertEqual(again.calls, [])
        self.assertTrue(result.already_complete)

    def test_resume_after_failure_continues_from_missing_page(self):
        with self.assertRaises(FetchError):
            fetch_all(FakeSource(total=25, fail_at_offset=20), self.store)
        self.assertEqual(self.store.completed_pages(), 2)

        resumed = FakeSource(total=25)
        result = fetch_all(resumed, self.store)
        self.assertEqual(resumed.calls, [20])
        self.assertEqual(result.resumed_from_page, 2)
        keys = [r["unique_key"] for r in self.store.iter_rows()]
        self.assertEqual(len(keys), 25)
        self.assertEqual(len(set(keys)), 25)

    def test_oversized_page_is_rejected(self):
        with self.assertRaises(FetchError):
            fetch_all(OversizedSource(), self.store)
        self.assertEqual(self.store.completed_pages(), 0)


class QueryTests(unittest.TestCase):
    def test_where_uses_half_open_interval(self):
        where = build_where(make_cfg())
        self.assertIn("created_date >= '2025-01-01T00:00:00'", where)
        self.assertIn("created_date < '2025-07-01T00:00:00'", where)

    def test_quotes_are_escaped(self):
        where = build_where(make_cfg(complaint_types=("O'Brien",)))
        self.assertIn("'O''Brien'", where)

    def test_params_contain_order_and_paging(self):
        params = build_params(make_cfg(), 30)
        self.assertEqual(params["$order"], "created_date,unique_key")
        self.assertEqual((params["$limit"], params["$offset"]), (10, 30))
        self.assertEqual(params["$select"], "unique_key,created_date")

    def test_fingerprint_depends_on_scope_not_offset(self):
        base = make_cfg()
        self.assertEqual(query_fingerprint(base), query_fingerprint(make_cfg()))
        shifted = replace(base, end_date=date(2025, 7, 31))
        self.assertNotEqual(query_fingerprint(base), query_fingerprint(shifted))


class SocrataSourceTests(unittest.TestCase):
    def test_token_header_sent_when_env_set(self):
        client = FakeClient([])
        with mock.patch.dict(os.environ, {"TEST_TOKEN": "abc"}):
            SocrataSource.from_env(client, make_cfg()).fetch_page(0)
        self.assertEqual(client.calls[0][2], {"X-App-Token": "abc"})

    def test_no_header_without_token(self):
        client = FakeClient([])
        with mock.patch.dict(os.environ):
            os.environ.pop("TEST_TOKEN", None)
            SocrataSource.from_env(client, make_cfg()).fetch_page(0)
        self.assertIsNone(client.calls[0][2])

    def test_non_list_payload_is_rejected(self):
        source = SocrataSource(FakeClient({"error": "x"}), make_cfg())
        with self.assertRaises(FetchError):
            source.fetch_page(0)


if __name__ == "__main__":
    unittest.main()