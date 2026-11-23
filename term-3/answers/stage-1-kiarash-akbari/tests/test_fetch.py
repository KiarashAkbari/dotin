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
    SocrataSource, build_params, build_where, cursor_of, fetch_all, query_fingerprint,
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
        params = build_params(make_cfg(), ("2025-01-02T00:00:00.000", "7"))
        self.assertEqual(params["$order"], "created_date,unique_key")
        self.assertEqual(params["$limit"], 10)
        self.assertNotIn("$offset", params, "paging is by keyset, not by offset")
        self.assertEqual(params["$select"], "unique_key,created_date")
        self.assertIn(
            "AND (created_date > '2025-01-02T00:00:00.000' OR "
            "(created_date = '2025-01-02T00:00:00.000' AND unique_key > '7'))",
            params["$where"],
        )

    def test_first_page_has_no_cursor_condition(self):
        self.assertEqual(build_params(make_cfg())["$where"], build_where(make_cfg()))

    def test_fingerprint_depends_on_scope_not_offset(self):
        base = make_cfg()
        self.assertEqual(query_fingerprint(base), query_fingerprint(make_cfg()))
        shifted = replace(base, end_date=date(2025, 7, 31))
        self.assertNotEqual(query_fingerprint(base), query_fingerprint(shifted))


class SocrataSourceTests(unittest.TestCase):
    def test_token_header_sent_when_env_set(self):
        client = FakeClient([])
        with mock.patch.dict(os.environ, {"TEST_TOKEN": "abc"}):
            SocrataSource.from_env(client, make_cfg()).fetch_after(None)
        self.assertEqual(client.calls[0][2], {"X-App-Token": "abc"})

    def test_no_header_without_token(self):
        client = FakeClient([])
        with mock.patch.dict(os.environ):
            os.environ.pop("TEST_TOKEN", None)
            SocrataSource.from_env(client, make_cfg()).fetch_after(None)
        self.assertIsNone(client.calls[0][2])

    def test_non_list_payload_is_rejected(self):
        source = SocrataSource(FakeClient({"error": "x"}), make_cfg())
        with self.assertRaises(FetchError):
            source.fetch_after(None)

    def test_a_row_at_or_before_the_cursor_is_rejected(self):
        row = {"unique_key": "5", "created_date": "2025-01-01T00:00:00.000"}
        source = SocrataSource(FakeClient([row]), make_cfg())
        with self.assertRaises(FetchError):
            source.fetch_after(("2025-01-01T00:00:00.000", "5"))


if __name__ == "__main__":
    unittest.main()

def live_row(i):
    return {"unique_key": f"{i:04d}", "created_date": f"2025-01-01T00:00:{i % 60:02d}.000"}


class LiveTable:
    """A source table that changes between requests, served by offset or by keyset."""

    def __init__(self, keys, page_size=10):
        self.rows = sorted((live_row(i) for i in keys), key=cursor_of)
        self.page_size = page_size
        self.requests = 0
        self.on_request = {}

    def _tick(self):
        self.requests += 1
        change = self.on_request.get(self.requests)
        if change:
            change(self)

    def offset_page(self, offset):
        self._tick()
        return self.rows[offset: offset + self.page_size]

    def keyset_page(self, cursor):
        self._tick()
        later = [r for r in self.rows if cursor is None or cursor_of(r) > cursor]
        return later[: self.page_size]


class OffsetLive:
    def __init__(self, table):
        self.table, self.page_size = table, table.page_size

    def fetch_page(self, offset):
        return self.table.offset_page(offset)


class KeysetLive:
    def __init__(self, table):
        self.table, self.page_size = table, table.page_size
        self.cursors = []

    def cursor_of(self, row):
        return cursor_of(row)

    def fetch_after(self, cursor):
        self.cursors.append(cursor)
        return self.table.keyset_page(cursor)


def delete_first(table):
    table.rows.pop(0)


def insert_early(table):
    table.rows.insert(0, {"unique_key": "0000a", "created_date": "2025-01-01T00:00:00.000"})


