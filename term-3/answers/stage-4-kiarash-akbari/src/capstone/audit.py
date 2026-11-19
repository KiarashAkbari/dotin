"""The column time audit, as a file the pipeline can be held to.

``04`` asks for a table that says, for every column handed to the model, at which
moment of an order's life it becomes available, whether it already existed at
``order_approved_at``, and what the certainty of that claim rests on. It then adds
the sentence that gives this section its weight: *a column that is not in that table
must not be in the model.* Prose cannot enforce that, so the table is a CSV and this
module is the enforcement: the audit file is an input to the feature layer and to the
anti-leak test (checklist 4.1, 4.2, 10.3), not a paragraph in the report.

Nothing here measures anything. Reading a verdict is cheap; producing one needs the
three witnesses ROADMAP phase 2 defines, which ``scripts/audit_columns.py`` prints.

The header is fixed, and the order of columns is the order ``04``'s table shows::

    table,column,available_at,at_approval,source,verdict

``verdict`` is one of ``allowed`` / ``forbidden`` / ``unverified``. ``unverified`` is
the honest default and it is never allowed: an unexamined column cannot enter the
model, and an ``allowed`` row without a ``source`` is treated as unverified too.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .errors import AuditError, InputError

if TYPE_CHECKING:  # only an annotation below; config imports nothing from here, but keeping the
    from .config import CapstoneConfig  # edge one-way costs nothing and stops a cycle forming

AUDIT_HEADER = ("table", "column", "available_at", "at_approval", "source", "verdict")
VERDICTS = ("allowed", "forbidden", "unverified")
TRUE_WORDS = ("true", "yes")
FALSE_WORDS = ("false", "no")
#: the nine tables the capstone reads, from stage-2's DDL
OLIST_TABLES = (
    "customers",
    "geolocation",
    "order_items",
    "order_reviews",
    "orders",
    "payments",
    "product_category_name_translation",
    "products",
    "sellers",
)


@dataclass(frozen=True)
class ColumnVerdict:
    """One row of the audit table."""

    table: str
    column: str
    available_at: str
    at_approval: bool | None
    source: str
    verdict: str

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"

    @property
    def usable(self) -> bool:
        """Allowed, present at the decision moment, and defended by a source."""
        return self.verdict == "allowed" and self.at_approval is True and bool(self.source)


def _flag(raw: str) -> bool | None:
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value in TRUE_WORDS:
        return True
    if value in FALSE_WORDS:
        return False
    return None


def parse_audit(text: str, *, origin: str = "column_audit.csv") -> list[ColumnVerdict]:
    """Read audit rows from text, and refuse a file that is not the contract."""
    rows = list(csv.reader(text.splitlines()))
    if not rows or not any(rows[0]):
        raise AuditError(
            f"{origin} is empty; the audit has to carry one row per column the model sees "
            "(checklist 4.1)",
            context={"path": origin},
        )
    header = tuple(cell.strip().lower() for cell in rows[0])
    if header != AUDIT_HEADER:
        raise AuditError(
            f"{origin} header is {','.join(header)!r}; expected {','.join(AUDIT_HEADER)!r}",
            context={"path": origin, "expected": list(AUDIT_HEADER), "found": list(header)},
        )
    verdicts: list[ColumnVerdict] = []
    for position, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        cells = [cell.strip() for cell in row]
        if len(cells) != len(AUDIT_HEADER):
            raise AuditError(
                f"{origin} line {position} has {len(cells)} fields; the header has "
                f"{len(AUDIT_HEADER)}, and a hand-edited row that loses one would silently "
                "move a verdict into the source column",
                context={"path": origin, "line": position, "fields": cells},
            )
        table, column, available_at, at_approval, source, verdict = cells
        lowered = verdict.lower()
        if lowered not in VERDICTS:
            raise AuditError(
                f"{origin} line {position}: verdict {verdict!r} is not one of "
                f"{', '.join(VERDICTS)}",
                context={"path": origin, "line": position},
            )
        verdicts.append(
            ColumnVerdict(
                table=table.lower(),
                column=column.lower(),
                available_at=available_at,
                at_approval=_flag(at_approval),
                source=source,
                verdict=lowered,
            )
        )
    return verdicts


def load_audit(path: Path) -> list[ColumnVerdict]:
    """Read the audit file, with an error that says who is supposed to write it."""
    if not path.is_file():
        raise AuditError(
            f"no column audit at {path}. ROADMAP phase 2 writes it; until then the feature "
            "layer has no permission to use a single column (checklist 4.1)",
            context={"path": str(path)},
        )
    return parse_audit(path.read_text(encoding="utf-8"), origin=str(path))


def audit_problems(audit: list[ColumnVerdict]) -> list[str]:
    """Every self-contradiction in the table, as one line each.

    These are the mistakes that make an audit look complete and be worthless: a row
    claiming a column was available before approval, a verdict with no source behind
    it, the same column audited twice with different answers.
    """
    problems: list[str] = []
    seen: dict[str, list[ColumnVerdict]] = {}
    for row in audit:
        seen.setdefault(row.key, []).append(row)
        if row.verdict == "allowed" and not row.source:
            problems.append(
                f"{row.key}: allowed without a source; the brief asks what the claim rests on"
            )
        if row.verdict == "allowed" and row.at_approval is not True:
            problems.append(
                f"{row.key}: allowed but at_approval is "
                f"{'blank' if row.at_approval is None else 'false'}"
            )
        if row.at_approval is True and not row.available_at:
            problems.append(f"{row.key}: claims availability with no moment named")
    for key, rows in seen.items():
        if len(rows) < 2:
            continue
        if len({(row.verdict, row.at_approval) for row in rows}) > 1:
            problems.append(f"{key}: audited {len(rows)} times with contradicting verdicts")
    return problems


def allowed_columns(audit: list[ColumnVerdict]) -> frozenset[str]:
    """The allowlist the feature layer is measured against."""
    problems = set(audit_problems(audit))
    blamed = {row.key for row in audit for problem in problems if problem.startswith(row.key)}
    return frozenset(
        row.key
        for row in audit
        if row.usable and row.key not in blamed and row.verdict != "forbidden"
    )


def require_allowed(audit: list[ColumnVerdict], used: list[str]) -> None:
    """Refuse any column the audit does not allow, naming each one.

    ``used`` holds ``table.column`` keys, in the order the feature frame was built,
    so the error message points at the offending select list instead of a count.
    """
    permitted = allowed_columns(audit)
    by_key = {row.key: row for row in audit}
    offenders: list[str] = []
    for key in used:
        row = by_key.get(key)
        if key in permitted:
            continue
        if row is None:
            offenders.append(f"{key}: not in the audit at all")
        elif row.verdict == "forbidden":
            offenders.append(f"{key}: audited and forbidden ({row.available_at})")
        elif row.verdict == "unverified":
            offenders.append(f"{key}: unverified, and an unexamined column is not evidence")
        else:
            offenders.append(f"{key}: audited but not usable (at_approval={row.at_approval!r})")
    if offenders:
        raise AuditError(
            "the column audit rejects this feature frame: " + "; ".join(offenders),
            context={"offenders": offenders, "allowed": sorted(permitted)},
        )


def coverage(audit: list[ColumnVerdict], ddl_columns: dict[str, list[str]]) -> dict[str, int]:
    """How much of the schema the audit actually covers, per table.

    ``04`` scopes the table to the columns handed to the model, while ROADMAP phase 2
    widens it to every column of the nine tables. This function makes the difference
    visible instead of argued: it reports the gap between the DDL and the file.
    """
    audited = {row.key for row in audit}
    missing: dict[str, int] = {}
    for table, columns in ddl_columns.items():
        gap = [f"{table}.{column}" for column in columns if f"{table}.{column}" not in audited]
        if gap:
            missing[table] = len(gap)
    return missing


def summarise(audit: list[ColumnVerdict]) -> dict[str, object]:
    """Counts the report quotes, so no one retypes them from a spreadsheet."""
    verdicts = Counter(row.verdict for row in audit)
    return {
        "rows": len(audit),
        "tables": len({row.table for row in audit}),
        "allowed": verdicts["allowed"],
        "forbidden": verdicts["forbidden"],
        "unverified": verdicts["unverified"],
        "usable": sum(1 for row in audit if row.usable),
        "problems": len(audit_problems(audit)),
    }


def columns_from_ddl(sql_path: Path) -> dict[str, list[str]]:
    """The column list of each ``olist`` table, read out of stage 2's DDL.

    The DDL is the only place in this repo where the nine tables are described with
    certainty, so coverage is checked against it and not against memory.
    """
    if not sql_path.is_file():
        raise AuditError(
            f"no DDL at {sql_path}; the audit's denominator comes from stage-2/sql",
            context={"path": str(sql_path)},
        )
    tables: dict[str, list[str]] = {}
    current: str | None = None
    depth = 0
    for line in sql_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("CREATE TABLE"):
            name = stripped.split("(")[0].split()[-1].strip()
            current = name.split(".")[-1] if name.startswith("olist.") else None
            depth = 0
            if current:
                tables.setdefault(current, [])
            continue
        if current is None:
            continue
        if stripped.startswith(")"):
            current = None
            continue
        if not stripped or stripped.startswith("--"):
            continue
        # a constraint's CHECK expression spans lines, and its body looks exactly like
        # a column definition to a line-based reader; only depth-0 lines are columns
        if stripped.startswith("CONSTRAINT") or depth > 0:
            depth += stripped.count("(") - stripped.count(")")
            continue
        depth += stripped.count("(") - stripped.count(")")
        words = stripped.split()
        if len(words) < 2 or words[1].upper() in {"PRIMARY", "FOREIGN", "CHECK", "UNIQUE"}:
            continue
        if words[0].isidentifier() and words[0].islower():
            tables[current].append(words[0])
    return tables


def template_rows(ddl_columns: dict[str, list[str]]) -> str:
    """One ``unverified`` row per column of the DDL, ready for phase 2 to defend.

    Generating the shell is not the same as doing the audit: every row this writes
    says ``unverified`` and carries no source, so a generated file cannot make a
    single column usable by accident. It exists so the denominator -- ``04``'s
    completeness rule -- is a file people edit rather than a spreadsheet nobody
    remembers to finish.
    """
    lines = [",".join(AUDIT_HEADER)]
    for table in sorted(ddl_columns):
        for column in ddl_columns[table]:
            lines.append(f"{table},{column},,,,unverified")
    return "\n".join(lines) + "\n"


#: the table the audit is mirrored into, exactly as ``sql/001_capstone_schema.sql`` declares it.
#: every identifier is quoted because two of the six names, ``table`` and ``column``, are reserved
#: close enough that an unquoted list is a syntax error -- and half-quoting is a worse trap than
#: quoting everything
COLUMN_AUDIT_TABLE = "capstone.column_audit"
#: the load order: the CSV header, which is the DDL's column order too
AUDIT_COLUMNS = AUDIT_HEADER
QUOTED_AUDIT_COLUMNS = ", ".join(f'"{field}"' for field in AUDIT_COLUMNS)
#: the row the guard consults, read back so a load can say what it changed instead of asserting it
READ_STATEMENT = f"SELECT {QUOTED_AUDIT_COLUMNS} FROM {COLUMN_AUDIT_TABLE}"
RESET_STATEMENT = f"TRUNCATE TABLE {COLUMN_AUDIT_TABLE}"
#: one row per execute, bound rather than inlined. ``render_load`` below emits the same rows as
#: literals because a psql script has no parameter mechanism: two frontends, one row set, and the
#: test that compares them is what keeps them one row set.
LOAD_STATEMENT = (
    f"INSERT INTO {COLUMN_AUDIT_TABLE} ({QUOTED_AUDIT_COLUMNS}) VALUES"
    f" ({', '.join(['%s'] * len(AUDIT_COLUMNS))})"
)
#: and the question ``features.build`` asks before it lets the guard run. An empty audit table is
#: not a verdict, it is a missing prerequisite, and the two have different remedies -- reading it as
#: one exception type is how "the frame leaks" gets reported when the real finding is "nobody loaded
#: the CSV".
AUDIT_COUNT_STATEMENT = f"SELECT count(*) FROM {COLUMN_AUDIT_TABLE}"


def sql_quote(value: str) -> str:
    """A SQL string literal, single quotes doubled. Nothing here needs a heavier escape."""
    return "'" + value.replace("'", "''") + "'"


def load_value(row: ColumnVerdict, field: str) -> object:
    """One field of one row, as a bind parameter.

    ``at_approval`` is the only non-text column and the only nullable one: an ``unverified`` row
    claims nothing about approval, and turning that into ``false`` would invent a verdict somebody
    could act on.
    """
    if field == "at_approval":
        return row.at_approval
    return str(getattr(row, field))


def load_values(rows: list[ColumnVerdict]) -> list[tuple]:
    """The rows to hand to the database, ordered by (table, column) so the run is reviewable."""
    return [
        tuple(load_value(row, field) for field in AUDIT_COLUMNS)
        for row in sorted(rows, key=lambda row: (row.table, row.column))
    ]


def render_load(rows: list[ColumnVerdict]) -> str:
    """The whole load as one psql script: truncate, insert, report, commit.

    Truncating is deliberate, and it is the half an upsert cannot do. The CSV is the entire truth
    about the audit; a column a student deletes from it must disappear from the table too, because a
    row that lingers says "allowed" about something nobody audited any more. ``ON CONFLICT DO
    UPDATE`` fixes the rows the CSV still carries and silently keeps the ones it dropped -- the one
    failure this table exists to prevent -- so the load replaces instead of merging, and running it
    twice is the same as running it once.
    """
    ordered = sorted(rows, key=lambda row: (row.table, row.column))
    values = ",\n".join(
        "    (" + ", ".join(sql_literal(row, field) for field in AUDIT_COLUMNS) + ")"
        for row in ordered
    )
    return (
        "-- generated by scripts/load_column_audit.py via capstone.audit.render_load;\n"
        "-- the source of truth is the CSV it reads\n"
        "BEGIN;\n\n"
        f"{RESET_STATEMENT};\n\n"
        f"INSERT INTO {COLUMN_AUDIT_TABLE} ({QUOTED_AUDIT_COLUMNS})\nVALUES\n"
        f"{values};\n\n"
        "-- the count is printed rather than assumed, so the load says what it put in\n"
        f"SELECT '{COLUMN_AUDIT_TABLE} loaded: ' || count(*)"
        " || ' rows, ' || count(*) FILTER (WHERE verdict = 'allowed') || ' allowed'"
        " || ', ' || count(*) FILTER (WHERE verdict = 'forbidden') || ' forbidden'"
        " || ', ' || count(*) FILTER (WHERE verdict = 'unverified') || ' unverified'\n"
        f"FROM {COLUMN_AUDIT_TABLE};\n\n"
        "COMMIT;\n"
    )


def sql_literal(row: ColumnVerdict, field: str) -> str:
    value = load_value(row, field)
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    return sql_quote(str(value))


def read_audit_table(cursor: Any) -> dict[tuple[str, str], tuple]:
    """The table as it stands, keyed by (table, column), for the before/after comparison."""
    cursor.execute(READ_STATEMENT)
    return {
        (str(row[0]), str(row[1])): tuple(row[2:]) for row in cursor.fetchall()
    }


def diff_audit(
    before: dict[tuple[str, str], tuple], after: dict[tuple[str, str], tuple]
) -> dict[str, int]:
    """What the load actually did, from the two snapshots rather than from a row count.

    ``psql`` cannot tell an overwritten row from an identical re-insert with ``ROW_COUNT()`` alone,
    and a report that guesses is worse than no report: the numbers here are the reason a reviewer
    can notice that a CSV edit never reached the database.
    """
    inserted = [key for key in after if key not in before]
    removed = [key for key in before if key not in after]
    rewritten = [key for key in after if key in before and after[key] != before[key]]
    unchanged = [key for key in after if key in before and after[key] == before[key]]
    return {
        "inserted": len(inserted),
        "rewritten": len(rewritten),
        "unchanged": len(unchanged),
        "removed": len(removed),
        "changed_keys": sorted(f"{table}.{column}" for table, column in (*inserted, *rewritten, *removed)),
    }


def load_from_csv(
    config: CapstoneConfig,
    *,
    uri: str | None = None,
    connect: Callable[[str], Any] | None = None,
    csv_path: Path | None = None,
) -> dict:
    """Mirror ``column_audit.csv`` into ``capstone.column_audit`` and report the delta.

    The same validation the file emitter runs before it prints anything: a row that contradicts its
    own verdict is refused *here* too, because the guard downstream treats the table as settled
    truth. The write is one transaction, so a database that dies halfway keeps the state it had.
    """
    from .features import _connect, database_uri  # late: features imports this module

    path = csv_path or config.audit_path
    if not path.is_file():
        raise InputError(
            f"no audit file at {path}; it is the input, not an output -- scripts/audit_columns.py"
            " prints the DDL columns and the review fills the verdicts in"
        )
    rows = load_audit(path)
    problems = audit_problems(rows)
    if problems:
        raise AuditError(
            f"{path.name} is not internally consistent, so it is not safe to mirror into the table"
            " the guard trusts: " + "; ".join(problems),
            context={"problems": problems},
        )
    if not rows:
        raise InputError(f"{path} has no rows; the guard would refuse every column")

    target = database_uri(config, uri, purpose="mirror the audit into " + COLUMN_AUDIT_TABLE)
    connection = (connect or (lambda uri: _connect(uri, purpose="mirror the audit")))(target)
    try:
        with connection.cursor() as cursor:
            before = read_audit_table(cursor)
            cursor.execute(RESET_STATEMENT)
            cursor.executemany(LOAD_STATEMENT, load_values(rows))
            connection.commit()
            after = read_audit_table(cursor)
    finally:
        connection.close()

    report = diff_audit(before, after)
    report.update(
        {
            "table": COLUMN_AUDIT_TABLE,
            "csv": str(path),
            "rows": len(rows),
            "verdicts": {
                verdict: sum(1 for row in rows if row.verdict == verdict)
                for verdict in VERDICTS
            },
        }
    )
    return report
