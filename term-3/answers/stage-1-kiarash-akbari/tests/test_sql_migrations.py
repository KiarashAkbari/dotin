from datetime import date
from pathlib import Path

import pytest

from data_factory.config import parse_config
from data_factory.database import sql_migration_files
from data_factory.errors import DataError


def test_sql_migration_files_are_direct_and_ordered(tmp_path: Path) -> None:
    (tmp_path / "002_views.sql").write_text("SELECT 2;")
    (tmp_path / "001_schema.sql").write_text("SELECT 1;")
    (tmp_path / "queries").mkdir()
    (tmp_path / "queries" / "999_dashboard.sql").write_text("SELECT 999;")
    (tmp_path / "rejected").mkdir()
    (tmp_path / "rejected" / "000_rejected.sql").write_text("SELECT 0;")

    files = sql_migration_files(tmp_path)

    assert [path.name for path in files] == [
        "001_schema.sql",
        "002_views.sql",
    ]


def test_sql_migration_files_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(DataError):
        sql_migration_files(tmp_path / "missing")


def test_database_config_accepts_optional_sql_migrations_dir() -> None:
    config = parse_config(
        {
            "source": {
                "url": "https://example.test",
                "agency": "NYPD",
                "complaint_types": ["Noise - Residential"],
                "columns": ["unique_key"],
                "start_date": date(2025, 1, 1),
                "end_date": date(2025, 1, 2),
                "page_size": 1000,
                "app_token_env": "TOKEN",
                "timezone": "America/New_York",
            },
            "http": {
                "timeout_seconds": 1,
                "max_retries": 0,
                "backoff_base_seconds": 0,
                "backoff_factor": 1,
                "retry_statuses": [500],
            },
            "paths": {
                "raw_dir": "raw",
                "curated_dir": "curated",
                "quarantine_dir": "quarantine",
                "manifest_path": "manifest.json",
            },
            "quality": {
                "max_quarantine_ratio": 0.5,
                "min_rows_for_threshold": 1,
            },
            "reference": {
                "url": "https://example.test",
                "columns": ["modzcta"],
                "limit": 1,
                "path": "reference.json",
            },
            "database": {
                "url": "postgresql+psycopg://user@localhost/db",
                "password_env": "PASSWORD",
                "olist_csv_dir": "csv",
                "stage1_curated_dir": "curated",
                "batch_size": 1,
                "sql_migrations_dir": "sql",
            },
        }
    )

    assert config.database is not None
    assert config.database.sql_migrations_dir == Path("sql")