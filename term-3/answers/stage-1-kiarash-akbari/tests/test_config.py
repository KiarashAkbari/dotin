import tempfile
import unittest
from pathlib import Path

from data_factory.config import load_config
from data_factory.errors import ConfigError

VALID = """
[source]
url = "https://example.test/x.json"
agency = "NYPD"
complaint_types = ["Noise - Residential"]
columns = ["unique_key", "created_date"]
start_date = 2025-01-01
end_date = 2025-06-30
page_size = 1000
app_token_env = "TOKEN"
timezone = "America/New_York"

[http]
timeout_seconds = 10
max_retries = 3
backoff_base_seconds = 1.5
backoff_factor = 2.0
retry_statuses = [429, 500, 503]

[paths]
raw_dir = "raw"
curated_dir = "out"
quarantine_dir = "q"
manifest_path = "m.json"

[quality]
max_quarantine_ratio = 0.05
min_rows_for_threshold = 1000
"""


class ConfigTests(unittest.TestCase):
    def _load(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_valid_config(self):
        cfg = self._load(VALID)
        self.assertEqual(cfg.source.page_size, 1000)
        self.assertEqual(cfg.http.max_retries, 3)
        self.assertEqual(cfg.http.backoff_factor, 2.0)
        self.assertEqual(cfg.http.retry_statuses, frozenset({429, 500, 503}))
        self.assertEqual(cfg.source.complaint_types, ("Noise - Residential",))

    def test_paths_use_curated_dir(self):
        cfg = self._load(VALID)
        self.assertEqual(cfg.paths.curated_dir, Path("out"))
        self.assertEqual(cfg.paths.raw_dir, Path("raw"))

    def test_old_processed_dir_key_rejected(self):
        bad = VALID.replace("curated_dir", "processed_dir")
        with self.assertRaisesRegex(ConfigError, "paths.curated_dir"):
            self._load(bad)

    def test_invalid_backoff_factor(self):
        bad = VALID.replace("backoff_factor = 2.0", "backoff_factor = 0.5")
        with self.assertRaisesRegex(ConfigError, "backoff_factor"):
            self._load(bad)

    def test_invalid_retry_statuses(self):
        bad = VALID.replace("retry_statuses = [429, 500, 503]", "retry_statuses = [429, 42]")
        with self.assertRaisesRegex(ConfigError, "retry_statuses"):
            self._load(bad)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config("/nonexistent/config.toml")

    def test_invalid_toml(self):
        with self.assertRaises(ConfigError):
            self._load("not = [valid")

    def test_start_after_end(self):
        bad = VALID.replace("start_date = 2025-01-01", "start_date = 2026-01-01")
        with self.assertRaises(ConfigError):
            self._load(bad)

    def test_page_size_too_large(self):
        with self.assertRaises(ConfigError):
            self._load(VALID.replace("page_size = 1000", "page_size = 60000"))

    def test_missing_section(self):
        with self.assertRaises(ConfigError):
            self._load(VALID.split("[quality]")[0])

    def test_quality_settings_are_loaded(self):
        cfg = self._load(VALID)
        self.assertEqual(cfg.quality.max_quarantine_ratio, 0.05)
        self.assertEqual(cfg.quality.min_rows_for_threshold, 1000)

    def test_invalid_min_rows(self):
        for bad in ("0", "-5", "1.5", "true"):
            with self.subTest(value=bad):
                text = VALID.replace(
                    "min_rows_for_threshold = 1000", f"min_rows_for_threshold = {bad}"
                )
                with self.assertRaisesRegex(ConfigError, "min_rows_for_threshold"):
                    self._load(text)

    def test_missing_min_rows(self):
        text = VALID.replace("min_rows_for_threshold = 1000\n", "")
        with self.assertRaisesRegex(ConfigError, "min_rows_for_threshold"):
            self._load(text)

    def test_timezone_is_loaded(self):
        cfg = self._load(VALID)
        self.assertEqual(cfg.source.timezone.key, "America/New_York")

    def test_unknown_timezone_rejected(self):
        bad = VALID.replace("America/New_York", "Mars/Olympus")
        with self.assertRaisesRegex(ConfigError, "source.timezone"):
            self._load(bad)

    def test_missing_timezone_rejected(self):
        bad = VALID.replace('timezone = "America/New_York"\n', "")
        with self.assertRaisesRegex(ConfigError, "source.timezone"):
            self._load(bad)


if __name__ == "__main__":
    unittest.main()

