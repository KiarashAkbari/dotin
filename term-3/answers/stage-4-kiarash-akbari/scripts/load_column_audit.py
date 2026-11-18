#!/usr/bin/env python3
"""Render `docs/evidence/column_audit.csv` as SQL that fills `capstone.column_audit`.

The audit table is created by ``sql/001_capstone_schema.sql`` and read by the guards in
``sql/002_time_guard.sql``. Nothing in ``sql/`` writes it, on purpose: the rows are the
student's verdicts, and they live in the CSV that ``scripts/audit_columns.py`` witnessed. But a
guard that reads an empty table refuses everything -- ``capstone.require_allowed`` is fail-closed,
so 33 frame columns come back as "not audited" and both live tests error out for a reason that
has nothing to do with the feature frame. That is the shape the live run hit (see
``docs/evidence/live-guards-dotin-run.txt``, section 6).

So this script closes the loop the way the rest of the stage does it: it prints SQL, and the
person holding the database runs it. It never connects, because a script that generates a
statement and then executes its own output has no second opinion left.

    python3 scripts/load_column_audit.py | psql "$CAPSTONE_DATABASE_URI" -v ON_ERROR_STOP=1 -f -

``\\copy`` from ``psql`` does the same job in one line and is what the student's run used; it is
kept here as the alternative because it needs no Python on the database host::

    \\copy capstone.column_audit ("table","column",available_at,at_approval,source,verdict) \
        from 'docs/evidence/column_audit.csv' with (format csv, header true)

The two differ in one respect worth knowing: ``\\copy`` reads the file as psql sees it (relative
to *psql's* working directory) and inserts blindly, while this script parses the CSV through
``capstone.audit`` -- so a row that contradicts its own verdict is refused before any SQL comes
out -- and it replaces the table's content instead of appending to it.

Truncating is deliberate. The CSV is the whole truth about the audit, so a load that leaves
yesterday's rows behind can make a forbidden column look allowed by accident, which is the one
failure this table exists to prevent. It is one transaction, so a database that dies halfway
keeps the state it had, and running it twice is the same as running it once -- the same
idempotency the ``sql/`` directory is held to by
``tests/test_capstone_sql_guards.py::LiveSqlContractTests::test_reapplying_the_directory_changes_nothing``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

STAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_ROOT / "src"))

from capstone.errors import CapstoneError  # noqa: E402
from capstone.exit_codes import ExitCode  # noqa: E402
from capstone.audit import (  # noqa: E402
    COLUMN_AUDIT_TABLE,
    ColumnVerdict,
    audit_problems,
    load_audit,
    render_load,
)

DEFAULT_CSV = STAGE_ROOT / "docs" / "evidence" / "column_audit.csv"
#: kept as aliases so a caller that already imports this tool keeps working; the generators live in
#: ``capstone.audit`` now, because ``capstone audit --load`` has to run the *same* load through a
#: connection and a second emitter would be a second opinion nobody asked for
TARGET = COLUMN_AUDIT_TABLE


def render(rows: list[ColumnVerdict]) -> str:
    """The load, as one transaction. ``capstone.audit.render_load`` is the implementation."""
    return render_load(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="the audit table in CSV form")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the SQL here instead of stdout (the file is not tracked; the CSV is)",
    )
    args = parser.parse_args(argv)

    try:
        rows = load_audit(args.csv)
    except (CapstoneError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return int(ExitCode.CONFIG if not isinstance(error, CapstoneError) else error.exit_code)

    problems = audit_problems(rows)
    if problems:
        print(
            f"error: {args.csv.name} is not internally consistent; refusing to emit SQL that a"
            " guard would then trust",
            file=sys.stderr,
        )
        for line in problems:
            print(f"  - {line}", file=sys.stderr)
        return int(ExitCode.DATA)
    if not rows:
        print(f"error: {args.csv} has no rows; the guard would refuse every column", file=sys.stderr)
        return int(ExitCode.DATA)

    sql = render(rows)
    if args.out is None:
        sys.stdout.write(sql)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(sql, encoding="utf-8")
        print(f"wrote {len(rows)} rows to {args.out}", file=sys.stderr)
    return int(ExitCode.OK)


if __name__ == "__main__":
    raise SystemExit(main())
