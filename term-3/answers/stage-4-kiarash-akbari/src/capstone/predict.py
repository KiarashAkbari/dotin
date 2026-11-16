"""The prediction entry point: the schema gate is real, the scoring is not (yet).

``04`` asks for one file, the same validation, the same preparation, a ranked list --
and a clear refusal when the schema is wrong, naming the file and the column. That
last part needs no model, so it is written now, on purpose: the most common way to
fail this requirement is to build the scoring path first and bolt the validation on at
the end, where it quietly becomes a second implementation of the main path.

The expected columns come from stage 2's DDL, not from a list copied into this file.
A hard-coded column list is how a schema change turns into a silent pass.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .audit import columns_from_ddl
from .config import CapstoneConfig
from .errors import ConfigError, InputError  # noqa: F401
from .pending import pending

#: the table a fresh-order file is expected to match
INPUT_TABLE = "orders"


def expected_columns(config: CapstoneConfig) -> list[str]:
    """The contract for an incoming order file, read from the DDL the database enforces."""
    ddl = config.features.sql_migrations_dir / "001_create_olist_schema.sql"
    if not ddl.is_file():
        # stage 4 has no DDL of its own until phase 4, so fall back to stage 2's
        ddl = config.root.parent / "stage-2-kiarash-akbari" / "sql" / "001_create_olist_schema.sql"
    if not ddl.is_file():
        raise ConfigError(
            "cannot say what a valid order file looks like: no 001_create_olist_schema.sql "
            f"under {config.features.sql_migrations_dir} or ../stage-2-kiarash-akbari/sql"
        )
    tables = columns_from_ddl(ddl)
    if INPUT_TABLE not in tables or not tables[INPUT_TABLE]:
        raise ConfigError(f"{ddl} has no olist.{INPUT_TABLE} table to take a schema from")
    return tables[INPUT_TABLE]


def read_header(path: Path) -> list[str]:
    """The first row of a file, with an error that names the file."""
    if not path.is_file():
        raise InputError(f"no input file at {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        try:
            header = next(csv.reader(handle))
        except StopIteration:
            raise InputError(f"{path.name} is empty; a header row is required") from None
    return [cell.strip().lower() for cell in header]


def check_schema(header: list[str], expected: list[str], *, origin: str) -> None:
    """Refuse a wrong header, naming the file and each offending column."""
    wanted, seen = set(expected), set(header)
    missing = sorted(wanted - seen)
    extra = sorted(seen - wanted)
    if not missing and not extra:
        return
    parts: list[str] = []
    if missing:
        parts.append(f"missing column(s): {', '.join(missing)}")
    if extra:
        parts.append(f"unexpected column(s): {', '.join(extra)}")
    if not header:
        parts.append("the header row is empty")
    raise InputError(
        f"{origin} is not an order file as the schema defines it -- " + "; ".join(parts),
        context={"file": str(origin), "missing": missing, "unexpected": extra},
    )


def validate_file(path: Path, config: CapstoneConfig) -> list[str]:
    """Run the gate a grader's sealed cut has to pass, and return the header."""
    header = read_header(path)
    check_schema(header, expected_columns(config), origin=str(path))
    return header


def score_file(path: Path, config: CapstoneConfig) -> list[dict]:
    """Validate the file, then hand it to the layer that is not written yet.

    The order is the point: a wrong schema is refused today, while a well-formed file
    reaches the placeholder and stops there. A grader's sealed cut therefore meets the
    same gate the training path meets, and ``10.3`` can test both halves without a model.
    """
    validate_file(path, config)
    return _score_rows(path, config)


@pending(phase=6, checklist=("8.1", "8.3"), step="predict")
def _score_rows(path: Path, config: CapstoneConfig) -> list[dict]:
    """Rank the rows of an already-validated file.

    Phase 6 owns the body, and it must reuse ``prep`` from the training path rather
    than re-implement it -- the one shortcut that turns this command into a second
    pipeline, which is what the brief scores as zero.
    """
    raise AssertionError("placeholder")


def refusal_reason(config: CapstoneConfig) -> str:
    """Why the entry point exists but does not score yet -- for the README and the doctor."""
    return (
        "the schema gate runs; the model behind it is ROADMAP phase 6 (checklist 8.1), so "
        f"{config.root / '02_system.ipynb'} cannot emit a ranked list yet"
    )
