from datetime import date
from pathlib import Path

from data_factory.config import Config, parse_config


def make_config(root: Path, *, max_ratio: float = 0.5, min_rows: int = 1) -> Config:
    return parse_config(
        {
            "source": {
                "url": "https://example.test/cases.json",
                "agency": "NYPD",
                "complaint_types": ["Noise - Residential"],
                "columns": ["unique_key"],
                "start_date": date(2025, 1, 1),
                "end_date": date(2025, 3, 31),
                "page_size": 100,
                "app_token_env": "TOKEN",
                "timezone": "America/New_York",
            },
            "http": {
                "timeout_seconds": 5,
                "max_retries": 1,
                "backoff_base_seconds": 0.0,
                "backoff_factor": 1.0,
                "retry_statuses": [503],
            },
            "paths": {
                "raw_dir": str(root / "raw"),
                "curated_dir": str(root / "curated"),
                "quarantine_dir": str(root / "quarantine"),
                "manifest_path": str(root / "manifest.json"),
            },
            "quality": {"max_quarantine_ratio": max_ratio, "min_rows_for_threshold": min_rows},
            "reference": {
                "url": "https://example.test/ref.json",
                "columns": ["modzcta", "label", "pop_est"],
                "limit": 500,
                "path": str(root / "ref.json"),
            },
        }
    )