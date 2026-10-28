"""Final outputs: case-level dataset, wide area x complaint x month table, hashing."""
from __future__ import annotations

import csv
import hashlib
import os
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from datetime import date
from pathlib import Path

from .clean import CLEAN_COLUMNS
from .errors import DataError

DATASET_FILENAME = "cases.csv"
WIDE_FILENAME = "complaints_by_area_month.csv"
TMP_SUFFIX = ".tmp"
HASH_CHUNK_BYTES = 1 << 20
UNKNOWN_AREA = "UNKNOWN"
# The wide table's month columns are a contract: the database sink reads them
# back by these prefixes, so neither side may spell them out separately.
COUNT_PREFIX = "count_"
RATE_PREFIX = "rate_per_10k_"
MONTH_LENGTH = 7
RATE_BASE = 10_000
RATE_DECIMALS = 4
TRUE_TEXT = "true"
FALSE_TEXT = "false"


def months_between(start: date, end: date) -> tuple[str, ...]:
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return tuple(months)


def format_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return TRUE_TEXT if value else FALSE_TEXT
    return str(value)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DatasetWriter:
    """Streams cleaned rows to CSV; the file appears only if the block succeeds."""

    def __init__(self, path: Path) -> None:
        self._final = Path(path)
        self._tmp = self._final.with_name(self._final.name + TMP_SUFFIX)
        self._fh = None
        self._writer = None

    @property
    def path(self) -> Path:
        return self._final

    def __enter__(self) -> "DatasetWriter":
        self._final.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._tmp.open("w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._fh, lineterminator="\n")
        self._writer.writerow(CLEAN_COLUMNS)
        return self

    def write(self, row: Mapping[str, object]) -> None:
        self._writer.writerow([format_cell(row[column]) for column in CLEAN_COLUMNS])

    def __exit__(self, exc_type, exc, tb) -> None:
        fh, self._fh = self._fh, None
        if exc_type is not None:
            fh.close()
            self._tmp.unlink(missing_ok=True)
            return
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()
        os.replace(self._tmp, self._final)


def _rate(count: int, population: int | None) -> str:
    if not population:
        return ""
    return f"{count / population * RATE_BASE:.{RATE_DECIMALS}f}"


class AreaMonthAggregator:
    """Counts complaints per area, complaint type and month, then pivots to wide rows."""

    def __init__(self, months: Iterable[str]) -> None:
        self._months = tuple(months)
        self._month_set = frozenset(self._months)
        self._counts: dict[tuple[str, str, str], int] = defaultdict(int)
        self._population: dict[str, int | None] = {}

    @property
    def columns(self) -> tuple[str, ...]:
        return (
            "modzcta",
            "population",
            "complaint_type",
            *(f"{COUNT_PREFIX}{m}" for m in self._months),
            *(f"{RATE_PREFIX}{m}" for m in self._months),
        )

    def add(self, row: Mapping[str, object]) -> None:
        area = str(row["modzcta"]) if row["modzcta"] else UNKNOWN_AREA
        month = str(row["created_at"])[:MONTH_LENGTH]
        if month not in self._month_set:
            raise DataError(
                f"Record outside the configured window: {month}",
                context={"unique_key": row.get("unique_key"), "month": month},
            )
        self._counts[(area, str(row["complaint_type"]), month)] += 1
        self._population[area] = row["population"]

    def rows(self) -> Iterator[dict[str, object]]:
        pairs = sorted({(area, ctype) for area, ctype, _ in self._counts})
        for area, ctype in pairs:
            population = self._population.get(area)
            out: dict[str, object] = {
                "modzcta": area,
                "population": population,
                "complaint_type": ctype,
            }
            counts = {m: self._counts.get((area, ctype, m), 0) for m in self._months}
            for month, count in counts.items():
                out[f"{COUNT_PREFIX}{month}"] = count
            for month, count in counts.items():
                out[f"{RATE_PREFIX}{month}"] = _rate(count, population)
            yield out


def write_wide(path: Path, aggregator: AreaMonthAggregator) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    columns = aggregator.columns
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(columns)
        for row in aggregator.rows():
            writer.writerow([format_cell(row[column]) for column in columns])
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)