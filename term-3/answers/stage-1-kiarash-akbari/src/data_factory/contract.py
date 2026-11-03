"""Dataset contract: what a valid case record looks like and why one is rejected."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

UNIQUE_KEY = "unique_key"
CREATED_COLUMN = "created_date"
CLOSED_COLUMN = "closed_date"
STATUS_COLUMN = "status"
ZIP_COLUMN = "incident_zip"

DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"
ZIP_PATTERN = r"(?!0{5})\d{5}"
CLOSED_STATUS = "Closed"
BOROUGHS = frozenset(
    {"BRONX", "BROOKLYN", "MANHATTAN", "QUEENS", "STATEN ISLAND", "Unspecified"}
)


class ReasonCode(str, Enum):
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    UNPARSEABLE_DATE = "UNPARSEABLE_DATE"
    CLOSED_BEFORE_CREATED = "CLOSED_BEFORE_CREATED"
    CLOSED_WITHOUT_CLOSED_DATE = "CLOSED_WITHOUT_CLOSED_DATE"
    BAD_ZIP_FORMAT = "BAD_ZIP_FORMAT"
    VALUE_OUT_OF_DOMAIN = "VALUE_OUT_OF_DOMAIN"
    DUPLICATE_UNIQUE_KEY = "DUPLICATE_UNIQUE_KEY"


class ColumnType(str, Enum):
    STRING = "string"
    DATETIME = "datetime"


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    type: ColumnType
    nullable: bool
    allowed_values: frozenset[str] | None = None


CONTRACT: tuple[ColumnSpec, ...] = (
    ColumnSpec(UNIQUE_KEY, ColumnType.STRING, nullable=False),
    ColumnSpec(CREATED_COLUMN, ColumnType.DATETIME, nullable=False),
    ColumnSpec(CLOSED_COLUMN, ColumnType.DATETIME, nullable=True),
    ColumnSpec("agency", ColumnType.STRING, nullable=False),
    ColumnSpec("complaint_type", ColumnType.STRING, nullable=False),
    ColumnSpec("descriptor", ColumnType.STRING, nullable=True),
    ColumnSpec(STATUS_COLUMN, ColumnType.STRING, nullable=False),
    # borough stays required: the source writes "Unspecified" for unknown, never null,
    # and every grouping in the analysis uses it
    ColumnSpec("borough", ColumnType.STRING, nullable=False, allowed_values=BOROUGHS),
    ColumnSpec(ZIP_COLUMN, ColumnType.STRING, nullable=True),
    ColumnSpec("community_board", ColumnType.STRING, nullable=True),
)