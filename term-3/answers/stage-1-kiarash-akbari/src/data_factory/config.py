"""Typed, validated configuration loaded from TOML."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ConfigError

MAX_SOCRATA_PAGE = 50_000
MIN_HTTP_STATUS = 100
MAX_HTTP_STATUS = 599


@dataclass(frozen=True)
class SourceConfig:
    url: str
    agency: str
    complaint_types: tuple[str, ...]
    columns: tuple[str, ...]
    start_date: date
    end_date: date
    page_size: int
    app_token_env: str
    timezone: ZoneInfo


@dataclass(frozen=True)
class HttpConfig:
    timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    backoff_factor: float
    retry_statuses: frozenset[int]


@dataclass(frozen=True)
class PathsConfig:
    raw_dir: Path
    curated_dir: Path
    quarantine_dir: Path
    manifest_path: Path


@dataclass(frozen=True)
class QualityConfig:
    max_quarantine_ratio: float
    min_rows_for_threshold: int


@dataclass(frozen=True)
class ReferenceConfig:
    url: str
    columns: tuple[str, ...]
    limit: int
    path: Path


@dataclass(frozen=True)
class DatabaseConfig:
    url: str
    password_env: str
    olist_csv_dir: Path
    stage1_curated_dir: Path
    batch_size: int


@dataclass(frozen=True)
class Config:
    source: SourceConfig
    http: HttpConfig
    paths: PathsConfig
    quality: QualityConfig
    reference: ReferenceConfig | None = None
    database: DatabaseConfig | None = None


def _section(data: dict, name: str) -> dict:
    section = data.get(name)
    if not isinstance(section, dict):
        raise ConfigError(f"Missing config section: [{name}]")
    return section


def _get(section: dict, key: str, name: str):
    if key not in section:
        raise ConfigError(f"Missing config key: {name}.{key}")
    return section[key]


def _str_tuple(value, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{label} must be a non-empty list of strings")
    return tuple(value)


def _zone(value) -> ZoneInfo:
    if not isinstance(value, str):
        raise ConfigError("source.timezone must be an IANA time zone name")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"source.timezone is not a known time zone: {value}") from exc


def _is_http_status(value) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_HTTP_STATUS <= value <= MAX_HTTP_STATUS
    )


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_reference(section: dict) -> ReferenceConfig:
    limit = _get(section, "limit", "reference")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ConfigError("reference.limit must be an integer >= 1")
    return ReferenceConfig(
        url=str(_get(section, "url", "reference")),
        columns=_str_tuple(_get(section, "columns", "reference"), "reference.columns"),
        limit=limit,
        path=Path(_get(section, "path", "reference")),
    )


def _parse_database(section: dict) -> DatabaseConfig:
    batch_size = _get(section, "batch_size", "database")
    if not _is_int(batch_size) or batch_size < 1:
        raise ConfigError("database.batch_size must be an integer >= 1")

    return DatabaseConfig(
        url=str(_get(section, "url", "database")),
        password_env=str(_get(section, "password_env", "database")),
        olist_csv_dir=Path(_get(section, "olist_csv_dir", "database")),
        stage1_curated_dir=Path(_get(section, "stage1_curated_dir", "database")),
        batch_size=batch_size,
    )


def parse_config(data: dict) -> Config:
    s = _section(data, "source")
    h = _section(data, "http")
    p = _section(data, "paths")
    q = _section(data, "quality")

    start = _get(s, "start_date", "source")
    end = _get(s, "end_date", "source")
    if not isinstance(start, date) or not isinstance(end, date):
        raise ConfigError("source.start_date and source.end_date must be TOML dates")
    if start > end:
        raise ConfigError("source.start_date must not be after source.end_date")

    page_size = _get(s, "page_size", "source")
    if not _is_int(page_size) or not 1 <= page_size <= MAX_SOCRATA_PAGE:
        raise ConfigError(f"source.page_size must be an integer in 1..{MAX_SOCRATA_PAGE}")

    retries = _get(h, "max_retries", "http")
    if not _is_int(retries) or retries < 0:
        raise ConfigError("http.max_retries must be an integer >= 0")

    timeout = _get(h, "timeout_seconds", "http")
    backoff = _get(h, "backoff_base_seconds", "http")
    if not _is_number(timeout) or timeout <= 0:
        raise ConfigError("http.timeout_seconds must be > 0")
    if not _is_number(backoff) or backoff < 0:
        raise ConfigError("http.backoff_base_seconds must be >= 0")

    factor = _get(h, "backoff_factor", "http")
    if not _is_number(factor) or factor < 1:
        raise ConfigError("http.backoff_factor must be >= 1")

    statuses = _get(h, "retry_statuses", "http")
    if not isinstance(statuses, list) or not all(_is_http_status(c) for c in statuses):
        raise ConfigError("http.retry_statuses must be a list of HTTP status codes")

    ratio = _get(q, "max_quarantine_ratio", "quality")
    if not _is_number(ratio) or not 0 < ratio < 1:
        raise ConfigError("quality.max_quarantine_ratio must be in (0, 1)")

    min_rows = _get(q, "min_rows_for_threshold", "quality")
    if not _is_int(min_rows) or min_rows < 1:
        raise ConfigError("quality.min_rows_for_threshold must be an integer >= 1")

    reference = _parse_reference(_section(data, "reference")) if "reference" in data else None
    database = _parse_database(_section(data, "database")) if "database" in data else None

    return Config(
        source=SourceConfig(
            url=str(_get(s, "url", "source")),
            agency=str(_get(s, "agency", "source")),
            complaint_types=_str_tuple(_get(s, "complaint_types", "source"), "source.complaint_types"),
            columns=_str_tuple(_get(s, "columns", "source"), "source.columns"),
            start_date=start,
            end_date=end,
            page_size=page_size,
            app_token_env=str(_get(s, "app_token_env", "source")),
            timezone=_zone(_get(s, "timezone", "source")),
        ),
        http=HttpConfig(
            timeout_seconds=float(timeout),
            max_retries=retries,
            backoff_base_seconds=float(backoff),
            backoff_factor=float(factor),
            retry_statuses=frozenset(statuses),
        ),
        paths=PathsConfig(
            raw_dir=Path(_get(p, "raw_dir", "paths")),
            curated_dir=Path(_get(p, "curated_dir", "paths")),
            quarantine_dir=Path(_get(p, "quarantine_dir", "paths")),
            manifest_path=Path(_get(p, "manifest_path", "paths")),
        ),
        quality=QualityConfig(float(ratio), min_rows),
        reference=reference,
        database=database,
    )


def load_config(path: str | Path) -> Config:
    path = Path(path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    return parse_config(data)