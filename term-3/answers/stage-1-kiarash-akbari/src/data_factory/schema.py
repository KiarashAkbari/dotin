"""Detect changes in the shape of the source before they become silent empty columns."""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .contract import CLOSED_COLUMN, CONTRACT
from .errors import SchemaDriftError

NULLABLE_CHECK_MIN_ROWS = 1000
# an all-open page legitimately carries no closed_date
MAY_BE_ABSENT = frozenset({CLOSED_COLUMN})


def required_columns(columns: Iterable[str]) -> frozenset[str]:
    wanted = set(columns)
    return frozenset(spec.name for spec in CONTRACT if not spec.nullable and spec.name in wanted)


def nullable_columns(columns: Iterable[str]) -> frozenset[str]:
    wanted = set(columns)
    return frozenset(spec.name for spec in CONTRACT if spec.nullable and spec.name in wanted)


def check_page_schema(
    rows: list[Mapping[str, object]], columns: Iterable[str], *, offset: int | None = None
) -> None:
    """Fail loudly when a page does not look like the configured columns.

    Socrata omits null fields, so a column missing from some rows is normal;
    a required column missing from every row, an unexpected column or a
    non-text value means the source changed shape.
    """
    if not rows:
        return
    expected = tuple(columns)
    present: set[str] = set()
    for row in rows:
        for column, value in row.items():
            present.add(column)
            if value is not None and not isinstance(value, str):
                raise SchemaDriftError(
                    f"Column {column} changed type: expected text, got {type(value).__name__}",
                    context={"column": column, "offset": offset},
                )
    absent = required_columns(expected) - present
    if len(rows) >= NULLABLE_CHECK_MIN_ROWS:
        absent |= nullable_columns(expected) - MAY_BE_ABSENT - present
    missing = sorted(absent)
    if missing:
        raise SchemaDriftError(
            f"Required column(s) absent from every row of a page: {', '.join(missing)}",
            context={"columns": missing, "rows": len(rows), "offset": offset},
        )
    unexpected = sorted(present - set(expected))
    if unexpected:
        raise SchemaDriftError(
            f"Unexpected column(s) in the response: {', '.join(unexpected)}",
            context={"columns": unexpected, "offset": offset},
        )