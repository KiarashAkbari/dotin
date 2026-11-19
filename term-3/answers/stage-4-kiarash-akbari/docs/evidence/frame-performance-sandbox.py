#!/usr/bin/env python3
"""Build a disposable PostgreSQL at production scale, measure the frame on it, and print one log.

This is the sandbox half of checklist 5.3 and item 12. The two scripts beside it do the real work --
``frame-performance-run.py`` measures any database you point it at, and
``frame-performance-synthetic.py`` fills one with a corpus the size of the brief's -- and this file is
what makes them runnable *at all* where no server exists: a throwaway cluster from the ``pgserver`` dev
extra, stage-2's own DDL, the five migrations, the audit, the synthetic corpus, then both measurements,
one after the other, in one capture.

Why the numbers cannot come from anywhere else

``olist`` is ninety-nine thousand orders. The fixture CSVs this repository ships are nine rows each, and
a duration or a plan measured on nine rows is a fact about the planner starting up. Since no route to the
student's database exists from a sandbox, the only honest way to talk about the *plan* -- the
``EXPLAIN`` tree, the row estimates, which operator does the work -- is to build a database whose
statistics look like the real one's. That is what this does, and every line of its output says so: a
timing measured here is labelled synthetic and production-sized, never "the query takes 395 ms".

What it refuses

* no ``pgserver`` (or no ``psql``) -> it says the server half was not measured and exits 0, the same
  rule ``postgres-smoke-run.py`` follows; a missing dev dependency is not a failed measurement;
* it never touches a database it did not start: the only URI used is the one ``pgserver`` hands back, and
  the cluster lives under a temp directory removed here, so no destroy flag is consulted -- there is
  nothing in scope to destroy but this cluster;
* it writes nothing into the repository. The log is what you ``tee``; no JSON sidecar, no fixture CSV.

Run it from ``stage-4/`` with the interpreter that has the dev extra::

    python3 docs/evidence/frame-performance-sandbox.py \
      | tee docs/evidence/frame-performance-sandbox-run.txt

Add ``--orders 25000`` to shrink the corpus while iterating: the plan changes shape below roughly that
size, so a small run is a quick check that the *procedure* works, not a measurement.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

STAGE = Path(__file__).resolve().parents[2]
ROOT = STAGE.parent
MIGRATIONS = (
    "001_capstone_schema.sql",
    "002_time_guard.sql",
    "003_feature_views.sql",
    "004_as_of_functions.sql",
    "005_external_signal.sql",
)

try:
    import pgserver  # type: ignore[import-not-found]

    PSQL = Path(str(pgserver.__file__)).parent / "pginstall" / "bin" / "psql"
    IMPORT_ERROR = ""
except ModuleNotFoundError as error:  # the refusal path, recorded rather than raised
    pgserver = None  # type: ignore[assignment]
    PSQL = Path("psql")
    IMPORT_ERROR = str(error)


ELAPSED = re.compile(r" in [0-9]+\.[0-9]+s")


def say(text: str = "") -> None:
    print(text, flush=True)


def rule(title: str) -> None:
    say()
    say("-" * 72)
    say(title)
    say("-" * 72)


def ddl_path() -> Path | None:
    """stage-2's Olist DDL, in either tree layout -- the same glob the smoke module uses."""
    name = "001_create_olist_schema.sql"
    candidates = [ROOT / "stage-2" / "sql" / name, *sorted(ROOT.glob(f"stage-2*/sql/{name}"))]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def run_psql(uri: str, *, sql: str | None = None, script: Path | None = None, label: str = "") -> None:
    args = [str(PSQL), uri, "-v", "ON_ERROR_STOP=1", "-q"]
    args += ["-c", sql] if sql else ["-f", str(script)]
    proc = subprocess.run(args, capture_output=True, text=True, cwd=STAGE)
    if proc.returncode:
        raise SystemExit(f"{label or 'psql'} failed on a cluster this script owns: {proc.stderr[:400]}")


def scrub(lines: list[str]) -> list[str]:
    """Drop unittest's elapsed-time clause, the one part of a test log that is never the same twice.

    The *durations* stay -- they are the measurement, and they move run to run, which is why this log
    reports five of them instead of one. A test-suite clock is different: it says nothing about the code
    and makes a byte comparison of two runs fail for no reason.
    """
    return [ELAPSED.sub(r" in <t>s", line) for line in lines]


def run_step(script: list[str], *, env: dict[str, str], label: str = "") -> int:
    """One child script, its output inlined so the log is self-contained."""
    say(f"$ python3 {' '.join(script)}")
    proc = subprocess.run([sys.executable, *script], cwd=STAGE, env=env, capture_output=True, text=True)
    for line in scrub(proc.stdout.splitlines()):
        say(f"  | {line}")
    for line in scrub(proc.stderr.strip().splitlines()[-8:]):
        say(f"  ! {line}")
    say(f"  [exit {proc.returncode}] {label}")
    return proc.returncode