class KeysetPagingTests(unittest.TestCase):
    """An upstream delete or insert between two requests must not skip or repeat rows."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def _keys(self, store):
        return [r["unique_key"] for r in store.iter_rows()]

    def test_offset_paging_skips_a_row_when_an_earlier_one_is_deleted(self):
        # documents the defect the keyset paging fixes
        table = LiveTable(range(25))
        table.on_request[2] = delete_first
        store = RawPageStore(self.root / "offset")
        fetch_all(OffsetLive(table), store)
        self.assertNotIn("0010", self._keys(store))

    def test_keyset_paging_survives_an_upstream_delete(self):
        table = LiveTable(range(25))
        table.on_request[2] = delete_first
        store = RawPageStore(self.root / "keyset")
        fetch_all(KeysetLive(table), store)
        keys = self._keys(store)
        self.assertEqual(keys, sorted(set(keys)), "no duplicates, in order")
        self.assertEqual(set(keys) - {"0000"}, {f"{i:04d}" for i in range(1, 25)})

    def test_keyset_paging_does_not_repeat_rows_after_an_upstream_insert(self):
        table = LiveTable(range(25))
        table.on_request[2] = insert_early
        store = RawPageStore(self.root / "keyset")
        fetch_all(KeysetLive(table), store)
        keys = self._keys(store)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), {f"{i:04d}" for i in range(25)})

    def test_resume_continues_after_the_last_stored_row(self):
        table = LiveTable(range(25))
        store = RawPageStore(self.root / "keyset")
        failing = KeysetLive(table)
        calls = {"n": 0}

        def flaky(cursor):
            calls["n"] += 1
            if calls["n"] == 3:
                raise FetchError("boom")
            return table.keyset_page(cursor)

        failing.fetch_after = flaky
        with self.assertRaises(FetchError):
            fetch_all(failing, store)
        self.assertEqual(store.completed_pages(), 2)

        resumed = KeysetLive(table)
        result = fetch_all(resumed, store)
        self.assertEqual(result.resumed_from_page, 2)
        self.assertEqual(resumed.cursors, [cursor_of(live_row(19))])
        self.assertEqual(self._keys(store), [f"{i:04d}" for i in range(25)])

    def test_a_row_without_a_cursor_cannot_be_paged_past(self):
        with self.assertRaises(FetchError):
            cursor_of({"unique_key": "1"})


class OpenWindowTests(unittest.TestCase):
    """A second run on the same day must see cases created since the first one."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = RawPageStore(Path(tmp.name) / "raw")
        self.table = LiveTable(range(25))
        fetch_all(KeysetLive(self.table), self.store)
        self.table.rows.append({"unique_key": "0099", "created_date": "2025-01-01T23:59:59.000"})

    def test_a_closed_window_trusts_its_cache(self):
        again = KeysetLive(self.table)
        result = fetch_all(again, self.store)
        self.assertTrue(result.already_complete)
        self.assertEqual(again.cursors, [])

    def test_an_open_window_refetches_the_short_last_page(self):
        again = KeysetLive(self.table)
        result = fetch_all(again, self.store, refresh_tail=True)
        self.assertFalse(result.already_complete)
        self.assertEqual(result.resumed_from_page, 2)
        self.assertEqual(again.cursors, [cursor_of(live_row(19))])
        keys = [r["unique_key"] for r in self.store.iter_rows()]
        self.assertIn("0099", keys)
        self.assertEqual(len(keys), len(set(keys)), "the refreshed page replaces, not appends")

    def test_an_open_window_with_offset_paging_also_refreshes(self):
        store = RawPageStore(self.store.directory.parent / "offset")
        fetch_all(FakeSource(total=25), store)
        again = FakeSource(total=27)
        fetch_all(again, store, refresh_tail=True)
        self.assertEqual(again.calls, [20])
        self.assertEqual(sum(1 for _ in store.iter_rows()), 27)


class CliOpenWindowTests(unittest.TestCase):
    def _fetch_with(self, today):
        from data_factory import cli
        from config_helpers import make_config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            client = mock.Mock(request_count=0)
            with (
                mock.patch.object(cli, "_today", return_value=today),
                mock.patch.object(cli, "fetch_all") as fetch,
                mock.patch("builtins.print"),
            ):
                fetch.return_value = mock.Mock(
                    pages_total=0, pages_fetched=0, rows_fetched=0, resumed_from_page=0
                )
                cli._fetch(cfg, client)
            return fetch.call_args.kwargs["refresh_tail"]

    def test_window_ending_today_refreshes_its_tail(self):
        self.assertTrue(self._fetch_with(date(2025, 3, 31)))

    def test_window_that_ended_trusts_the_cache(self):
        self.assertFalse(self._fetch_with(date(2025, 4, 1)))
