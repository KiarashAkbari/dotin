"""Cleaning and enrichment of records that passed the dataset contract."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .contract import (
    CLOSED_COLUMN,
    CREATED_COLUMN,
    DATETIME_FORMAT,
    UNIQUE_KEY,
    ZIP_COLUMN,
)
from .errors import DataError
from .population import ZipArea
from .quality import ClassifiedRecord
from .timeutil import closed_duration, possible_instants

OUTPUT_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
PASSTHROUGH_COLUMNS = (
    "agency",
    "complaint_type",
    "descriptor",
    "status",
    "borough",
    "community_board",
)
CLEAN_COLUMNS = (
    UNIQUE_KEY,
    "created_at",
    "closed_at",
    "duration_seconds",
    "is_ambiguous",
    *PASSTHROUGH_COLUMNS,
    ZIP_COLUMN,
    "modzcta",
    "population",
)


@dataclass
class JoinStats:
    """Row accounting: matched + missing_zip + unmatched_zip == rows_out."""

    rows_in: int = 0
    rows_out: int = 0
    matched: int = 0
    missing_zip: int = 0
    unmatched_zip: int = 0
    null_population: int = 0
    ambiguous: int = 0
    max_created_at: str | None = None
    keys_at_max_created: list[str] = field(default_factory=list)
    unmatched_by_zip: Counter[str] = field(default_factory=Counter)
    borough_by_modzcta: dict[str, set[str]] = field(default_factory=dict)


def _text(value: object) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def clean_record(
    row: Mapping[str, object],
    zone: ZoneInfo,
    zip_index: Mapping[str, ZipArea],
    stats: JoinStats,
) -> dict[str, object]:
    """Type, derive and enrich one record that already passed validation.

    Missing-value policy: a missing ZIP keeps the row with empty area and
    population; a ZIP absent from the reference keeps the row and is counted;
    an open case keeps empty closed_at and duration.
    """
    created = datetime.strptime(_text(row[CREATED_COLUMN]), DATETIME_FORMAT)
    created_text = created.strftime(OUTPUT_DATETIME_FORMAT)
    row_key = str(row[UNIQUE_KEY]).strip()
    if stats.max_created_at is None or created_text > stats.max_created_at:
        stats.max_created_at = created_text
        stats.keys_at_max_created = [row_key]
    elif created_text == stats.max_created_at:
        stats.keys_at_max_created.append(row_key)

    closed_source = _text(row.get(CLOSED_COLUMN))
    closed = datetime.strptime(closed_source, DATETIME_FORMAT) if closed_source else None

    duration: int | None = None
    if closed is not None:
        result = closed_duration(created, closed, zone)
        if result.seconds is None:
            raise DataError(
                "Record violates closing order after validation",
                context={"unique_key": row.get(UNIQUE_KEY)},
            )
        duration, ambiguous = result.seconds, result.is_ambiguous
    else:
        ambiguous = len(possible_instants(created, zone)) > 1
    stats.ambiguous += ambiguous

    zip_code = _text(row.get(ZIP_COLUMN))
    area = zip_index.get(zip_code) if zip_code else None
    if zip_code is None:
        stats.missing_zip += 1
    elif area is None:
        stats.unmatched_zip += 1
        stats.unmatched_by_zip[zip_code] += 1
    else:
        stats.matched += 1
        stats.null_population += area.population is None
        borough = _text(row.get("borough"))
        if borough:
            stats.borough_by_modzcta.setdefault(area.modzcta, set()).add(borough)

    cleaned: dict[str, object] = {
        UNIQUE_KEY: str(row[UNIQUE_KEY]).strip(),
        "created_at": created_text,
        "closed_at": closed.strftime(OUTPUT_DATETIME_FORMAT) if closed else None,
        "duration_seconds": duration,
        "is_ambiguous": ambiguous,
        ZIP_COLUMN: zip_code,
        "modzcta": area.modzcta if area else None,
        "population": area.population if area else None,
    }
    for column in PASSTHROUGH_COLUMNS:
        cleaned[column] = _text(row.get(column))
    return {column: cleaned[column] for column in CLEAN_COLUMNS}


def clean_stream(
    records: Iterable[ClassifiedRecord],
    zone: ZoneInfo,
    zip_index: Mapping[str, ZipArea],
    stats: JoinStats,
) -> Iterator[dict[str, object]]:
    """Lazily clean the records that passed validation; quarantined ones are skipped."""
    for record in records:
        if not record.is_valid:
            continue
        stats.rows_in += 1
        cleaned = clean_record(record.row, zone, zip_index, stats)
        stats.rows_out += 1
        yield cleaned