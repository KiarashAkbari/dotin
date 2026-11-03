"""Time helpers for naive local timestamps that cross daylight-saving changes."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

FOLDS = (0, 1)


@dataclass(frozen=True)
class DurationResult:
    seconds: int | None
    is_ambiguous: bool


def possible_instants(local: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    """All distinct UTC instants a naive local time can refer to (1 or 2).

    The repeated autumn hour and the skipped spring hour both yield two
    instants, one per fold; ordinary times yield one.
    """
    candidates = {
        local.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc) for fold in FOLDS
    }
    return tuple(sorted(candidates))


def closed_duration(created: datetime, closed: datetime, zone: ZoneInfo) -> DurationResult:
    """Smallest non-negative duration over all interpretations, or None if none exists."""
    created_options = possible_instants(created, zone)
    closed_options = possible_instants(closed, zone)
    durations = [
        int((end - start).total_seconds())
        for start in created_options
        for end in closed_options
    ]
    non_negative = [d for d in durations if d >= 0]
    ambiguous = len(created_options) > 1 or len(closed_options) > 1
    return DurationResult(min(non_negative) if non_negative else None, ambiguous)