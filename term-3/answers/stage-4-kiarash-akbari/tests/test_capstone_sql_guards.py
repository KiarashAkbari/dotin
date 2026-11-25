"""The SQL contract of stage 4, checked instead of asserted.

``stage-4/sql/README.md`` states four rules and a reviewer reads them as promises: files are
versioned and idempotent, no ``SELECT *`` on a delivered-orders view, no aggregate over the
time-bearing tables without a decision-moment bound, and no column that
``docs/evidence/column_audit.csv`` does not allow. A promise in a README is worth nothing on
its own, so this file turns each one into a check that fails when the SQL drifts.

Two halves:

* **static** checks, which always run: the shipped file names against the README's frozen
  plan, the checksum manifest against the bytes, "every object is replaceable", and the
  banned patterns. No database needed, which matters because a reviewer's machine may or may
  not have one.
* **live** checks, skipped when no database is configured: the frame's *real* column
  dependencies read out of ``pg_depend`` and pushed through ``capstone.require_allowed``, the
  feature-count and source-table rules counted by the catalog instead of by a list I wrote,
  the label separation, and the agreement between the optimised window in ``003`` and the
  slow, plain as-of functions in ``004``.

A skipped live test is not a passed one, so its message says exactly what was not verified.
The pattern checkers are themselves falsified in ``NegativeControlTests``: a synthetic
forbidden file must make each one fire, otherwise a green run here means nothing.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))


SQL_DIR = STAGE / "sql"
MANIFEST = STAGE / "docs" / "evidence" / "sql-checksums.txt"
ORDER = [
    "001_capstone_schema.sql",
    "002_time_guard.sql",
    "003_feature_views.sql",
    "004_as_of_functions.sql",
    "005_external_signal.sql",
    # 006 is the bridge the model step reads; it is in this list because the directory's plan and the
    # directory's contents have to be the same six files, and a seventh name here without a file is
    # exactly as much a lie as a file here without the name
    "006_external_bridge.sql",
]
# the tables whose rows can carry information from after the decision moment; an aggregate
# over either one is where a leak would actually be written
HISTORY_RELATIONS = ("olist.orders", "olist.order_items")
ANCHOR_MARKERS = (
    "order_approved_at",
    "capstone.v_decision_moment",
    "capstone.v_customer_order_history",
    "_before(",
)


def database_uri() -> str | None:
    """Where a loaded ``olist`` schema lives, or ``None`` when this machine says nothing.

    An environment variable and nothing else, on purpose: guessing a localhost socket would
    turn "not verified" into "passed", and a skip that names no variable leaves a reader
    looking for a config file that was never consulted.
    """
    return (os.environ.get("CAPSTONE_DATABASE_URI") or "").strip() or None


def _connect():
    """A connection to that database, or ``None`` -- a missing driver is not a failure.

    The driver is resolved by ``capstone.features.connect``, the same path the feature layer
    uses. Hard-coding one module here was a second opinion that wasn't: a test pinned to
    ``psycopg2`` skips quietly on the psycopg-3 environment this project ships, and "skipped
    because no driver" reads exactly like "skipped because no database" -- which is how a
    green run of 145 offline tests came to say nothing about the eleven live ones. The two
    answers now come from one resolver, and a refusal is a different outcome from an absent
    variable: see tests/test_capstone_features.py, where a step that reaches for a connection
    is treated as having reached it.
    """
    uri = database_uri()
    if uri is None:
        return None
    try:
        from capstone.features import connect

        return connect(uri, autocommit=True, purpose="run the live SQL guards")
    except Exception:  # pragma: no cover - depends on the machine
        return None


def sql_files() -> list[Path]:
    return sorted(path for path in SQL_DIR.rglob("*.sql") if path.parent == SQL_DIR)


def body_of(text: str) -> str:
    """The SQL with comments removed, so a check cannot fire on prose inside ``--``."""
    return "\n".join(re.sub(r"--.*$", "", line) for line in text.splitlines())


def has_star_expansion(sql: str) -> bool:
    return re.search(r"\bSELECT\s+\*", sql, re.IGNORECASE) is not None


#: how each write statement can spell the schema name, including the object keyword that
#: ``DROP`` puts in between and the ``ONLY``/``TABLE`` noise ``TRUNCATE`` allows
_DESTRUCTIVE = {
    "DROP": r"\bDROP\s+(?:TABLE|SCHEMA|VIEW|FUNCTION|INDEX)\s+(?:IF\s+EXISTS\s+)?olist\.",
    "TRUNCATE": r"\bTRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?olist\.",
    "DELETE FROM": r"\bDELETE\s+FROM\s+(?:ONLY\s+)?olist\.",
    "UPDATE": r"\bUPDATE\s+(?:ONLY\s+)?olist\.",
}


def destructive_on_source(sql: str) -> list[str]:
    return [
        keyword
        for keyword, pattern in _DESTRUCTIVE.items()
        if re.search(pattern, sql, re.IGNORECASE)
    ]


def unreplaceable_creates(sql: str) -> list[str]:
    """Every ``CREATE`` must be ``OR REPLACE`` or ``IF NOT EXISTS``, or it is not idempotent.

    The window is 120 characters after the keyword rather than the rest of the file: a
    ``CREATE`` two pages away must not be excused by an ``IF NOT EXISTS`` belonging to a
    different object.
    """
    offenders: list[str] = []
    for match in re.finditer(r"\bCREATE\b", sql, re.IGNORECASE):
        # read what follows the keyword, not what starts at it: "CREATE" is in front of both
        # "OR REPLACE" and the object name, and matching from the keyword misses the former
        fragment = sql[match.end() : match.end() + 140].split(";")[0]
        if re.search(r"^\s*OR\s+REPLACE\b", fragment, re.IGNORECASE):
            continue
        if re.search(r"\bIF\s+NOT\s+EXISTS\b", fragment, re.IGNORECASE):
            continue
        offenders.append(("CREATE " + re.sub(r"\s+", " ", fragment)).strip()[:60])
    return offenders


def aggregates_without_anchor(sql: str) -> list[str]:
    """An aggregate over a time-bearing table, with no decision-moment bound in sight.

    Statement-level on purpose: the README's rule is about what a reader sees when they open
    a file, and a CTE that is bounded by the view around it is a different argument that this
    check does not make. The live test in this file is where the exactness lives.
    """
    offenders: list[str] = []
    for statement in body_of(sql).split(";"):
        if not re.search(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", statement, re.IGNORECASE):
            continue
        mentions_history = any(
            re.search(rf"\b{re.escape(table)}\b", statement, re.IGNORECASE)
            for table in HISTORY_RELATIONS
        )
        if not mentions_history:
            continue
        if not any(marker in statement for marker in ANCHOR_MARKERS):
            offenders.append(re.sub(r"\s+", " ", statement).strip()[:70])
    return offenders


AUDIT_CSV = STAGE / "docs" / "evidence" / "column_audit.csv"
#: the only two capstone objects allowed to read a column the audit does not permit as a
#: feature: the label needs the review score, and the D1/D2 counts need the final status
NON_FEATURE_VIEWS = ("v_order_label", "v_d1_excluded_orders", "v_d2_unlabelled_orders")


#: how a ``COMMENT ON FUNCTION`` can spell a type the ``CREATE OR REPLACE FUNCTION`` spelled
#: differently, and mean the same function: these are the catalog's own names for the short forms
#: the shipped files use. Anything outside this table is treated as a different type, on purpose --
#: a guard that resolves every difference away stops being a guard.
_TYPE_SYNONYMS = {
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "double precision": "float8",
    "character varying": "varchar",
}


#: how far back a ``DROP FUNCTION`` has to look for the check that guards it. The width is borrowed
#: from ``unreplaceable_creates`` for the same reason: a guard two pages away belongs to a different
#: statement, and a window keeps this a per-statement claim.
_DROP_WINDOW = 1600


def stale_drops(sql: str) -> list[dict]:
    """Every ``DROP FUNCTION`` in *sql*, with the two properties that make it safe.

    ``dynamic`` is the closer one and it is deliberately strict: the drop has to be written as
    ``EXECUTE format(...)``, i.e. asked of ``pg_proc`` for whichever stale signatures are present,
    because a literal ``DROP FUNCTION capstone.f(text, timestamptz)`` in a migration is a different
    claim -- it names one signature and assumes the database is the one that has it.

    ``guarded`` then wants the dependents count and the refusal in front of it. The window is text
    proximity, not analysis: this cannot judge whether ``dependents > 0`` is the right test, only
    whether the file shows a reader that a dependent object stops the drop. ``CASCADE`` is refused
    outright by the test that calls this, which is what keeps the window from being a joke.
    """
    body = body_of(sql)
    found: list[dict] = []
    for match in re.finditer(r"\bDROP\s+FUNCTION\b", body, re.IGNORECASE):
        start = match.start()
        window = body[max(0, start - _DROP_WINDOW) : start]
        found.append(
            {
                "line": body[:start].count("\n") + 1,
                # the keyword is inside the format string, so the quote in front of it is the seam
                "dynamic": re.search(r"\bEXECUTE\s+format\(\s*['\"]$", window, re.IGNORECASE)
                is not None,
                "guarded": all(marker in window for marker in ("pg_depend", "RAISE EXCEPTION")),
            }
        )
    return found


def split_arguments(text: str) -> list[str]:
    """One parameter list, split on the commas that are not inside parentheses.

    ``p_price numeric(10, 2)`` has a comma that belongs to the type, and counting it as a second
    parameter would make the guard report a signature that never was.
    """
    parts, depth, current = [], 0, ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return [part for part in parts if part.strip()]


def _inside_parens(text: str, open_index: int) -> str:
    """The balanced group that starts at this ``(`` -- the argument list of one statement."""
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
    raise AssertionError("unbalanced parentheses in a function signature")


def _param_types(raw: str) -> tuple[str, ...]:
    """The types of a parameter list, in order, with names and modes dropped.

    ``IN``/``OUT``/``VARIADIC`` are not types and a parameter's own name comes before its type, so
    a parameter of the shape ``p_at timestamp`` resolves to one word. An empty list is a real
    answer, not a parse failure: ``capstone.assert_no_source_writes()`` takes nothing.
    """
    types: list[str] = []
    for part in split_arguments(raw):
        tokens = part.replace("\n", " ").split()
        while tokens and tokens[0].upper() in {"IN", "OUT", "INOUT", "VARIADIC"}:
            tokens.pop(0)
        if len(tokens) > 1:
            tokens.pop(0)
        spelled = " ".join(tokens).lower().replace("pg_catalog.", "").strip()
        types.append(_TYPE_SYNONYMS.get(spelled, spelled))
    return tuple(types)


def created_signatures(sql: str) -> dict[str, set[tuple[str, ...]]]:
    """Every ``CREATE OR REPLACE FUNCTION`` header here, as ``{name: {parameter types}}``."""
    found: dict[str, set[tuple[str, ...]]] = {}
    pattern = re.compile(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+([A-Za-z_][\w.]*)\s*\(", re.IGNORECASE
    )
    for match in pattern.finditer(sql):
        name = match.group(1).lower()
        types = _param_types(_inside_parens(sql, match.end() - 1))
        found.setdefault(name, set()).add(types)
    return found


def commented_signatures(sql: str) -> list[tuple[str, tuple[str, ...], int]]:
    """Every ``COMMENT ON FUNCTION`` here, with its line number for the failure message."""
    found: list[tuple[str, tuple[str, ...], int]] = []
    pattern = re.compile(
        r"COMMENT\s+ON\s+FUNCTION\s+([A-Za-z_][\w.]*)\s*\(", re.IGNORECASE
    )
    for match in pattern.finditer(sql):
        inner = _inside_parens(sql, match.end() - 1)
        rest = sql[match.end() + len(inner) + 1 :]
        if not re.match(r"\s*IS\b", rest, re.IGNORECASE):
            # a ``COMMENT ON FUNCTION`` inside prose, or a multi-statement line that is not this one
            continue
        line = sql[: match.start()].count("\n") + 1
        found.append((match.group(1).lower(), _param_types(inner), line))
    return found


def blocks_of(sql: str) -> list[tuple[str, str]]:
    """``[(object name, body)]``, split on ``CREATE``.

    Splitting on ``;`` would cut a function body in half, because ``AS $$ ... ; ... $$``
    contains semicolons the SQL parser does not see as statement ends.
    """
    out: list[tuple[str, str]] = []
    parts = re.split(r"(?=\bCREATE\b)", sql)
    for part in parts:
        if not re.match(r"\s*CREATE\b", part, re.IGNORECASE):
            continue
        name = re.search(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|MATERIALIZED\s+VIEW|FUNCTION|SCHEMA)\s+"
            r"(?:IF\s+NOT\s+EXISTS\s+)?([a-zA-Z_][\w.]*)",
            part,
            re.IGNORECASE,
        )
        out.append(((name.group(1).split(".")[-1] if name else "?"), part))
    return out


def audit_rows() -> dict[tuple[str, str], str]:
    import csv as csv_module

    with AUDIT_CSV.open(newline="", encoding="utf-8") as handle:
        return {(row["table"], row["column"]): row["verdict"] for row in csv_module.DictReader(handle)}


def olist_columns_used(body: str) -> list[tuple[str, str]]:
    """Every ``olist.table.column`` a block reads, resolving the aliases the block declares.

    Aliases are per block on purpose: two views in one file may both call something ``i``, and
    reading the file as one blob would attribute ``i.n_items`` -- a column of a capstone
    rollup -- to ``olist.order_items``. That false positive is how a check like this earns a
    reputation for crying wolf, so the scope is the statement.
    """
    aliases = {
        (match.group(2) or match.group(1)): match.group(1)
        for match in re.finditer(
            r"(?:FROM|JOIN)\s+olist\.(\w+)(?:\s+AS\s+(\w+))?", body, re.IGNORECASE
        )
    }
    used = {
        (aliases[match.group(1)], match.group(2))
        for match in re.finditer(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", body)
        if match.group(1) in aliases
    }
    return sorted(used)


#: the table the audit reserves for the label. Column-level attribution can be fooled --
#: a subquery that reads it without an alias, or a bare ``review_score`` inside one -- so the
#: whole *table* is refused on the feature side instead of only its forbidden columns.
ANSWER_TABLE = "order_reviews"


def unaudited_or_forbidden(body: str, *, may_read_the_answer: bool) -> list[str]:
    rows = audit_rows()
    problems = []
    if not may_read_the_answer and re.search(
        rf"\b{ANSWER_TABLE}\b", body, re.IGNORECASE
    ):
        problems.append(
            f"reads olist.{ANSWER_TABLE}, which the audit allows only as the label's join path"
        )
    for table, column in olist_columns_used(body):
        verdict = rows.get((table, column))
        if verdict is None:
            problems.append(f"{table}.{column} has no row in the audit")
        elif verdict != "allowed" and not may_read_the_answer:
            problems.append(f"{table}.{column} is {verdict}")
    return problems


class StaticSqlContractTests(unittest.TestCase):
    def test_the_shipped_files_are_exactly_the_readmes_plan(self):
        found = sorted(path.name for path in sql_files())
        self.assertEqual(found, ORDER)

    def test_every_file_name_is_versioned_and_snake_case(self):
        offenders = [
            path.name
            for path in sql_files()
            if re.fullmatch(r"[0-9]{3}_[a-z0-9_]+\.sql", path.name) is None
        ]
        self.assertEqual(offenders, [])

    def test_no_shipped_migration_is_edited_in_place(self):
        """The manifest is the tripwire stage 3 learned to need: edited bytes break a ledger."""
        self.assertTrue(MANIFEST.exists(), msg=f"{MANIFEST} must be committed with the SQL")
        recorded = {
            line.split()[1]: line.split()[0]
            for line in MANIFEST.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        }
        current = {
            path.relative_to(STAGE).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sql_files()
        }
        self.assertEqual(recorded, current)

    def test_every_object_is_replaceable(self):
        offenders = [
            f"{path.name}: {offence}"
            for path in sql_files()
            for offence in unreplaceable_creates(body_of(path.read_text(encoding="utf-8")))
        ]
        self.assertEqual(offenders, [])

    def test_a_comment_never_names_a_signature_the_directory_does_not_create(self) -> None:
        """A ``COMMENT ON FUNCTION`` is resolved by exact signature, so a stale one is a defect.

        The mirror's ``fix/stage4-live-guards`` commit of 2026-11-18 is why this exists: it made all
        four as-of parameters ``p_at timestamp`` and left
        ``COMMENT ON FUNCTION capstone.customer_order_volume_before(text, timestamptz)`` under them.
        On a database that had never held the old signature, that statement aborts the migration. On
        one that had, it succeeds -- and comments a stale overload the ``CREATE OR REPLACE`` could
        not replace, because a changed argument type creates a second function rather than editing
        the first. The second outcome is the dangerous one: a green apply, a comment that looks
        right, and a frame whose call resolves by argument types to whichever overload the
        planner prefers.
        """
        for path in sql_files():
            spoken = len(
                re.findall(r"COMMENT\s+ON\s+FUNCTION", body_of(path.read_text(encoding="utf-8")))
            )
            seen = len(commented_signatures(body_of(path.read_text(encoding="utf-8"))))
            self.assertEqual(
                spoken,
                seen,
                f"{path.name}: {spoken} COMMENT ON FUNCTION statements in the text, "
                f"{seen} parsed -- a guard that reads none would pass for the wrong reason",
            )

        created: dict[str, set[tuple[str, ...]]] = {}
        for path in sql_files():
            body = body_of(path.read_text(encoding="utf-8"))
            for name, options in created_signatures(body).items():
                created.setdefault(name, set()).update(options)
        offenders: list[str] = []
        for path in sql_files():
            body = body_of(path.read_text(encoding="utf-8"))
            for name, types, line in commented_signatures(body):
                if types not in created.get(name, set()):
                    spelled = ", ".join(types)
                    offenders.append(f"{path.name}:{line}: nothing creates {name}({spelled})")
        self.assertEqual(offenders, [])

    def test_a_stale_overload_is_dropped_only_when_nothing_depends_on_it(self) -> None:
        """The clean-up that leaves four as-of functions on a re-applied database, not eight.

        Three claims. The drop exists and `004` owns exactly one of them; it is guarded, meaning a
        dependents count and a refusal are written in front of it; and no `CASCADE` appears anywhere
        in the directory, so the guard cannot be satisfied by the one keyword that makes it optional.
        The last is why this is a static test rather than a live one: a `CASCADE` would have to be
        written down, and what a reader of `004` has to be able to trust is the text.
        """
        for path in sql_files():
            with self.subTest(name=path.name):
                body = body_of(path.read_text(encoding="utf-8"))
                self.assertEqual([], re.findall(r"\bCASCADE\b", body, re.IGNORECASE))
        offenders = [
            f"{path.name}:{drop['line']}: DROP FUNCTION "
            + ("with no dependents guard" if not drop["guarded"] else "not asked of pg_proc")
            for path in sql_files()
            for drop in stale_drops(path.read_text(encoding="utf-8"))
            if not (drop["guarded"] and drop["dynamic"])
        ]
        self.assertEqual(offenders, [])
        self.assertEqual(
            len(stale_drops((SQL_DIR / "004_as_of_functions.sql").read_text(encoding="utf-8"))),
            1,
            msg="004 drops the stale overload once, and no other file drops one at all",
        )
        # the parser reads what is there: a bare drop has to look different to it, and the safe
        # spelling has to look like the safe spelling
        self.assertEqual(
            [{"line": 1, "dynamic": False, "guarded": False}],
            stale_drops("DROP FUNCTION capstone.f(text, timestamptz);"),
            msg="the guard's own tripwire: an unguarded literal drop must not read as a guarded one",
        )
        self.assertEqual(
            [{"line": 3, "dynamic": True, "guarded": True}],
            stale_drops(
                "SELECT COUNT(*) INTO dependents FROM pg_depend;\n"
                "RAISE EXCEPTION 'still used';\n"
                "EXECUTE format('DROP FUNCTION %I.%I(%s)', n, p, a);"
            ),
        )
        self.assertEqual(
            [],
            stale_drops("SELECT COUNT(*) FROM pg_depend;\nRAISE EXCEPTION 'x';\n"),
            msg="the markers alone are not a drop, and a check that invents offences is not a check",
        )

    def test_the_bridge_columns_year_is_the_year_its_guard_checks(self) -> None:
        """`external_density_2010` earns its suffix, inside one file.

        The column name is a claim about the data (the year the registered contract fetched), and the
        `DO` block is what refuses a database that disagrees. Both live in `006`, so the rot that has
        to be prevented is the pair drifting: somebody edits the guard to accept 2022, leaves the
        column name alone, and the report then reads a 2022 number under a 2010 label. Nothing outside
        this file can be compared against -- `external.py` takes the period from the response and the
        URL that carried it sits in the student's ignored local config -- so the check is that the
        file names exactly one year, and that the name and the check agree.
        """
        text = (SQL_DIR / "006_external_bridge.sql").read_text(encoding="utf-8")
        body = re.sub(r"^--.*$", "", text, flags=re.MULTILINE)
        years = set(re.findall(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])", body))
        self.assertEqual(
            years,
            {"2010"},
            msg=f"the bridge speaks {sorted(years)}; one column per period means one year per file, and a "
            "second year has to arrive as a second column with its own decision",
        )
        self.assertIn("AS external_density_2010", text)
        self.assertIn("e.period <> '2010'", text)
        self.assertNotIn(
            "LIKE '%@2010'",
            text,
            "filtering by a spelled key is how the indicator name sneaks back into SQL: the guard's job is "
            "to refuse an ambiguous pick, not to guess a string",
        )

    def test_the_bridge_does_not_touch_the_frame_it_reads_through(self) -> None:
        """`005`'s decision, kept: the frame does not learn about the external source.

        `v_feature_frame` is what `capstone features` builds and what the audit governs; a migration
        that added columns to it would move the 29-column count the timing item quotes *and* put an
        unverified source inside the as-of story. So the bridge is a separate view joined by
        `order_id`, and this refuses the tempting version -- including the one where `006` merely
        grows a `CREATE OR REPLACE VIEW capstone.v_feature_frame` "for convenience".
        """
        text = (SQL_DIR / "006_external_bridge.sql").read_text(encoding="utf-8")
        for forbidden in ("CREATE OR REPLACE VIEW capstone.v_feature_frame", "ALTER VIEW", "DROP VIEW"):
            with self.subTest(pattern=forbidden):
                self.assertNotIn(forbidden, text)
        created = re.findall(r"CREATE OR REPLACE VIEW capstone\.(\w+)", text)
        self.assertEqual(created, ["v_external_feature", "v_external_feature_coverage"])
        self.assertEqual(
            re.findall(r"FROM (olist\.\w+)", text),
            [],
            "the bridge reads the frame's own views, never a source table: that is what keeps the audit "
            "guard's denominator unchanged",
        )

    def test_no_star_expansion_and_no_write_against_the_source_schema(self):
        offenders: list[str] = []
        for path in sql_files():
            body = body_of(path.read_text(encoding="utf-8"))
            if has_star_expansion(body):
                offenders.append(f"{path.name}: SELECT *")
            offenders += [f"{path.name}: {word} on olist" for word in destructive_on_source(body)]
        self.assertEqual(offenders, [])

    def test_aggregates_over_time_bearing_tables_carry_the_anchor(self):
        offenders: list[str] = []
        for path in sql_files():
            offenders += [
                f"{path.name}: {offence}"
                for offence in aggregates_without_anchor(path.read_text(encoding="utf-8"))
            ]
        self.assertEqual(offenders, [])

    def test_no_feature_reads_a_column_the_audit_does_not_allow(self):
        """Every ``olist`` column in a feature-side block must be audited and ``allowed``.

        The audit's own verdict for ``orders.order_status`` is the case that matters: the final
        status contains ``delivered``, which is the outcome wearing a feature's clothes, and a
        frame built on a base view that exposes it would inherit it whether anyone selects it
        or not. That is why the rule is checked per block and why exactly three objects are
        allowed to read the answer at all.
        """
        offenders = []
        for path in sql_files():
            for name, body in blocks_of(body_of(path.read_text(encoding="utf-8"))):
                may_read_the_answer = name in NON_FEATURE_VIEWS
                for problem in unaudited_or_forbidden(
                    body, may_read_the_answer=may_read_the_answer
                ):
                    offenders.append(f"{path.name}:{name} -- {problem}")
        self.assertEqual(offenders, [])

    def test_the_feature_side_never_names_the_reviews_table(self):
        offenders = []
        for path in sql_files():
            for name, body in blocks_of(body_of(path.read_text(encoding="utf-8"))):
                if name in NON_FEATURE_VIEWS:
                    continue
                if re.search(r"\border_reviews\b", body, re.IGNORECASE):
                    offenders.append(f"{path.name}:{name} reads olist.order_reviews")
        self.assertEqual(offenders, [])

    def test_the_three_exceptions_are_the_only_ones(self):
        """The exemption list is short, named, and cannot grow quietly.

        Adding a view here is the same act as marking a column allowed in the audit, so it has
        to be visible in a diff of this file rather than discovered in a diff of the SQL.
        """
        referenced = {
            name
            for path in sql_files()
            for name, _ in blocks_of(body_of(path.read_text(encoding="utf-8")))
            if name in NON_FEATURE_VIEWS
        }
        self.assertEqual(referenced, set(NON_FEATURE_VIEWS))
        self.assertEqual(len(NON_FEATURE_VIEWS), 3)

    def test_the_as_of_predicate_is_in_sql_not_in_python(self):
        """``04`` requires the rule in the query, so the SQL files must contain it."""
        text = "\n".join(path.read_text(encoding="utf-8") for path in sql_files())
        self.assertIn("o.order_approved_at < p_at", text)
        self.assertIn("PRECEDING", text)


class NegativeControlTests(unittest.TestCase):
    """The checkers above are live, not decorative: forbidden SQL has to make them fire."""

    def test_star_expansion_is_caught(self):
        self.assertTrue(has_star_expansion("CREATE VIEW a.v AS SELECT * FROM olist.orders;"))
        self.assertFalse(has_star_expansion("CREATE VIEW a.v AS SELECT o.order_id FROM olist.orders o;"))
        # a comment mentioning it is not a violation, and must not be reported as one
        self.assertFalse(has_star_expansion(body_of("-- SELECT * is banned\nSELECT 1;")))

    def test_writes_against_the_source_are_caught(self):
        self.assertEqual(
            destructive_on_source("DROP TABLE olist.orders;"),
            ["DROP"],
        )
        self.assertEqual(destructive_on_source("DROP TABLE capstone.column_audit;"), [])

    def test_a_bare_create_is_caught(self):
        self.assertTrue(unreplaceable_creates("CREATE TABLE olist.a (x int);"))
        self.assertEqual(unreplaceable_creates("CREATE TABLE IF NOT EXISTS capstone.a (x int);"), [])
        self.assertEqual(unreplaceable_creates("CREATE OR REPLACE VIEW capstone.a AS SELECT 1;"), [])

    def test_a_feature_reading_the_answer_is_caught(self):
        leaked = (
            "CREATE OR REPLACE VIEW capstone.v_feature_frame AS"
            " SELECT o.order_id, r.review_score FROM olist.order_reviews AS r"
            " JOIN olist.orders AS o ON TRUE;"
        )
        problems = unaudited_or_forbidden(body_of(leaked), may_read_the_answer=False)
        self.assertEqual(len(problems), 2, msg=str(problems))
        self.assertTrue(any("join path" in problem for problem in problems), msg=str(problems))
        # the same read inside the label view is the one place it is legal
        self.assertEqual(
            unaudited_or_forbidden(body_of(leaked), may_read_the_answer=True), []
        )
        bounded = (
            "CREATE OR REPLACE VIEW capstone.v_feature_frame AS"
            " SELECT o.order_id, o.order_approved_at FROM olist.orders AS o;"
        )
        self.assertEqual(unaudited_or_forbidden(body_of(bounded), may_read_the_answer=False), [])

    def test_an_unbounded_aggregate_is_caught(self):
        leaked = (
            "CREATE VIEW capstone.v AS SELECT o.customer_id, COUNT(*) AS n "
            "FROM olist.orders AS o GROUP BY 1;"
        )
        self.assertEqual(len(aggregates_without_anchor(leaked)), 1)
        bounded = leaked.replace(
            "GROUP BY 1;",
            "WHERE o.order_approved_at IS NOT NULL AND o.order_approved_at < now() GROUP BY 1;",
        )
        self.assertEqual(aggregates_without_anchor(bounded), [])


class LiveSqlContractTests(unittest.TestCase):
    """Checks that need the loaded ``olist`` schema and the applied ``capstone`` one."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.connection = _connect()
        if cls.connection is None:
            raise unittest.SkipTest(
                "CAPSTONE_DATABASE_URI is unset (or no driver/connection), so the live SQL "
                "guards were NOT verified: the frame's column dependencies, the feature and "
                "source counts, the label separation, the as-of agreement, the audit mirror "
                "beside its CSV and the type of every number both paths produce stay unverified"
            )
        with cls.connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('capstone.v_feature_frame')")
            if cursor.fetchone()[0] is None:
                raise unittest.SkipTest(
                    "capstone.v_feature_frame does not exist here, so the live SQL guards were "
                    "NOT verified. Apply the directory first: psql -f each file in the order "
                    "sql/README.md gives (the Python half is still phase 4's pending)"
                )

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "connection", None) is not None:
            cls.connection.close()

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def frame_columns(self) -> list[str]:
        return [
            row[0]
            for row in self.query(
                """
                SELECT a.attname
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                JOIN pg_attribute AS a ON a.attrelid = c.oid
                WHERE n.nspname = 'capstone' AND c.relname = 'v_feature_frame'
                  AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY a.attnum
                """
            )
        ]

    def frame_dependencies(self) -> list[tuple[str, str | None]]:
        """Follow rewrite-rule dependencies through intermediate views."""
        return self.query(
            """
            WITH RECURSIVE view_tree(oid) AS (
                SELECT 'capstone.v_feature_frame'::regclass::oid
                UNION
                SELECT referenced.oid
                FROM view_tree AS tree
                JOIN pg_rewrite AS rule ON rule.ev_class = tree.oid
                JOIN pg_depend AS dep
                  ON dep.classid = 'pg_rewrite'::regclass
                 AND dep.objid = rule.oid
                 AND dep.refclassid = 'pg_class'::regclass
                JOIN pg_class AS referenced ON referenced.oid = dep.refobjid
                WHERE referenced.relkind IN ('v', 'm')
            )
            SELECT DISTINCT source.relname, attribute.attname
            FROM view_tree AS tree
            JOIN pg_rewrite AS rule ON rule.ev_class = tree.oid
            JOIN pg_depend AS dep
              ON dep.classid = 'pg_rewrite'::regclass
             AND dep.objid = rule.oid
             AND dep.refclassid = 'pg_class'::regclass
            JOIN pg_class AS source ON source.oid = dep.refobjid
            JOIN pg_namespace AS namespace ON namespace.oid = source.relnamespace
            LEFT JOIN pg_attribute AS attribute
              ON attribute.attrelid = source.oid
             AND attribute.attnum = dep.refobjsubid
             AND attribute.attnum > 0
             AND NOT attribute.attisdropped
            WHERE namespace.nspname = 'olist'
            ORDER BY source.relname, attribute.attname
            """
        )

    def frame_source_columns(self) -> list[tuple[str, str]]:
        """Return audited source columns from the full view dependency closure."""
        return [
            (table, column)
            for table, column in self.frame_dependencies()
            if column is not None
        ]

    def test_the_audit_table_holds_the_rows_the_repository_ships(self):
        """``capstone.column_audit`` and ``docs/evidence/column_audit.csv`` are the same table.

        The guards are fail-closed by design: an empty ``column_audit`` refuses every column, and
        a live run then reports errors about the feature frame that have nothing to do with it --
        two ERRORs naming `v_feature_frame`, with the cause one ``RAISE`` deep inside
        ``require_allowed``. So the precondition is a test now, and it fails with the command that
        fixes it. Row by row rather than by count, because a count agrees while verdicts disagree,
        and that is the version of this that would actually mislead someone.
        """
        rows = self.query(
            'SELECT "table", "column", verdict FROM capstone.column_audit'
            ' ORDER BY "table", "column"'
        )
        self.assertGreater(
            len(rows),
            0,
            msg="capstone.column_audit is empty; the guards refuse everything by design. Load"
                " it with: python3 scripts/load_column_audit.py | psql"
                ' "$CAPSTONE_DATABASE_URI" -v ON_ERROR_STOP=1 -f -',
        )
        from capstone import audit

        expected = [
            (row.table, row.column, row.verdict)
            for row in sorted(audit.load_audit(AUDIT_CSV), key=lambda row: (row.table, row.column))
        ]
        self.assertEqual(
            [tuple(str(value) for value in row) for row in rows],
            [tuple(row) for row in expected],
            msg="the table does not match the CSV; reload it, do not edit the table",
        )

    def test_the_frame_reads_no_column_the_audit_forbids(self):
        columns = sorted({f"{table}.{column}" for table, column in self.frame_source_columns()})
        self.assertGreater(len(columns), 0, msg="the catalog reported no dependencies at all")
        # ``require_allowed`` raises on any refusal, and the driver the URI names turns that into a
        # python exception -- whichever driver that is. This test only asserts that the query runs or
        # raises, so it has no opinion about the class; the negative control below does, and it asks
        # the resolver instead of guessing.
        self.query("SELECT capstone.require_allowed(%s::text[])", (columns,))

    def test_the_guard_below_bites_on_a_forbidden_column(self):
        """The negative control for the test above, on the same database."""
        # the exception class comes from the URI, not from an import here:
        # ``capstone.features.raise_exception_type`` records why a hard-coded ``psycopg2`` import
        # made this test error on a psycopg-3 connection -- the guard's own exception escaped
        # ``assertRaises``, which looks like a broken test and leaves the leak unproved
        from capstone.features import raise_exception_type

        raise_error = raise_exception_type(database_uri())

        self.query("SELECT capstone.require_allowed(ARRAY['orders.order_approved_at'])")
        with self.assertRaises(raise_error):
            self.query("SELECT capstone.require_allowed(ARRAY['order_reviews.review_score'])")

    def test_the_frame_meets_the_minimum_size_rules(self):
        technical = {"order_id", "customer_id", "order_approved_at", "order_status"}
        features = [name for name in self.frame_columns() if name not in technical]
        self.assertGreaterEqual(len(features), 10, msg=str(sorted(features)))
        tables = sorted({table for table, _ in self.frame_source_columns()})
        self.assertGreaterEqual(len(tables), 6, msg=str(tables))

    def test_the_frame_carries_no_answer(self):
        names = self.frame_columns()
        self.assertFalse([name for name in names if "negative" in name or "review" in name])
        dependencies = self.frame_dependencies()
        self.assertGreater(len(dependencies), 0, msg="no source dependencies found")
        reviews = sorted({
            table for table, _ in dependencies if "review" in table
        })
        self.assertEqual(reviews, [])

    def test_the_window_and_the_plain_as_of_query_agree(self):
        rows = self.query(
            """
            SELECT f.order_id, f.customer_id, f.order_approved_at,
                   f.prior_orders_by_customer, f.days_since_prior_order
            FROM capstone.v_feature_frame AS f
            WHERE f.prior_orders_by_customer > 0
            ORDER BY f.order_id
            LIMIT 200
            """
        )
        self.assertGreater(len(rows), 0, msg="no repeat customer in this database")
        for order_id, customer_id, at, count, days in rows:
            expected = self.query(
                "SELECT capstone.customer_order_volume_before(%s, %s), "
                "capstone.customer_days_since_prior_order(%s, %s)",
                (customer_id, at, customer_id, at),
            )[0]
            self.assertEqual(
                (count, round(days, 6) if days is not None else None),
                (expected[0], round(expected[1], 6) if expected[1] is not None else None),
                msg=order_id,
            )

    def test_the_two_paths_agree_in_type_as_well_as_value(self):
        """The same number out of both implementations, and the same type.

        ``test_the_window_and_the_plain_as_of_query_agree`` above catches a wrong formula. It does
        not catch a frame column that arrives as ``Decimal`` while the function's ``RETURNS double
        precision`` arrives as ``float``, because ``assertAlmostEqual`` forgives that; and a view's
        column types come from its expressions, not from a declaration, so they move whenever
        someone rewrites a view. Cheap to check, and it is the check that has to sit next to the
        value comparison for either of them to mean anything.
        """
        types = {
            str(row[0]): str(row[1])
            for row in self.query(
                """
                SELECT a.attname, t.typname
                FROM pg_attribute AS a
                JOIN pg_type AS t ON t.oid = a.atttypid
                WHERE a.attrelid = 'capstone.v_feature_frame'::regclass
                  AND a.attname IN ('days_since_prior_order', 'shipping_limit_days', 'promise_days')
                """
            )
        }
        self.assertEqual(sorted(types), [
            "days_since_prior_order",
            "promise_days",
            "shipping_limit_days",
        ], msg=str(sorted(types)))
        for name in sorted(types):
            self.assertEqual(
                types[name],
                "float8",
                msg=f"{name} is {types[name]}; a float8 beside a numeric is how one value"
                    " becomes Decimal from one path and a float from the other",
            )
        for signature in (
            "capstone.customer_days_since_prior_order(text, timestamp)",
            "capstone.seller_mean_price_before(text, timestamp)",
        ):
            self.assertEqual(
                str(self.query(f"SELECT pg_get_function_result('{signature}'::regprocedure)")[0][0]),
                "double precision",
                msg=f"{signature} no longer returns the type the frame stores",
            )
            # and it takes the instant in the same type the frame stores it in. A `timestamp`
            # column compared against a `timestamptz` parameter is resolved through the session
            # zone, which is how the same number can differ by an hour between the two paths.
            arguments = str(
                self.query(f"SELECT pg_get_function_arguments('{signature}'::regprocedure)")[0][0]
            )
            self.assertIn("timestamp without time zone", arguments, msg=f"{signature}: {arguments}")
        anchor_type = str(
            self.query(
                "SELECT t.typname FROM pg_attribute AS a JOIN pg_type AS t ON t.oid = a.atttypid"
                " WHERE a.attrelid = 'capstone.v_feature_frame'::regclass"
                " AND a.attname = 'order_approved_at'"
            )[0][0]
        )
        self.assertEqual(
            anchor_type,
            "timestamp",
            msg="the frame's anchor is naive, and every as-of comparison must stay naive with it",
        )
        # and the one float8 aggregate in the frame must not be a float8 aggregate
        centroid = self.query(
            "SELECT data_type FROM information_schema.columns"
            " WHERE table_schema = 'capstone' AND table_name = 'v_zip_centroid'"
            " AND column_name IN ('lat', 'lng') ORDER BY column_name"
        )
        self.assertEqual([str(row[0]) for row in centroid], ["numeric", "numeric"], msg=str(centroid))

    def test_reapplying_the_directory_changes_nothing(self):
        """Idempotency, tested rather than asserted: run the directory again, compare."""
        digest = (
            "SELECT count(*), coalesce(sum(hashtext(f.*::text)), 0), "
            "coalesce(md5(string_agg(f.order_id, ',' ORDER BY f.order_id)), '') "
            "FROM capstone.v_feature_frame AS f"
        )
        before = self.query(digest)
        self.assertGreater(before[0][0], 0)
        for name in ORDER:
            with self.connection.cursor() as cursor:
                cursor.execute((SQL_DIR / name).read_text(encoding="utf-8"))
        self.assertEqual(before, self.query(digest))


if __name__ == "__main__":
    unittest.main()
