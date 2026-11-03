"""Generated data quality report: per column null share, distinct values and range."""
from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

DISTINCT_CAP = 10_000
PERCENT = 100
PERCENT_DECIMALS = 4
REPORT_FILENAME = "data_quality_report.json"
TMP_SUFFIX = ".tmp"


class ColumnProfile:
    """Streaming statistics for one column; distinct counting is capped to bound memory."""

    def __init__(self) -> None:
        self.rows = 0
        self.nulls = 0
        self.minimum = None
        self.maximum = None
        self._distinct: set = set()
        self._overflow = False

    def add(self, value: object) -> None:
        self.rows += 1
        if value is None:
            self.nulls += 1
            return
        if not self._overflow:
            self._distinct.add(value)
            if len(self._distinct) > DISTINCT_CAP:
                self._distinct.clear()
                self._overflow = True
        if self.minimum is None or value < self.minimum:
            self.minimum = value
        if self.maximum is None or value > self.maximum:
            self.maximum = value

    def as_dict(self) -> dict:
        share = round(self.nulls / self.rows * PERCENT, PERCENT_DECIMALS) if self.rows else 0.0
        return {
            "rows": self.rows,
            "null_count": self.nulls,
            "null_percent": share,
            "distinct_values": None if self._overflow else len(self._distinct),
            "distinct_capped": self._overflow,
            "min": self.minimum,
            "max": self.maximum,
        }


class QualityProfile:
    def __init__(self, columns: Iterable[str]) -> None:
        self._profiles = {column: ColumnProfile() for column in columns}

    def add(self, row: Mapping[str, object]) -> None:
        for column, profile in self._profiles.items():
            profile.add(row[column])

    def as_dict(self) -> dict:
        return {
            "distinct_cap": DISTINCT_CAP,
            "columns": {name: p.as_dict() for name, p in self._profiles.items()},
        }


def write_profile(
    path: Path, profile: QualityProfile, extra: dict | None = None
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    payload = profile.as_dict()
    if extra:
        payload.update(extra)
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, sort_keys=True, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)