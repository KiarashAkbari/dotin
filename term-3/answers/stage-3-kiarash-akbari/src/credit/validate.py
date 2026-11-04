"""Record-level checks for the credit file: reasons, not silent drops.

Stage 1's ``validate_record`` cannot be imported here because it encodes the
311 domain (dates, ZIP patterns, borough values). What stage 3 reuses is the
*vocabulary*: a record is never discarded quietly, it is rejected with a reason
code and lands in the quarantine file, and the counts go to the manifest. The
same ``QuarantineWriter`` and ``ClassifiedRecord`` are used, so a rejected credit
row has the same shape as a rejected case row.

What is deliberately **not** checked: any value range that has not been measured
against the real file. Stage 2 learned that the hard way - a plausible ``CHECK``
on ``closed_at >= created_at`` aborted a full load because the daylight-saving
hour repeats. So negative counts and out-of-range utilization are counted and
reported in the notebook, where a human decides, and only the label's own domain
is enforced here.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import Enum

from .columns import COLUMNS, CreditColumn

#: text the file may use for "not recorded", besides an empty field
MISSING_TEXT = frozenset({"", "na", "n/a", "nan", "null", "none", "-"})


class CreditReasonCode(str, Enum):
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    UNPARSEABLE_NUMBER = "UNPARSEABLE_NUMBER"
    NOT_INTEGRAL = "NOT_INTEGRAL"
    VALUE_OUT_OF_DOMAIN = "VALUE_OUT_OF_DOMAIN"
    DUPLICATE_ROW_ID = "DUPLICATE_ROW_ID"


@dataclass(frozen=True)
class ParsedRow:
    """A row with its reasons; ``record`` is only trustworthy when there are none."""

    reasons: tuple[CreditReasonCode, ...]
    record: dict[str, object]

    @property
    def is_valid(self) -> bool:
        return not self.reasons


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip().lower() in MISSING_TEXT


def _spec_by_csv_name(name: str) -> CreditColumn:
    for spec in COLUMNS:
        if spec.csv_name == name:
            return spec
    raise KeyError(f"no contract entry for column {name!r}")


def _parse_number(
    text: str, spec: CreditColumn
) -> tuple[object, CreditReasonCode | None]:
    """Coerce one field, or say why it cannot be coerced."""
    try:
        value = float(text)
    except ValueError:
        return None, CreditReasonCode.UNPARSEABLE_NUMBER
    if not math.isfinite(value):
        # inf/nan text would otherwise smuggle a non-value into the database
        return None, CreditReasonCode.UNPARSEABLE_NUMBER
    if spec.integral:
        if not value.is_integer():
            return None, CreditReasonCode.NOT_INTEGRAL
        if spec.sql_type == "smallint":
            if value not in (0.0, 1.0):
                return None, CreditReasonCode.VALUE_OUT_OF_DOMAIN
            return int(value), None
        return int(value), None
    return value, None


def parse_row(
    row: Mapping[str, object],
    *,
    seen_ids: set[int] | None = None,
    id_column: str = "row_id",
) -> ParsedRow:
    """Validate one raw record and return it under its database names.

    ``seen_ids`` enables duplicate detection; it is optional so the same function
    serves a single-row prediction path, where an identifier seen before is not a
    defect.
    """
    reasons: list[CreditReasonCode] = []
    record: dict[str, object] = {}
    for spec in COLUMNS:
        raw = row.get(spec.csv_name)
        if _is_missing(raw):
            if not spec.nullable:
                reasons.append(CreditReasonCode.MISSING_REQUIRED_FIELD)
            record[spec.db_name] = None
            continue
        value, reason = _parse_number(str(raw).strip(), spec)
        if reason is not None:
            reasons.append(reason)
            record[spec.db_name] = None
            continue
        record[spec.db_name] = value

    row_id = record.get(id_column)
    if seen_ids is not None and isinstance(row_id, int):
        if row_id in seen_ids:
            reasons.append(CreditReasonCode.DUPLICATE_ROW_ID)
        else:
            seen_ids.add(row_id)

    return ParsedRow(tuple(dict.fromkeys(reasons)), record)


def parse_stream(
    rows: Iterable[Mapping[str, object]],
    *,
    detect_duplicates: bool = True,
) -> Iterator[tuple[ParsedRow, Mapping[str, object]]]:
    """Parse a stream of raw records, tracking identifiers as it goes."""
    seen: set[int] | None = set() if detect_duplicates else None
    for row in rows:
        yield parse_row(row, seen_ids=seen), row


def count_reasons(parsed: Iterable[ParsedRow]) -> dict[str, int]:
    """Reason counts for the manifest, in declaration order, zeros included."""
    counts = {code.value: 0 for code in CreditReasonCode}
    for item in parsed:
        for reason in item.reasons:
            counts[reason.value] += 1
    return counts
