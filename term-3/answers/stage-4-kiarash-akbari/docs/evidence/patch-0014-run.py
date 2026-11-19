#!/usr/bin/env python3
"""Produce the log that patch 0014 ships with: the offline suite, the red/green of the refusal fix,
and the same commands against a disposable server.

Three things are recorded here, in the order a reviewer needs them:

1. the offline suite on the plain interpreter, which is the gate every patch in this set passes;
2. the fix tested *both ways* -- a scratch copy of ``src/`` with the translation removed, then the
   tree as shipped. This is the only way a refusal is worth writing down: "the test passes" proves
   nothing about what it would have done before, and the harness that produced the green log is the
   same one that produced the red one, so the two are comparable line for line;
3. the commands on a real server, because the bug was a driver exception escaping the CLI and a
   stubbed test cannot show that a closed port is now a sentence with exit code 67. psycopg has to
   be installed for that half to mean anything, which is why it runs here and not on a laptop with
   no dev extra.

What is deliberately *not* measured: the frame timings. Those belong to
``frame-performance-run.py``, and the number that closed checklist 5.3 came from the student's own
database -- see ``frame-performance-student-run.txt``. A sandbox timing added here would only offer
itself to be mistaken for that one. The corpus this script seeds is 200 synthetic orders, enough for
``v_external_match_rate`` to have a denominator that is not zero.

Run it from ``stage-4/``::

    python3 docs/evidence/patch-0014-run.py | tee docs/evidence/patch-0014-run.txt
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import textwrap
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
#: the two lines that make ``connect`` refuse politely; removing them is the whole bug
FIXED_BODY = """    try:
        connection = module.connect(dsn)
    except Exception as error:"""
BROKEN_BODY = """    connection = module.connect(dsn)
    if False:
        _ = error  # pragma: no cover - the fix, removed"""

try:
    import pgserver  # type: ignore[import-not-found]

    PSQL = Path(str(pgserver.__file__)).parent / "pginstall" / "bin" / "psql"
    IMPORT_ERROR = ""
except ModuleNotFoundError as error:  # the refusal path, recorded rather than raised
    pgserver = None  # type: ignore[assignment]
    PSQL = Path("psql")
    IMPORT_ERROR = str(error)

ELAPSED = re.compile(r" in [0-9]+\.[0-9]+s")
TEST_MODULES = ("tests.test_capstone_features", "tests.test_capstone_external")
REFUSAL_CLASSES = ("tests.test_capstone_features.ConnectRefusalTests",
                   "tests.test_capstone_external.RefusedDatabaseTests")


def say(text: str = "") -> None:
    print(text, flush=True)


def rule(title: str) -> None:
    say()
    say("-" * 72)
    say(title)
    say("-" * 72)


def scrub(lines: list[str]) -> list[str]:
    """Unittest's elapsed clause is the one part of a log that can never repeat; drop it."""
    return [ELAPSED.sub(r" in <t>s", line) for line in lines]


def step(
    script: list[str],
    *,
    env: dict[str, str],
    label: str = "",
    interpreter: str | None = None,
    tail: int = 0,
    cwd: Path | None = None,
) -> tuple[int, str]:
    """One child process, its output inlined, and its exit code handed back for a check to use."""
    command = f"$ {(interpreter or 'python3')} {' '.join(script)}"
    if len(command) > 96:  # the echo is part of the log, so it wraps like a shell would
        say(textwrap.fill(command, width=96, subsequent_indent="  ... "))
    else:
        say(command)
    proc = subprocess.run(
        [interpreter or sys.executable, *script],
        cwd=str(cwd or STAGE),
        env=env,
        capture_output=True,
        text=True,
    )
    text = proc.stdout + (("\n" + proc.stderr) if proc.stderr.strip() else "")
    shown = text.splitlines() if not tail else text.splitlines()[-tail:]
    for line in scrub(shown):
        say(f"  | {line}"[:99])
    say(f"  [exit {proc.returncode}] {label}")
    return proc.returncode, text


