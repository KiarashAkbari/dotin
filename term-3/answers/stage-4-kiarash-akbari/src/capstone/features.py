"""The feature layer: a query in the database, guarded by the audit.

``04`` puts feature construction in the database (a view or a procedure) and caps the
generating query at 500 ms with a stored plan, so the two things this layer has to
prove are *where* the work happened and *what it cost*. Both are cheap to check and
impossible to check after the fact, which is why the checks live here rather than in
the report.

``build`` is a placeholder on purpose: writing the SQL needs the audit file
(checklist 4.1) and the schema of the source tables, and inventing either would put a
guessed column into the delivery. The guards, in contrast, are real and tested.

The connection is opened here, in one function, because the driver it opens it with was
hard-coded: ``import psycopg2`` is what the live guards used, and this project's lock ships
``psycopg==3.3.6``, so on the environment every other stage installs the whole database half
of stage 4 skipped and blamed a driver nobody here uses. ``connect`` reads the dialect out of
the URI and imports that module late -- so the guards stay importable with no driver at all.
"""

from __future__ import annotations

import importlib
import os
from typing import Any

from .audit import ColumnVerdict, require_allowed
from .config import CapstoneConfig
from .errors import BudgetError, ConfigError
from .pending import pending


def allowed_only(audit: list[ColumnVerdict], used: list[str]) -> list[str]:
    """Return the used columns, after refusing any the audit does not allow."""
    require_allowed(audit, used)
    return used


def check_query_cost(measured_ms: float, config: CapstoneConfig) -> float:
    """Fail loudly when the feature query broke the ceiling the brief states.

    ``02`` settled the house rule for a number like this: one run and a read-off value
    is not a measurement. The caller passes a mean over repeated runs -- warm and cold
    -- and this function only judges it, so no duration is invented here.
    """
    ceiling = config.features.feature_query_ms
    if measured_ms < 0:
        raise BudgetError(f"a duration cannot be negative: {measured_ms} ms")
    if measured_ms > ceiling:
        raise BudgetError(
            f"the feature query took {measured_ms:.1f} ms on average, over the {ceiling} ms the "
            "brief allows (checklist 5.3). Store the EXPLAIN (ANALYZE) output with the number "
            "and fix the query; raising the ceiling in config is a report finding, not a fix.",
            context={"measured_ms": measured_ms, "ceiling_ms": ceiling},
        )
    return float(measured_ms)


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


def connect(uri: str, *, autocommit: bool = False, purpose: str = "read and write the feature layer") -> Any:
    """Open the URI with the driver the URI names; imported late so the guards run without one.

    ``autocommit`` is for the live test suite, which issues DDL and cannot wrap a whole migration in a
    transaction it never rolls back. Production paths commit explicitly -- ``features.build`` commits
    after the DDL, ``audit.load_from_csv`` after the mirror -- because a partially applied SQL
    directory is the state this stage refuses to leave behind.
    """
    module_name, dsn = driver_for(uri)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the interpreter
        raise ConfigError(
            f"capstone needs to {purpose} over {module_name} and the module is not installed"
            f" ({exc}); the project's lock ships psycopg==3.3.6 (see ../stage-3-kiarash-akbari/requirements.txt),"
            " so either install that or point CAPSTONE_DATABASE_URI at the driver you do have,"
            " e.g. postgresql+psycopg2://…",
            context={"module": module_name, "uri_scheme": uri.split("://", 1)[0]},
        ) from exc
    connection = module.connect(dsn)
    if autocommit:
        connection.autocommit = True
    return connection


def _connect(uri: str, *, purpose: str = "read and write the feature layer") -> Any:
    """The private default every caller gets; ``connect`` is the public door."""
    return connect(uri, purpose=purpose)


@pending(phase=4, checklist=("5.1", "5.2", "5.3"), step="features")
def build(config: CapstoneConfig, audit: list[ColumnVerdict]) -> dict:
    """Run ``stage-4/sql`` to materialise the feature frame and return its metadata.

    The body belongs to ROADMAP phase 4. What it must satisfy, in order: at least ten
    features from at least six tables, every aggregate carrying an as-of condition in
    its own SQL (5.2), every column present in the audit with a verdict of ``allowed``
    (4.2), and a mean query time under the ceiling in config (5.3).
    """
    raise AssertionError("placeholder")
