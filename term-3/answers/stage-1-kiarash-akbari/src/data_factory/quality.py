"""Streaming classification of records into passed and quarantined."""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from .contract import UNIQUE_KEY, ReasonCode
from .errors import DataError, QuarantineThresholdError
from .validate import validate_record


@dataclass(frozen=True)
class ClassifiedRecord:
    row: Mapping[str, object]
    reasons: tuple[ReasonCode, ...]

    @property
    def is_valid(self) -> bool:
        return not self.reasons


@dataclass
class QualityReport:
    total: int = 0
    passed: int = 0
    quarantined: int = 0
    by_reason: Counter[ReasonCode] = field(default_factory=Counter)

    def add(self, reasons: tuple[ReasonCode, ...]) -> None:
        self.total += 1
        if reasons:
            self.quarantined += 1
            self.by_reason.update(reasons)
        else:
            self.passed += 1

    @property
    def quarantine_ratio(self) -> float:
        return self.quarantined / self.total if self.total else 0.0


def classify_records(
    rows: Iterable[Mapping[str, object]], report: QualityReport, zone: ZoneInfo
) -> Iterator[ClassifiedRecord]:
    """Lazily classify rows; the report is updated as the stream is consumed."""
    seen_keys: set[str] = set()
    for row in rows:
        reasons = list(validate_record(row, zone))
        raw_key = row.get(UNIQUE_KEY)
        key = "" if raw_key is None else str(raw_key).strip()
        if key and key in seen_keys:
            reasons.append(ReasonCode.DUPLICATE_UNIQUE_KEY)
        elif key and not reasons:
            seen_keys.add(key)
        result = ClassifiedRecord(row, tuple(reasons))
        report.add(result.reasons)
        yield result


def enforce_threshold(
    report: QualityReport, *, max_ratio: float, min_rows: int, allow_empty: bool = False
) -> None:
    """Fail the run when too large a share of the data was quarantined.

    An empty input fails a full run (an empty window means the scope or the source
    is wrong) but not an increment: "nothing was created since the last run" is a
    normal answer for --since-last-run, so allow_empty lets it publish zero rows.
    """
    if report.total == 0 and allow_empty:
        return
    if report.total == 0:
        raise DataError(
            "No records to process; refusing to publish empty outputs",
            context={"total": 0},
        )
    nothing_passed = report.passed == 0
    over_limit = report.total >= min_rows and report.quarantine_ratio > max_ratio
    if nothing_passed or over_limit:
        raise QuarantineThresholdError(
            f"Quarantined {report.quarantined} of {report.total} records "
            f"({report.quarantine_ratio:.4%}); limit is {max_ratio:.4%}",
            context={
                "total": report.total,
                "quarantined": report.quarantined,
                "ratio": report.quarantine_ratio,
                "max_ratio": max_ratio,
                "min_rows": min_rows,
                "by_reason": {c.value: n for c, n in report.by_reason.items()},
            },
        )