def check(name: str, ok: bool, detail: str = "") -> bool:
    say(f"  check[{name}]: {'ok' if ok else 'FAILED'}{(' -- ' + detail) if detail else ''}")
    return ok


def ddl_path() -> Path | None:
    """stage-2's Olist DDL, in either tree layout -- the same glob the other scripts use."""
    name = "001_create_olist_schema.sql"
    candidates = [ROOT / "stage-2" / "sql" / name, *sorted(ROOT.glob(f"stage-2*/sql/{name}"))]
    return next((path for path in candidates if path.is_file()), None)


COPY_INTO_SCRATCH = ("src", "tests", "config", "sql", "docs/evidence")


def unpatched_tree(dest: Path) -> Path:
    """A scratch stage-4 with the refusal translation removed -- the red half.

    Whole directories, not just ``src``: each test module puts ``Path(__file__).parents[1] / "src"``
    on ``sys.path`` itself, so a scratch ``tests`` directory drags a scratch ``src`` with it and needs
    ``config/`` and ``docs/evidence/`` to be there too. Copying only the source would have the red run
    import the fixed tree and print a reassuring green -- which is the exact failure mode a
    red/green harness has to be built to avoid.
    """
    for name in COPY_INTO_SCRATCH:
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(STAGE / name, dest / name)
    target = dest / "src" / "capstone" / "features.py"
    text = target.read_text(encoding="utf-8")
    if text.count(FIXED_BODY) != 1:
        raise SystemExit(
            "the anchor this harness removes did not match features.connect, so the red run would not"
            " be the bug: fix the script before believing the green one"
        )
    target.write_text(text.replace(FIXED_BODY, BROKEN_BODY), encoding="utf-8")
    compile_check = subprocess.run(
        [sys.executable, "-c", f"import ast,pathlib;ast.parse(pathlib.Path({str(target)!r}).read_text())"],
        capture_output=True,
        text=True,
    )
    if compile_check.returncode:
        raise SystemExit(f"the scratch copy does not even parse: {compile_check.stderr[:300]}")
    return dest


