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

BASE_TOML = """
[source]
url = "https://example.test/cases.json"
agency = "NYPD"
complaint_types = ["Noise - Residential"]
columns = ["unique_key", "created_date"]
start_date = 2025-01-01
end_date = 2025-03-31
page_size = 100
app_token_env = "TOKEN"
timezone = "America/New_York"

[http]
timeout_seconds = 5
max_retries = 0
backoff_base_seconds = 0.0
backoff_factor = 1.0
retry_statuses = [503]

[paths]
raw_dir = "{raw}"
curated_dir = "{curated}"
quarantine_dir = "{quarantine}"
manifest_path = "{manifest}"

[quality]
max_quarantine_ratio = 0.5
min_rows_for_threshold = 1

[reference]
url = "https://example.test/ref.json"
columns = ["modzcta", "label", "pop_est"]
limit = 500
path = "{reference}"
"""


def write_config_toml(path: Path, *, extra: str = "", **paths: str) -> Path:
    """Write a complete stage-1 TOML config; path values default to absolute paths
    under the config file's folder, and any of them may be overridden (for example
    with a relative path, to test how relative paths are resolved)."""
    path = Path(path)
    root = path.parent
    values = {
        "raw": (root / "raw").as_posix(),
        "curated": (root / "curated").as_posix(),
        "quarantine": (root / "quarantine").as_posix(),
        "manifest": (root / "manifest.json").as_posix(),
        "reference": (root / "ref.json").as_posix(),
    }
    values.update(paths)
    path.write_text(BASE_TOML.format(**values) + extra, encoding="utf-8")
    return path
