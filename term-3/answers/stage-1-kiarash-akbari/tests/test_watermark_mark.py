import json
import tempfile
import unittest
import zoneinfo
from pathlib import Path

from config_helpers import make_config
from data_factory.clean import JoinStats, clean_record
from data_factory.manifest import build_manifest
from data_factory.pipeline import process
from data_factory.population import ZipArea
from data_factory.rolling import Watermark, after_watermark, load_mark, rolling_manifest_path
from helpers import make_row

NY = zoneinfo.ZoneInfo("America/New_York")
MARK = Watermark("2025-03-05T10:00:00", frozenset({"a"}))


def raw(created, key):
    return {"created_date": created, "unique_key": key}


def write_manifest_file(path, created_at, keys):
    payload = {"watermark": {"max_created_at": created_at, "keys_at_max_created_at": keys}}
    path.write_text(json.dumps(payload), "utf-8")


class AfterWatermarkTests(unittest.TestCase):
    def test_drops_older_rows_and_ties_already_seen(self):
        rows = [
            raw("2025-03-05T09:00:00.000", "x"),
            raw("2025-03-05T10:00:00.000", "a"),
            raw("2025-03-05T10:00:00.000", "b"),
            raw("2025-03-05T10:00:01.000", "c"),
        ]
        kept = [r["unique_key"] for r in after_watermark(rows, MARK)]
        self.assertEqual(kept, ["b", "c"])

    def test_without_a_mark_everything_is_kept(self):
        rows = [raw("2025-03-05T09:00:00.000", "x")]
        self.assertEqual(list(after_watermark(rows, None)), rows)

    def test_rows_with_unreadable_dates_reach_the_validator(self):
        rows = [raw(None, "p"), raw("03/01/2025", "q")]
        self.assertEqual(list(after_watermark(rows, MARK)), rows)


class LoadMarkTests(unittest.TestCase):
    def test_reads_the_second_and_the_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_manifest_file(path, "2025-03-05T10:00:00", ["a", "b"])
            self.assertEqual(load_mark(path), Watermark("2025-03-05T10:00:00", frozenset({"a", "b"})))

    def test_prefers_the_later_rolling_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_manifest_file(path, "2025-03-05T10:00:00", ["a"])
            write_manifest_file(rolling_manifest_path(path), "2025-03-18T09:00:00", ["z"])
            self.assertEqual(load_mark(path), Watermark("2025-03-18T09:00:00", frozenset({"z"})))

    def test_no_manifest_gives_no_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(load_mark(Path(tmp) / "manifest.json"))


class TieKeyTests(unittest.TestCase):
    def test_clean_record_collects_keys_at_the_latest_second(self):
        stats = JoinStats()
        rows = (
            ("1", "2025-01-05T10:00:00.000"),
            ("2", "2025-01-09T08:00:00.000"),
            ("3", "2025-01-09T08:00:00.000"),
            ("4", "2025-01-02T00:00:00.000"),
        )
        for key, created in rows:
            row = make_row(unique_key=key, created_date=created, closed_date="2025-01-10T00:00:00.000")
            clean_record(row, NY, {}, stats)
        self.assertEqual(sorted(stats.keys_at_max_created), ["2", "3"])

    def test_manifest_records_the_tie_keys_for_the_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            row = make_row(
                unique_key="7",
                incident_zip="10466",
                created_date="2025-01-10T10:00:00.000",
                closed_date="2025-01-10T11:00:00.000",
            )
            result = process([row], cfg, {"10466": ZipArea("10466", 72479)})
            manifest = build_manifest(
                cfg=cfg, result=result, requests=0, started_at="s", finished_at="f"
            )
            self.assertEqual(
                manifest["watermark"],
                {"max_created_at": "2025-01-10T10:00:00", "keys_at_max_created_at": ["7"]},
            )