def main(argv: list[str]) -> int:
    plain = shutil.which("python3") or sys.executable
    failures = 0

    rule("1. the offline suite, plain interpreter, no database and no driver")
    base_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "PYTHONPATH": f"{STAGE / 'src'}:{STAGE / 'tests'}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    code, text = step(
        ["-m", "unittest", "discover", "-s", "tests", "-t", "."],
        env=base_env,
        interpreter=plain,
        label="the whole suite, with the two new classes in it",
        tail=12,
    )
    failures += not check("offline suite green", code == 0, f"exit {code}")
    tally = re.search(r"Ran (\d+) tests", text)
    verdict = re.search(r"^(OK\b.*|FAILED.*)$", text, re.M)
    if tally and verdict:
        say(f"  tally: {tally.group(1)} tests -- {verdict.group(1).strip()}")
    else:
        say("  tally: unreadable -- the child did not print a unittest summary")

    rule("2. the same guards against the bug put back")
    with tempfile.TemporaryDirectory(prefix="capstone-red-") as tmp:
        scratch = unpatched_tree(Path(tmp))
        red_env = dict(base_env, PYTHONPATH=f"{scratch / 'src'}:{scratch / 'tests'}")
        code_red, text_red = step(
            ["-m", "unittest", *REFUSAL_CLASSES],
            env=red_env,
            interpreter=plain,
            label="expected RED: without the translation, a refused port escapes as a traceback",
            tail=16,
            cwd=scratch,
        )
        failures += not check(
            "red run fails on the driver error",
            code_red != 0 and "OperationalError" in text_red,
            f"exit {code_red}",
        )
    code_green, text_green = step(
        ["-m", "unittest", *REFUSAL_CLASSES],
        env=base_env,
        interpreter=plain,
        label="GREEN on the tree as shipped",
        tail=8,
    )
    failures += not check("green run passes", code_green == 0, f"exit {code_green}")

    if pgserver is None:
        rule("3. skipped: no server here")
        say(
            "not measured: the dev extra is not installed on this interpreter, so there is no cluster to"
            " point a closed port at. Install it (``pip install pgserver 'psycopg[binary]'``) and re-run,"
            " or run this section on your own machine against a port you know is closed -- which is the"
            " point of the check. Refusal: " + IMPORT_ERROR
        )
        say("  (exit 0 for this section: a machine without the dev extra has not failed a measurement)")
        return 0 if failures == 0 else 1

    work = Path(tempfile.mkdtemp(prefix="capstone-0014-"))
    server: Any = None
    try:
        server = pgserver.get_server(work / "pgdata", cleanup_mode="stop")
        uri = str(server.get_uri())
        env = dict(
            base_env,
            CAPSTONE_DATABASE_URI=uri,
            XDG_RUNTIME_DIR=str(work),
            CAPSTONE_SMOKE_ALLOW_DESTROY="1",
        )
        ddl = ddl_path()

        rule("3. a disposable server, and the two paths through --report")
        if ddl is None:
            say("  stage-2's DDL is not beside this checkout, so `olist` cannot be created; the")
            say("  refusal checks below still run against an empty database.")
        else:
            subprocess.run(
                [str(PSQL), uri, "-v", "ON_ERROR_STOP=1", "-q", "-c",
                 "DROP SCHEMA IF EXISTS capstone CASCADE; DROP SCHEMA IF EXISTS olist CASCADE;"],
                capture_output=True, text=True, check=False,
            )
            for script in (ddl, *(STAGE / "sql" / name for name in MIGRATIONS)):
                proc = subprocess.run(
                    [str(PSQL), uri, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(script)],
                    capture_output=True, text=True, check=False,
                )
                failures += not check(
                    f"applied {script.name}", proc.returncode == 0, proc.stderr.strip()[:160]
                )
        step(
            ["-m", "capstone", "audit", "--load", "--config", "config/capstone.example.toml"],
            env=env,
            label="the audit the guard asks for, through the shipped loader",
            tail=4,
        )
        step(
            ["docs/evidence/frame-performance-synthetic.py", "--orders", "200"],
            env=env,
            label="200 synthetic orders, so the rate has a denominator",
            tail=6,
        )
        code_live, text_live = step(
            ["-m", "capstone", "external", "--report", "--config", "config/capstone.example.toml"],
            env=env,
            label="the normal path: a rate, read from the view, nothing stored",
            tail=8,
        )
        failures += not check("report on a live server exits 0", code_live == 0, f"exit {code_live}")
        failures += not check("it names the denominator", "keys =" in text_live)

        closed_port = "postgresql+psycopg://nobody:apassword@127.0.0.1:5599/none"
        closed = dict(env, CAPSTONE_DATABASE_URI=closed_port)
        code_refused, text_refused = step(
            ["-m", "capstone", "external", "--report", "--config", "config/capstone.example.toml"],
            env=closed,
            label="the bug: a well-formed uri whose port is closed",
            tail=8,
        )
        failures += not check(
            "refused port exits 67", code_refused == 67, f"exit {code_refused}"
        )
        failures += not check(
            "no traceback escapes", "Traceback (most recent call last)" not in text_refused
        )
        failures += not check("the driver's own words survive", "refused" in text_refused.lower())
        failures += not check(
            "the password is not echoed", "apassword" not in text_refused
        )

        rule("4. the live guards, on the server they were written for")
        code_tests, _ = step(
            ["-m", "unittest", *TEST_MODULES],
            env=env,
            label="with a URI set, so the skip path is not what ran",
            tail=6,
        )
        failures += not check("live modules green", code_tests == 0, f"exit {code_tests}")

        say()
        say(f"summary: {0 if failures == 0 else failures} check(s) failed")
        say("end note: the refusals above were produced against a real driver and a real socket; the")
        say("frame timings are not in this file and the 200-order corpus is synthetic. Neither number")
        say("is the stage's -- the one that closed 5.3 is in frame-performance-student-run.txt.")
        return 0 if failures == 0 else 1
    finally:
        if server is not None:
            server.cleanup()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