def main(argv: list[str]) -> int:
    orders = "100000"
    if "--orders" in argv:
        orders = argv[argv.index("--orders") + 1]
    if pgserver is None:
        say(
            "not measured: the dev extra is not installed on this interpreter, so there is no\n"
            "temporary cluster to build. Install it (``pip install pgserver``) and re-run, or run\n"
            "``docs/evidence/frame-performance-run.py`` against your own server -- which is the\n"
            "half of checklist 5.3 that only you can close. Refusal: " + IMPORT_ERROR + "\n"
            "exit 0 -- a machine without the dev extra has not failed a measurement."
        )
        return 0
    ddl = ddl_path()
    if ddl is None:
        say(
            "not measured: no stage-2/sql/001_create_olist_schema.sql beside this checkout, so there\n"
            "is no Olist DDL to build the corpus from. In the dotin layout the file lives at\n"
            "term-3/answers/stage-2-<name>/sql/ with the same name; nothing was created."
        )
        return 0
    work = Path(tempfile.mkdtemp(prefix="capstone-perf-"))
    server: Any = None
    try:
        server = pgserver.get_server(work / "pgdata", cleanup_mode="stop")
        uri = str(server.get_uri())
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(Path.home()),
            "CAPSTONE_DATABASE_URI": uri,
            # pgserver wants a runtime directory it can own; a temp one keeps a shared machine happy
            "XDG_RUNTIME_DIR": str(work),
            "PYTHONPATH": f"{STAGE / 'src'}:{STAGE / 'tests'}",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        seed_env = dict(env, CAPSTONE_SMOKE_ALLOW_DESTROY="1")
        rule("0. the cluster this log belongs to")
        say(f"built: pgserver under {work.name}, removed when this script exits")
        say(f"ddl:   {ddl}")
        say("uri:   not printed -- it is a socket path on this machine and nothing else")
        say("scale: synthetic, --orders " + orders + "; the seeder prints the row counts it wrote")

        rule("1. schema and migrations, in the order the chain applies them")
        run_psql(
            uri,
            sql="DROP SCHEMA IF EXISTS capstone CASCADE; DROP SCHEMA IF EXISTS olist CASCADE;",
            label="reset",
        )
        run_psql(uri, script=ddl, label="stage-2 olist DDL")
        for name in MIGRATIONS:
            run_psql(uri, script=STAGE / "sql" / name, label=name)
        say("  applied: the Olist DDL, then this stage's sql/001..005")

        rule("2. the audit, loaded through the shipped loader (not a hand-written INSERT)")
        run_step(
            ["-m", "capstone", "audit", "--load", "--config", "config/capstone.example.toml"],
            env=env,
            label=(
                "53 rows is the whole audit; on a fresh table every row is an insert, and the"
                " `changed:` lines are the keys the CSV added, so 53 of each is the expected shape"
            ),
        )

        rule("3. the corpus, at the scale the brief describes")
        run_step(
            ["docs/evidence/frame-performance-synthetic.py", "--orders", orders],
            env=seed_env,
            label="the seeder refuses a target that already holds rows",
        )

        rule("4. checklist 5.3 -- the timing, the plan, and the verdict")
        run_step(
            ["docs/evidence/frame-performance-run.py", "--runs", "5"],
            env=env,
            label="the ceiling is the config's; nothing here raises it",
        )

        rule("5. item 12 -- the external table, empty and then filled")
        run_step(
            ["-m", "capstone", "external", "--report", "--config", "config/capstone.example.toml"],
            env=env,
            label="the empty-table case must be printable, not fatal",
        )
        run_step(
            ["docs/evidence/frame-performance-external-demo.py"],
            env=seed_env,
            label="20 keys stored through capstone.upsert_external_municipio",
        )
        run_step(
            ["-m", "capstone", "external", "--report", "--config", "config/capstone.example.toml"],
            env=env,
            label="the same command, now with a match rate to report",
        )

        rule("6. the live guards, on the server they were written for")
        run_step(
            ["-m", "unittest", "tests.test_capstone_external", "tests.test_capstone_features"],
            env=env,
            label="with a URI set, so the skip path is not what ran",
        )
        say()
        say(
            "end note: every duration above was measured on a synthetic corpus in a temporary cluster"
            "\non a shared sandbox. They are the plan's evidence and the procedure's proof, not the"
            "\nstage's number -- that one comes from a run on the project's own PostgreSQL."
        )
        return 0
    finally:
        if server is not None:
            server.cleanup()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
