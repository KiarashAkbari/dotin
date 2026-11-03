import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from config_helpers import make_config
from data_factory.clean import JoinStats, clean_record
from data_factory.errors import ConfigError, DataError
from data_factory.fetch import query_fingerprint
from data_factory.rolling import load_watermark, rolling_config, rolling_manifest_path
from helpers import make_row

import zoneinfo

NY = zoneinfo.ZoneInfo("America/New_York")


def write_manifest_file(path, max_created_at):
    path.write_text(json.dumps({"watermark": {"max_created_at": max_created_at}}), "utf-8")


class RollingConfigTests(unittest.TestCase):
    def test_window_is_narrowed_and_outputs_are_kept_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            rolled = rolling_config(cfg, date(2025, 3, 1), date(2025, 3, 20))
            self.assertEqual(
                (rolled.source.start_date, rolled.source.end_date),
                (date(2025, 3, 1), date(2025, 3, 20)),
            )
            self.assertEqual(rolled.paths.curated_dir.name, "rolling")
            self.assertEqual(rolled.paths.quarantine_dir.name, "rolling")
            self.assertEqual(rolled.paths.manifest_path.name, "manifest.rolling.json")

    def test_rolling_window_never_reuses_the_full_runs_raw_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            rolled = rolling_config(cfg, date(2025, 3, 1), date(2025, 3, 20))
            self.assertNotEqual(query_fingerprint(cfg.source), query_fingerprint(rolled.source))

    def test_watermark_after_today_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            with self.assertRaises(ConfigError):
                rolling_config(cfg, date(2025, 4, 1), date(2025, 3, 20))


class WatermarkTests(unittest.TestCase):
    def test_reads_the_date_of_the_last_full_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_manifest_file(path, "2025-03-05T10:00:00")
            self.assertEqual(load_watermark(path), date(2025, 3, 5))

    def test_prefers_the_later_rolling_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_manifest_file(path, "2025-03-05T10:00:00")
            write_manifest_file(rolling_manifest_path(path), "2025-03-18T09:00:00")
            self.assertEqual(load_watermark(path), date(2025, 3, 18))

    def test_no_manifest_asks_for_a_full_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(DataError):
                load_watermark(Path(tmp) / "manifest.json")

    def test_manifest_without_watermark_asks_for_a_full_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            write_manifest_file(path, None)
            with self.assertRaises(DataError):
                load_watermark(path)


class MaxCreatedTests(unittest.TestCase):
    def test_clean_record_tracks_the_latest_creation_time(self):
        stats = JoinStats()
        for created in ("2025-01-05T10:00:00.000", "2025-01-09T08:00:00.000", "2025-01-02T00:00:00.000"):
            clean_record(
                make_row(created_date=created, closed_date="2025-01-10T00:00:00.000"),
                NY, {}, stats,
            )
        self.assertEqual(stats.max_created_at, "2025-01-09T08:00:00")