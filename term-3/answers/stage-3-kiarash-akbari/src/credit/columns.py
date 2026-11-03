"""Column contract for the credit file, and the one place its names are translated.

Two properties of this dataset make it awkward to load, and both are handled
here so that the DDL, the loader and the notebook cannot drift apart:

* The header names contain dots and hyphens
  (``NumberOfTime30-59DaysPastDueNotWorse``), which are unusable as bare SQL
  identifiers. Rather than quoting an ugly name in every query, each column has
  an explicit snake_case database name, and the mapping is bidirectional.
* Three columns carry codes that look like measurements. They are declared
  here as *candidate* sentinels: the codes are measured against the real file
  (``sql/queries/credit_sentinel_spikes.sql``) before anything treats them as
  data-entry artifacts.

The header itself is a hard contract: a file whose header differs from
``CSV_COLUMNS`` fails with ``SchemaDriftError`` instead of quietly producing
empty columns, which is how stage 1 treats a changed API response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ID_COLUMN = ""  # the Kaggle file leaves its index column unnamed
TARGET_COLUMN = "SeriousDlqin2yrs"

#: the header this project loads; verified against the file on every run
CSV_COLUMNS: tuple[str, ...] = (
    ID_COLUMN,
    TARGET_COLUMN,
    "RevolvingUtilizationOfUnsecuredLines",
    "age",
    "NumberOfTime30-59DaysPastDueNotWorse",
    "DebtRatio",
    "MonthlyIncome",
    "NumberOfOpenCreditLinesAndLoans",
    "NumberOfTimes90DaysLate",
    "NumberRealEstateLoansOrLines",
    "NumberOfTime60-89DaysPastDueNotWorse",
    "NumberOfDependents",
)

FEATURE_COLUMNS: tuple[str, ...] = tuple(
    name for name in CSV_COLUMNS if name not in (ID_COLUMN, TARGET_COLUMN)
)

DB_SCHEMA = "credit"
DB_TABLE = "application"
DB_INPUT_VIEW = "v_model_input"
DB_MISSINGNESS_VIEW = "v_column_missingness"
DB_RUN_TABLE = "load_run"

#: columns whose extreme values are documented as codes rather than counts
SENTINEL_COLUMNS: tuple[str, ...] = (
    "NumberOfTime30-59DaysPastDueNotWorse",
    "NumberOfTimes90DaysLate",
    "NumberOfTime60-89DaysPastDueNotWorse",
)
#: candidate codes; the counts behind them are measured, not assumed
SENTINEL_CODES: frozenset[int] = frozenset({96, 98})

SQL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class CreditColumn:
    """One column of the file, as the loader and the database both see it.

    ``sql_type`` is ``double precision`` for every feature on purpose. The file
    writes some integral values with a decimal part, and PostgreSQL rejects
    ``NaN`` in ``numeric``; the loader maps blank and ``NaN`` text to NULL, which
    is also what makes missingness countable in SQL.
    """

    csv_name: str
    db_name: str
    sql_type: str
    nullable: bool
    #: require an integral value when parsing; only certain for id and label
    integral: bool = False


COLUMNS: tuple[CreditColumn, ...] = (
    CreditColumn(ID_COLUMN, "row_id", "bigint", nullable=False, integral=True),
    CreditColumn(TARGET_COLUMN, "serious_dlqin_2yrs", "smallint", nullable=False, integral=True),
    CreditColumn(
        "RevolvingUtilizationOfUnsecuredLines",
        "revolving_utilization_unsecured",
        "double precision",
        nullable=True,
    ),
    CreditColumn("age", "age", "double precision", nullable=True),
    CreditColumn(
        "NumberOfTime30-59DaysPastDueNotWorse", "num_late_30_59", "double precision", nullable=True
    ),
    CreditColumn("DebtRatio", "debt_ratio", "double precision", nullable=True),
    CreditColumn("MonthlyIncome", "monthly_income", "double precision", nullable=True),
    CreditColumn(
        "NumberOfOpenCreditLinesAndLoans",
        "num_open_credit_lines",
        "double precision",
        nullable=True,
    ),
    CreditColumn("NumberOfTimes90DaysLate", "num_late_90", "double precision", nullable=True),
    CreditColumn(
        "NumberRealEstateLoansOrLines",
        "num_real_estate_loans",
        "double precision",
        nullable=True,
    ),
    CreditColumn(
        "NumberOfTime60-89DaysPastDueNotWorse", "num_late_60_89", "double precision", nullable=True
    ),
    CreditColumn("NumberOfDependents", "num_dependents", "double precision", nullable=True),
)


def db_for(csv_name: str) -> str:
    """Database name for a header name; a clear error beats a KeyError mid-load."""
    for spec in COLUMNS:
        if spec.csv_name == csv_name:
            return spec.db_name
    raise KeyError(
        f"{csv_name!r} is not part of the credit contract; "
        f"known columns: {', '.join(spec.csv_name for spec in COLUMNS)}"
    )


def csv_for(db_name: str) -> str:
    """Header name for a database name, used when reporting to the reader."""
    for spec in COLUMNS:
        if spec.db_name == db_name:
            return spec.csv_name
    raise KeyError(f"{db_name!r} is not a column of {DB_SCHEMA}.{DB_TABLE}")


def as_records(row: dict[str, object]) -> dict[str, object]:
    """Rename a parsed file record to database names, leaving values untouched."""
    return {db_for(key): value for key, value in row.items()}


DB_COLUMNS: tuple[str, ...] = tuple(spec.db_name for spec in COLUMNS)
#: the ten model-facing columns, in the order the matrix is built
MODEL_FEATURE_COLUMNS: tuple[str, ...] = tuple(
    spec.db_name for spec in COLUMNS if spec.csv_name in FEATURE_COLUMNS
)
LATE_PAYMENT_COLUMNS: tuple[str, ...] = tuple(db_for(csv_name) for csv_name in SENTINEL_COLUMNS)

DERIVED_COLUMNS: tuple[str, ...] = (
    "utilization_capped",
    "income_missing",
)
#: what the notebook selects from the view, target last; explicit, never `SELECT *`,
#: so a column added to the view cannot silently change a model matrix
VIEW_TARGET_COLUMN = "serious_dlqin_2yrs"
VIEW_SELECT_COLUMNS: tuple[str, ...] = (
    "row_id",
    *MODEL_FEATURE_COLUMNS,
    *DERIVED_COLUMNS,
    VIEW_TARGET_COLUMN,
)
