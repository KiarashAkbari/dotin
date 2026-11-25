"""The feature layer: a query in the database, guarded by the audit.

``04`` puts feature construction in the database (a view or a procedure) and caps the generating
query at 500 ms with a stored plan, so the two things this layer has to prove are *where* the work
happened and *what it cost*. Both are cheap to check and impossible to check after the fact, which
is why the checks live here rather than in the report.

What ``build`` does, in order, because the order is the point:

1. it refuses before touching a database if ``stage-4/sql`` does not hash to the committed
   manifest -- a frame built from edited SQL is not the frame the review read;
2. it applies the migrations in file-name order, which is idempotent by contract
   (``CREATE OR REPLACE`` everywhere, no bare ``CREATE``, no inserts);
3. it asks the *database* whether the frame reads a column the audit does not allow, through
   ``capstone.require_allowed`` over the view-dependency closure -- so the guard runs on the way
   to the number a report would quote, not only in a test suite somebody has to remember to run;
4. it counts what the frame holds (rows, features) and refuses a frame that is too small to be
   the one the brief asked for;
5. it times the query that generates the frame and judges the fastest repeat against the ceiling
   in config, through ``check_query_cost`` -- the statistic is ``TIMING_BASIS``, named once here
   instead of re-described in a docstring and a manifest field, which is how this sentence managed
   to disagree with the code it documents.

What it does not do: reimplement any feature in pandas. The SQL is the definition; this module is
the harness that refuses to let an unchecked version of it be called "the features". The connection
is injectable for exactly that reason -- the guards below are tested against a stub, and the
measurement is tested on the student's database, never here.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .audit import AUDIT_COUNT_STATEMENT, COLUMN_AUDIT_TABLE, ColumnVerdict, require_allowed
from .config import CapstoneConfig
from .errors import BudgetError, ConfigError, InputError, NotReadyError

#: the frame, as the SQL directory names it
FRAME_VIEW = "capstone.v_feature_frame"
#: the statement the ceiling is measured on, in one place because the harness in
#: ``docs/evidence/frame-performance-run.py`` has to time *this* query and not its own idea of it.
#: ``EXPLAIN (ANALYZE, TIMING OFF)`` runs the real ``SELECT *`` -- every row, every column computed --
#: and discards the rows on the server, so the client transfer is still not counted. The old
#: ``count(*)`` wrapper let PostgreSQL skip projecting columns and drop provably unique LEFT JOINs;
#: on the full dataset it measured ~0.9 s against ~5 s for the real query (review B9).
FRAME_TIMING_SQL = "EXPLAIN (ANALYZE, TIMING OFF) SELECT * FROM {frame}"
#: the statistic the ceiling verdict is taken on, in one place. ``run`` judges the fastest repeat
#: (on a shared machine the slow runs measure the neighbours), reports the mean beside it, and the
#: refusal prints the same string the manifest records -- so a change of rule is one edit, and a
#: docstring that lags behind is a test failure rather than a sentence nobody re-reads.
TIMING_BASIS = "best of the repeats"
#: the same frame with the transfer included. Nothing in ``src/`` runs this -- it exists so the
#: performance log can show how much of the wall time is the database and how much is the client,
#: which is the one number a reader is entitled to ask for after "under 500 ms" is claimed.
FRAME_READ_SQL = "SELECT * FROM {frame} AS frame"
#: how many rows the frame holds. A count of the view, not of the timing wrapper: the two answer
#: different questions ("how big is the frame" and "how long does reading it take") and a log that
#: conflates them is how a row estimate ends up quoted as a duration.
FRAME_COUNT_SQL = "SELECT count(*) FROM {frame} AS f"
#: the frame's columns, in declaration order. ``information_schema`` rather than ``SELECT * LIMIT
#: 0`` because the cursor description of a prepared count query reports the wrapper, not the frame.
FRAME_COLUMNS_SQL = (
    "SELECT column_name FROM information_schema.columns"
    " WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position"
)

#: keys that travel with a row but are not features
TECHNICAL_COLUMNS = frozenset({"order_id", "customer_id", "order_approved_at", "order_status"})
#: the brief's minimum for a feature frame built in the database
MINIMUM_FEATURES = 10
#: where the checksum tripwire lives, relative to this stage's root
CHECKSUM_MANIFEST = "docs/evidence/sql-checksums.txt"
#: this stage's root, from the installed module rather than from a config file: the manifest is
#: committed next to the code that reads it, and ``--config`` may point anywhere on purpose, so a
#: root taken from the config would look for the manifest beside the config and find nothing
STAGE_ROOT = Path(__file__).resolve().parents[2]

#: the recursive half of the live audit guard, in one place because two copies of a dependency
#: walk is how a guard and a test stop agreeing. The projection differs -- the test lists the
#: columns, the runtime guard feeds them to ``capstone.require_allowed`` -- the closure does not.
DEPENDENCY_CLOSURE_SQL = """
WITH RECURSIVE view_tree(oid) AS (
    SELECT '{frame}'::regclass::oid
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
"""
#: the columns the frame really reads, as ``table.column`` strings, source schema only
SOURCE_COLUMNS_BODY = """
SELECT DISTINCT source.relname || '.' || attribute.attname AS column_name
FROM view_tree AS tree
JOIN pg_rewrite AS rule ON rule.ev_class = tree.oid
JOIN pg_depend AS dep
  ON dep.classid = 'pg_rewrite'::regclass
 AND dep.objid = rule.oid
 AND dep.refclassid = 'pg_class'::regclass
JOIN pg_class AS source ON source.oid = dep.refobjid
JOIN pg_namespace AS namespace ON namespace.oid = source.relnamespace
JOIN pg_attribute AS attribute
  ON attribute.attrelid = source.oid
 AND attribute.attnum = dep.refobjsubid
 AND attribute.attnum > 0
 AND NOT attribute.attisdropped
WHERE namespace.nspname = '{schema}'
"""
SOURCE_COLUMNS_SQL = DEPENDENCY_CLOSURE_SQL + SOURCE_COLUMNS_BODY + "\nORDER BY column_name\n"
#: the same list, handed to the guard that lives inside the database rather than in Python
AUDIT_GUARD_SQL = (
    DEPENDENCY_CLOSURE_SQL
    + ", audited AS ("
    + SOURCE_COLUMNS_BODY
    + ")\nSELECT capstone.require_allowed(array_agg(column_name ORDER BY column_name))"
    " FROM audited\n"
)


def allowed_only(audit: list[ColumnVerdict], used: list[str]) -> list[str]:
    """Return the used columns, after refusing any the audit does not allow."""
    require_allowed(audit, used)
    return used


def check_query_cost(measured_ms: float, config: CapstoneConfig) -> float:
    """Fail loudly when the feature query broke the ceiling the brief states.

    ``02`` settled the house rule for a number like this: one run and a read-off value
    is not a measurement. The caller passes the statistic named in ``TIMING_BASIS`` -- the
    fastest of the repeats, with the mean and every run reported beside it for the reader to
    weigh -- and this function only judges it, so no duration is invented here.
    """
    ceiling = config.features.feature_query_ms
    if measured_ms < 0:
        raise BudgetError(f"a duration cannot be negative: {measured_ms} ms")
    if measured_ms > ceiling:
        raise BudgetError(
            f"the feature query took {measured_ms:.1f} ms ({TIMING_BASIS}), over the {ceiling} ms the "
            "brief allows (checklist 5.3). `python scripts/analyze_ceiling_feature.py` stores the "
            "EXPLAIN (ANALYZE, BUFFERS) output beside the number, either side of one ANALYZE, and "
            "says whether the breach is a stale plan on the rebuilt snapshot or a cost of the frame, "
            "and `--set KEY=VALUE` measures a session condition (parallelism, work_mem) the same way -- "
            "a number that moves there is credited only when the stored plan moves with it, because "
            "`best of the repeats` also rewards one fast outlier; raising the ceiling in config is a "
            "report finding, not a fix.",
            context={"measured_ms": measured_ms, "ceiling_ms": ceiling},
        )
    return float(measured_ms)


#: read before the directory is applied: `006` refuses a mixture of indicator-period pairs, but it
#: refreshes `capstone.mv_external_density` first, so applying it file by file (the procedure in
#: `sql/README.md`) leaves the snapshot -- and the model's view of it -- rebuilt over the mixture.
BRIDGE_PAIRS_STATEMENT = (
    "SELECT coalesce(count(DISTINCT (indicator, period)), 0) FROM capstone.external_municipio"
)

#: the statistics the bridge resets. `sql/006` rebuilds `capstone.mv_external_density` on every apply
#: and a refreshed materialised view arrives with no rows in `pg_statistic`, so the frame would be
#: planned against a table the planner believes is empty -- which is a measurement artefact wearing a
#: query's clothes. Analysed before the timing loop, and only for the snapshot when it exists.
BRIDGE_ANALYZE = ("capstone.external_municipio", "capstone.mv_external_density", FRAME_VIEW)

#: where `006`'s contract is stated, and the only sanctioned way out of a mixture
BRIDGE_HELP = (
    "one value per joinable place is the bridge's contract: either narrow the table to the registered "
    "source with `SELECT capstone.refresh_external_municipio('<source>')` and re-fetch, or give the "
    "second indicator its own column and view. Do not let the view decide by sorting"
)


def bridge_pairs_error(pair_count: int) -> str | None:
    """A sentence when the stored fetch cannot satisfy `006_external_bridge.sql`."""
    if pair_count < 2:
        return None
    return (
        f"capstone.external_municipio holds {pair_count} indicator-period pairs, so "
        f"`sql/006_external_bridge.sql` would refuse the apply halfway through: {BRIDGE_HELP}"
    )


def sql_migrations(directory: Path) -> list[Path]:
    """The versioned files, in the order they are applied. A stray ``.sql`` is not a migration."""
    return sorted(path for path in directory.glob("*.sql") if path.parent == directory)


def checksum_mismatches(migrations: Sequence[Path], manifest: Path) -> list[str]:
    """Files whose bytes are not what the committed manifest recorded, one line each.

    Checked here and not only in the tests because this is the moment the SQL is executed: an
    edited migration that nobody re-hashed would otherwise produce a frame whose evidence trail
    points at different bytes than the ones that made it.
    """
    if not manifest.is_file():
        return [f"{CHECKSUM_MANIFEST} is missing; run scripts/write_sql_checksums.py"]
    recorded = {
        line.split()[1]: line.split()[0]
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    # keyed by "<directory>/<name>", which is the manifest's own format and which works whether
    # the directory is the shipped sql/ or a fixture's copy of one -- relative_to() the stage root
    # would raise on the second, and quietly matching on the bare name would let sql/003 and a
    # scratch 003 look like the same file
    offenders: list[str] = []
    by_key = {f"{path.parent.name}/{path.name}": path for path in migrations}
    for key, path in sorted(by_key.items()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if key not in recorded:
            offenders.append(f"{key} is not in the manifest")
        elif recorded[key] != digest:
            offenders.append(f"{key} does not match the manifest")
    for key in sorted(recorded):
        if key not in by_key:
            offenders.append(f"{key} is in the manifest but not in {migrations[0].parent.name}/")
    return offenders


def database_uri(
    config: CapstoneConfig, override: str | None = None, *, purpose: str = "build the frame"
) -> str:
    """Where to connect, or the error that says what is missing.

    Two sources, in this order: the override a caller passes, then ``CAPSTONE_DATABASE_URI``. The
    stage-1 config's ``[database]`` url is deliberately *not* a fallback here: it carries the
    staging database, and a feature run against the wrong database produces numbers that look
    exactly like the real ones.

    ``purpose`` is a parameter because two steps share this resolver -- ``features`` and the audit
    load -- and a refusal that says "no database to build the frame against" while the user is
    loading an audit file sends them to the wrong layer's docs.
    """
    for candidate in (override,):
        if candidate:
            return str(candidate)
    env = os.environ.get("CAPSTONE_DATABASE_URI", "").strip()
    if env:
        return env
    raise ConfigError(
        f"no database to {purpose}. Set CAPSTONE_DATABASE_URI to the PostgreSQL uri "
        "the stage-1 loader filled (the same variable the live SQL guards use); "
        f"{config.source} keeps the rest of the contract.",
        context={"config": str(config.source), "variable": "CAPSTONE_DATABASE_URI"},
    )


#: the dialect suffix of a SQLAlchemy URI, mapped to the module that answers it. Two things are
#: recorded here rather than discovered at runtime: which drivers this project ships at all, and
#: that a bare ``postgresql://`` belongs to psycopg 3 -- ``psycopg==3.3.6`` is what stages 2 and 3
#: pin and what ``../stage-1-kiarash-akbari/config/default.toml`` asks for, so the default driver follows the lock.
#: Hard-coding ``import psycopg2`` was the bug this mapping replaces: it fails in the one
#: environment every other stage of this repository installs, while the refusal blamed psycopg2.
DRIVERS = {"": "psycopg", "psycopg": "psycopg", "psycopg2": "psycopg2"}


def driver_for(uri: str) -> tuple[str, str]:
    """Split a connection URI into ``(module to import, dsn that module accepts)``.

    ``postgresql+psycopg://u@h/db`` is SQLAlchemy's shape, not libpq's: the dialect after the plus
    names the driver and is then *dropped*, because a driver handed ``postgresql+psycopg://``
    refuses it as an unknown scheme. That stripping is the whole reason this is a function with a
    test instead of a line inside ``connect``: a URI that reaches the driver with its dialect still
    attached fails with an error about schemes, and nobody would guess the code was at fault.
    """
    scheme, separator, rest = uri.partition("://")
    if not separator:
        raise ConfigError(
            f"{uri!r} is not a connection uri; the layer expects"
            " postgresql[+driver]://user@host:port/database",
            context={"uri": uri},
        )
    base, _, dialect = scheme.partition("+")
    if base != "postgresql":
        raise ConfigError(
            "the feature layer speaks PostgreSQL and this uri says "
            f"{base!r}; CAPSTONE_DATABASE_URI is read verbatim, so nothing was guessed for you",
            context={"scheme": base},
        )
    module = DRIVERS.get(dialect)
    if module is None:
        raise ConfigError(
            f"this uri asks for the {dialect!r} driver, which this project does not ship; the"
            f" dialects that resolve are {', '.join(sorted(d for d in DRIVERS if d))}"
            " (and the empty one, for a bare postgresql://)",
            context={"dialect": dialect},
        )
    return module, f"postgresql://{rest}"


def _driver_and_dsn(uri: str, *, purpose: str) -> tuple[Any, str]:
    """``(the module the URI names, the dsn that module accepts)``, resolved in one place.

    The pair is the reason this is a function: a caller that picks the module itself and forwards the
    config value verbatim hands the driver a ``postgresql+psycopg://`` URI and gets an error about
    schemes. Importing late is the other half -- the offline suite has neither driver installed, and
    a module-level import would turn "no database here" into "this file does not import".
    """
    module_name, dsn = driver_for(uri)
    try:
        return importlib.import_module(module_name), dsn
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the interpreter
        raise ConfigError(
            f"capstone needs to {purpose} over {module_name} and the module is not installed"
            f" ({exc}); the project's lock ships psycopg==3.3.6 (see ../stage-3-kiarash-akbari/requirements.txt),"
            " so either install that or point CAPSTONE_DATABASE_URI at the driver you do have,"
            " e.g. postgresql+psycopg2://…",
            context={"module": module_name, "uri_scheme": uri.split("://", 1)[0]},
        ) from exc


def connect(
    uri: str, *, autocommit: bool = False, purpose: str = "read and write the feature layer"
) -> Any:
    """Open the URI with the driver the URI names; imported late so the guards run without one.

    ``autocommit`` is for the live test suite, which issues DDL and cannot wrap a whole migration in a
    transaction it never rolls back. Production paths commit explicitly -- ``features.build`` commits
    after the DDL, ``audit.load_from_csv`` after the mirror -- because a partially applied SQL
    directory is the state this stage refuses to leave behind.
    """
    module, dsn = _driver_and_dsn(uri, purpose=purpose)
    try:
        connection = module.connect(dsn)
    except Exception as error:
        # the driver's own name for "there is no database on the other end" -- refused, unreachable,
        # wrong credentials, missing database. It is a *state* of the machine, and every other state
        # of this stage is a sentence with an exit code, so it does not leave here as a traceback out
        # of a third-party module
        raise NotReadyError(connection_refusal(uri, error, purpose=purpose)) from error
    if autocommit:
        connection.autocommit = True
    return connection


def connection_refusal(uri: str, error: BaseException, *, purpose: str) -> str:
    """One sentence about a database that would not open, with nothing secret in it.

    The ``uri`` is deliberately not quoted: it can carry a password, and this text ends up in
    ``docs/evidence/`` where other people read it. The driver's own words are kept, because the
    difference between "connection refused" and "password authentication failed" is the whole
    diagnosis, and a wrapper that hides it is worse than the traceback it replaced.
    """
    from urllib.parse import urlsplit

    try:
        secret = urlsplit(uri).password or ""
    except ValueError:  # a uri the parser dislikes is exactly the input this function describes
        secret = ""
    reported = " ".join(str(error).split())
    if secret:
        reported = reported.replace(secret, "«redacted»")
    if len(reported) > 240:
        reported = reported[:240] + " …"
    return (
        f"the database named by CAPSTONE_DATABASE_URI would not open, so capstone cannot {purpose}: "
        f"{type(error).__name__}: {reported}. Nothing was read or written. The uri is not repeated here"
        " because it can hold a password -- check that a server is listening on that host and port and"
        " that the database name belongs to it, then re-run the same command."
    )


def raise_exception_type(uri: str, *, purpose: str = "run the live SQL guards") -> type:
    """The class *this* URI's driver raises for a PL/pgSQL ``RAISE EXCEPTION``.

    A guard test that wants to assert on a refusal used to write ``from psycopg2 import errors`` at
    the top of the test. That asks the interpreter which driver is installed, while the connection
    asks the URI which driver to open, and those are different questions: with ``postgresql+psycopg``
    in ``CAPSTONE_DATABASE_URI`` the guard raised ``psycopg.errors.RaiseException``, ``assertRaises``
    had been handed ``psycopg2``'s, and the escaped exception read as a broken test -- while quietly
    un-proving the leak the test exists to catch. Deriving the class from the same resolution as the
    connection makes it one decision, the one that is actually open.

    ``psycopg2`` grew its ``errors`` module in 2.8; older releases surface a ``RAISE`` as
    ``ProgrammingError``, which is what the fallback returns -- the parent, so an assertion still
    recognises what it catches instead of inventing a class.
    """
    module, _dsn = _driver_and_dsn(uri, purpose=purpose)
    errors = getattr(module, "errors", None)
    candidate = getattr(errors, "RaiseException", None) if errors is not None else None
    if isinstance(candidate, type) and issubclass(candidate, BaseException):
        return candidate
    fallback = getattr(module, "ProgrammingError", None)
    return fallback if isinstance(fallback, type) else BaseException


def _connect(uri: str, *, purpose: str = "read and write the feature layer") -> Any:
    """The private default every caller gets; ``connect`` is the public door."""
    return connect(uri, purpose=purpose)


def run(
    config: CapstoneConfig,
    *,
    uri: str | None = None,
    connect: Callable[[str], Any] | None = None,
    clock: Callable[[], float] | None = None,
    repeat: int | None = None,
) -> dict:
    """The chain step: read the audit the config names, then build.

    This is the one-argument shape :func:`capstone.chain.run_step` calls. The audit is read here
    rather than by the CLI so the runner and ``capstone features`` cannot drift into asking two
    different questions of the same file -- and a missing audit file is passed on as an empty list
    instead of refused twice, because the loader's own error already names who writes it.

    ``connect``/``clock``/``repeat`` exist for the tests; the chain passes none of them, and
    ``None`` means "whatever :func:`build` defaults to".
    """
    from .audit import load_audit

    audit = load_audit(config.audit_path) if config.audit_path.is_file() else []
    kwargs: dict[str, Any] = {}
    if uri is not None:
        kwargs["uri"] = uri
    if connect is not None:
        kwargs["connect"] = connect
    if clock is not None:
        kwargs["clock"] = clock
    if repeat is not None:
        kwargs["repeat"] = repeat
    return build(config, audit, **kwargs)


def build(
    config: CapstoneConfig,
    audit: list[ColumnVerdict],
    *,
    uri: str | None = None,
    connect: Callable[[str], Any] = _connect,
    clock: Callable[[], float] = time.monotonic,
    repeat: int = 5,
    manifest: Path | None = None,
) -> dict:
    """Apply ``stage-4/sql``, guard it, and return the frame's measured metadata.

    ``audit`` is the parsed CSV and is used for the *Python-side* refusal only -- the frame's own
    columns are derived names, so the column-by-column permission question is answered inside the
    database, over the dependency closure, which is why ``AUDIT_GUARD_SQL`` runs before any count.
    ``connect`` and ``clock`` are injectable so the sequencing is testable offline; the numbers
    are not, and that is left to the run on a real database.

    ``repeat`` timings are taken on a query that computes the whole frame and returns one number:
    the plan and the work are the frame's, the client transfer is not, and a duration that depends
    on how fast the laptop reads rows is not a property of the query.
    """
    if repeat < 1:
        raise InputError(f"repeat must be at least 1 run, not {repeat}")
    migrations = sql_migrations(config.features.sql_migrations_dir)
    if not migrations:
        raise InputError(
            f"no migrations in {config.features.sql_migrations_dir}; the SQL directory is the "
            "feature layer, so an empty one is not a frame",
        )
    offenders = checksum_mismatches(migrations, manifest or STAGE_ROOT / CHECKSUM_MANIFEST)
    if offenders:
        raise InputError(
            "the SQL directory does not match docs/evidence/sql-checksums.txt, so the frame this "
            "run builds is not the frame the evidence describes: " + "; ".join(offenders)
            + ". Fix the SQL or regenerate the manifest with scripts/write_sql_checksums.py.",
            context={"offenders": offenders},
        )

    connection = connect(database_uri(config, uri))
    try:
        with connection.cursor() as cursor:
            # asked before *any* migration, so a mixture is reported by this project's sentence and a
            # file-by-file apply never leaves the snapshot rebuilt over it
            cursor.execute("SELECT to_regclass('capstone.external_municipio') IS NOT NULL")
            bridge_present = bool(cursor.fetchone()[0])
            if bridge_present:
                cursor.execute(BRIDGE_PAIRS_STATEMENT)
                clash = bridge_pairs_error(int(cursor.fetchone()[0] or 0))
                if clash:
                    raise InputError(clash)
            for path in migrations:
                cursor.execute(path.read_text(encoding="utf-8"))
            connection.commit()

            # asked before the guard, and only about emptiness: an empty audit table is a missing
            # prerequisite, while a populated one that rejects a column is a finding about the
            # frame. Conflating them would send a reader to load the CSV when the CSV is the thing
            # that has to change, and -- worse -- a guard error relabelled as a setup error stops
            # being a bug
            cursor.execute(AUDIT_COUNT_STATEMENT)
            if not int(cursor.fetchone()[0] or 0):
                raise InputError(
                    f"{COLUMN_AUDIT_TABLE} is empty, so every column of {FRAME_VIEW} is unaudited"
                    " and the guard would refuse the frame for a reason that is not the frame's."
                    " Load the CSV first: `python -m capstone audit --load`, or without Python,"
                    " `python3 scripts/load_column_audit.py | psql \"$CAPSTONE_DATABASE_URI\" -v"
                    " ON_ERROR_STOP=1 -f -`",
                    context={"audit_table": COLUMN_AUDIT_TABLE},
                )
            # the refusal happens inside the database, which is the only place that knows what the
            # view expands to; the driver turns the RAISE into an exception, and that is the point
            cursor.execute(AUDIT_GUARD_SQL.format(frame=FRAME_VIEW, schema="olist"))
            cursor.execute(SOURCE_COLUMNS_SQL.format(frame=FRAME_VIEW, schema="olist"))
            read = [str(row[0]) for row in cursor.fetchall()]
            if audit:
                # cross-check, not a second opinion: the CSV the reviewer read and the table the
                # guard above consulted are two copies of one decision, and this stage has already
                # seen them disagree (an empty table refuses everything). A caller with no audit
                # loaded -- the chain's own placeholder probe -- passes [] and skips it.
                allowed_only(audit, read)
            if not read:
                raise InputError(
                    f"{FRAME_VIEW} reads no column of the source schema at all; a frame built from "
                    "another view's copy would do that, and it is not a frame",
                )

            cursor.execute(
                FRAME_COLUMNS_SQL,
                (config.features.schema, FRAME_VIEW.split(".", 1)[1]),
            )
            names = [str(row[0]) for row in cursor.fetchall()]
            features = [name for name in names if name not in TECHNICAL_COLUMNS]
            if len(features) < MINIMUM_FEATURES:
                raise InputError(
                    f"{FRAME_VIEW} has {len(features)} feature columns and the brief asks for at "
                    f"least {MINIMUM_FEATURES}: {sorted(features)}",
                    context={"features": features},
                )
            if "order_id" not in names:
                raise InputError(
                    f"{FRAME_VIEW} has no order_id, so nothing downstream can join a score back "
                    "to the order it scored"
                )

            cursor.execute(FRAME_COUNT_SQL.format(frame=FRAME_VIEW))
            rows = int(cursor.fetchone()[0])
            if rows <= 0:
                raise InputError(
                    f"{FRAME_VIEW} is empty; the audit, the anchor filter or the label policy has "
                    "removed every order, and a model trained on nothing is not a baseline"
                )

            # the judge reads the fastest repeat, so the state it judges has to be the state a reader
            # can reproduce: statistics warm, on the relations this stage actually changes
            if bridge_present:
                for target in BRIDGE_ANALYZE:
                    if target == "capstone.mv_external_density":
                        cursor.execute("SELECT to_regclass('capstone.mv_external_density') IS NOT NULL")
                        if not cursor.fetchone()[0]:
                            continue
                    cursor.execute(f"ANALYZE {target}")
            durations_ms: list[float] = []
            for _ in range(repeat):
                started = clock()
                cursor.execute(FRAME_TIMING_SQL.format(frame=FRAME_VIEW))
                cursor.fetchall()
                durations_ms.append((clock() - started) * 1000.0)
            connection.commit()
    finally:
        connection.close()

    mean_ms = sum(durations_ms) / len(durations_ms)
    # ``TIMING_BASIS``: on a shared machine the slow runs measure the neighbours, and the fastest
    # one is the closest reading of the query itself. Every run is still returned, so a reader can
    # see the spread the verdict was taken from (decisions.md section 16).
    best_ms = min(durations_ms)
    check_query_cost(best_ms, config)
    return {
        "frame": FRAME_VIEW,
        "rows": rows,
        "columns": len(names),
        "features": len(features),
        "source_columns_read": len(read),
        "migrations_applied": [path.name for path in migrations],
        "query_ms_mean": round(mean_ms, 3),
        "query_ms_best": round(best_ms, 3),
        "judged_on": TIMING_BASIS,
        "query_ms_runs": [round(value, 3) for value in durations_ms],
        "ceiling_ms": config.features.feature_query_ms,
        "cold_ms": round(durations_ms[0], 3),
    }
