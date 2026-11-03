import unittest
from datetime import date

from data_factory.config import parse_config
from data_factory.errors import ConfigError


def base():
    return {
        "source": {
            "url": "https://example.test/x.json",
            "agency": "NYPD",
            "complaint_types": ["Noise - Residential"],
            "columns": ["unique_key"],
            "start_date": date(2025, 1, 1),
            "end_date": date(2025, 6, 30),
            "page_size": 1000,
            "app_token_env": "TOKEN",
            "timezone": "America/New_York",
        },
        "http": {
            "timeout_seconds": 10,
            "max_retries": 3,
            "backoff_base_seconds": 1.5,
            "backoff_factor": 2.0,
            "retry_statuses": [429, 500, 503],
        },
        "paths": {"raw_dir": "r", "curated_dir": "c", "quarantine_dir": "q", "manifest_path": "m.json"},
        "quality": {"max_quarantine_ratio": 0.01, "min_rows_for_threshold": 10},
    }


class BooleanFieldTests(unittest.TestCase):
    def test_a_valid_config_parses(self):
        self.assertEqual(parse_config(base()).source.page_size, 1000)

    def test_booleans_are_rejected_for_every_numeric_field(self):
        fields = (
            ("source", "page_size"),
            ("http", "max_retries"),
            ("http", "timeout_seconds"),
            ("http", "backoff_base_seconds"),
            ("http", "backoff_factor"),
            ("quality", "max_quarantine_ratio"),
            ("quality", "min_rows_for_threshold"),
        )
        for section, key in fields:
            for value in (True, False):
                with self.subTest(field=f"{section}.{key}", value=value):
                    data = base()
                    data[section][key] = value
                    with self.assertRaises(ConfigError):
                        parse_config(data)
