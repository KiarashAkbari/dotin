"""Record-level validation against the dataset contract; pure, no I/O."""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from zoneinfo import ZoneInfo

from .contract import (
    CLOSED_COLUMN,
    CLOSED_STATUS,
    CONTRACT,
    CREATED_COLUMN,
    DATETIME_FORMAT,
    STATUS_COLUMN,
    ZIP_COLUMN,
    ZIP_PATTERN,
    ColumnType,
    ReasonCode,
)
from .timeutil import closed_duration

_ZIP_RE = re.compile(ZIP_PATTERN)


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _norm(value: object) -> str:
    return str(value).strip()


def validate_record(row: Mapping[str, object], zone: ZoneInfo) -> tuple[ReasonCode, ...]:
    """Return the reason codes a record violates; an empty tuple means it passes.

    Timestamps are naive local times in ``zone``. A closing time earlier than the
    creation time is only a violation when no interpretation of the ambiguous
    daylight-saving hour yields a non-negative duration.
    """
    reasons: list[ReasonCode] = []
    dates: dict[str, datetime] = {}

    for spec in CONTRACT:
        value = row.get(spec.name)
        if _is_blank(value):
            if not spec.nullable:
                reasons.append(ReasonCode.MISSING_REQUIRED_FIELD)
            continue
        text = _norm(value)
        if spec.type is ColumnType.DATETIME:
            try:
                dates[spec.name] = datetime.strptime(text, DATETIME_FORMAT)
            except ValueError:
                reasons.append(ReasonCode.UNPARSEABLE_DATE)
        elif spec.allowed_values is not None and text not in spec.allowed_values:
            reasons.append(ReasonCode.VALUE_OUT_OF_DOMAIN)

    zip_value = row.get(ZIP_COLUMN)
    if not _is_blank(zip_value) and not _ZIP_RE.fullmatch(_norm(zip_value)):
        reasons.append(ReasonCode.BAD_ZIP_FORMAT)

    created, closed = dates.get(CREATED_COLUMN), dates.get(CLOSED_COLUMN)
    if created is not None and closed is not None:
        if closed_duration(created, closed, zone).seconds is None:
            reasons.append(ReasonCode.CLOSED_BEFORE_CREATED)

    if _norm(row.get(STATUS_COLUMN)) == CLOSED_STATUS and _is_blank(row.get(CLOSED_COLUMN)):
        reasons.append(ReasonCode.CLOSED_WITHOUT_CLOSED_DATE)

    return tuple(dict.fromkeys(reasons))