#!/usr/bin/env python3
"""Run the PostgreSQL smoke tests two ways, and prove what the offline gates cannot see.

``sql/004_as_of_functions.sql`` carried a ``DO`` block whose catalog predicate did not parse
(``oidvector = regtype[]``) and, in the next revision, one that parsed and was *always false*
(``proargtypes::oid[]`` keeps the vector's zero-based bounds, so ``[0:1]`` never equals ``[1:2]``).
Both survived ``scripts/write_sql_checksums.py``, the static signature guards and ``capstone audit``:
a manifest says a file has not changed, not that the server accepts what is in it. So this script runs
the same files past both kinds of check and prints the disagreement.

Three things are measured, in this order:

1. **the offline gates** -- ``/usr/bin/python3``, the interpreter a reader has without the dev extra,
   which skips the smoke module and says why; and the full suite, which is green on the shipped tree.
2. **a real server, both ways** -- a temp cluster started by ``pgserver``, then the same module pointed
   at that cluster through ``CAPSTONE_SMOKE_DATABASE_URI`` + ``CAPSTONE_SMOKE_ALLOW_DESTROY`` so the
   outside-server path a reader uses is exercised here too, plus the refusal that fires when the destroy
   flag is missing (a skip, not a green).
3. **the two predicates** -- each is spliced into the real file, the checksum manifest is regenerated to
   match (a manifest proves the file changed, never whether the server accepts it, so it has to move with
   the file for the comparison to be fair), and then the *same bytes* are run past both kinds of gate: the
   offline suite under an interpreter without the dev extra -- green, which is the finding -- and the smoke
   module against a real server -- red. The file and the manifest are restored afterwards and verified
   against the manifest, which is the same tripwire the reviewer gets.

   Run this with an interpreter that *does* have ``pgserver`` and the suite in step 1 also goes red on a
   mutation, which is the point of the dev extra: installing it buys a check that can read the server.

Run from ``stage-4/`` with the interpreter that has the dev extra::

    .venv/bin/python docs/evidence/postgres-smoke-run.py | tee docs/evidence/postgres-smoke-run.txt

If ``pgserver`` is missing, the script prints that the server half was not measured and exits 0 -- a
missing server is a fact about the machine, not a failed claim, but no line below pretends otherwise.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STAGE = Path(__file__).resolve().parents[2]
SQL = STAGE / "sql"
FUNCTIONS_FILE = SQL / "004_as_of_functions.sql"
MANIFEST = STAGE / "docs" / "evidence" / "sql-checksums.txt"
CHECKSUM_SCRIPT = STAGE / "scripts" / "write_sql_checksums.py"
SMOKE = "tests.test_postgres_smoke"
SUITE = ["-m", "unittest", "discover", "-s", "tests", "-t", "."]
#: the predicate as shipped, and the two shapes that look equivalent and are not
GOOD = "AND p.proargtypes = ARRAY['text'::regtype, 'timestamptz'::regtype]::oidvector"
MUTATIONS = [
    (
        "the predicate that shipped in the seventh patch",
        "AND p.proargtypes = ARRAY['text', 'timestamptz']::regtype[]",
        "operator does not exist",
    ),
    (
        "a predicate that parses and is always false",
        "AND p.proargtypes::oid[] = ARRAY['text'::regtype::oid, 'timestamptz'::regtype::oid]",
        "the drop went ahead beside a dependent view",
    ),
]
SYSTEM_PYTHON = Path("/usr/bin/python3")


def scrub(text: str) -> str:
    """The last few meaningful lines, without the noise a temp cluster prints.

    The elapsed time is dropped from every ``Ran N tests in X`` line: this log is meant to be diffable
    against the next run on a reader's machine, and a wall-clock number is the only thing that differs.
    """
    text = re.sub(r"in [0-9]+\.[0-9]+s", "in <t>s", text)
    keep = [
        line
        for line in text.splitlines()
        if line.strip()
        and "RuntimeDirWarning" not in line
        and "user_runtime" not in line
        and not line.startswith("  return ")
    ]
    return "\n".join(f"      | {line[:118]}" for line in keep[-4:])


def run(argv: list[str], *, env: dict[str, str] | None = None, python: str | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [python or sys.executable, *argv],
        cwd=STAGE,
        env={**os.environ, "PYTHONPATH": "src:tests", **(env or {})},
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def smoke(*, env: dict[str, str] | None = None, python: str | None = None) -> tuple[int, str]:
    """One run of the smoke module, with the interpreter and the environment a reader would use."""
    argv = ["-m", "unittest", SMOKE]
    if python is not None:
        proc = subprocess.run(
            [python, *argv],
            cwd=STAGE,
            env={**os.environ, "PYTHONPATH": "src:tests", **(env or {})},
            capture_output=True,
            text=True,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    return run(argv, env=env)


def head() -> tuple[str, str, str]:
    """HEAD, the tracked diff, and a note about what is deliberately not counted as drift.

    Untracked scratch is not a difference in a file, and the log cannot be committed *and* be the output
    of the run that writes it -- so it is named in the report rather than counted as drift. What matters is
    whether `sql/004_as_of_functions.sql` and the guard it exercises are the committed bytes.
    """
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=STAGE, capture_output=True, text=True
    ).stdout.strip()
    state = subprocess.run(
        ["git", "status", "--porcelain", "-uno"], cwd=STAGE, capture_output=True, text=True
    ).stdout
    own = str(Path(__file__).with_suffix(".txt").relative_to(STAGE.parent))
    tracked = "\n".join(line for line in state.splitlines() if own not in line).strip()
    extra = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=STAGE, capture_output=True, text=True
    ).stdout.splitlines()
    note = ""
    if extra:
        own_name = Path(__file__).with_suffix(".txt").name
        note = (
            f"(untracked, and untouched by this script: {len(extra)} path(s); {own_name} is not "
            "counted above either, since it is this run's own output)"
        )
    return commit, tracked, note


def main() -> int:
    problems = 0
    print("=" * 72)
    print("postgres smoke run -- what a real server says about sql/004 that the offline gates did not")
    print("=" * 72)
    commit, tracked, note = head()
    label = (
        "no tracked file differs from HEAD -- the bytes below are the committed ones"
        if not tracked
        else "TRACKED DIFF:"
    )
    print(f"tree: HEAD {commit}, {label}")
    for line in tracked.splitlines():
        print(f"      {line}")
    if note:
        print(f"      {note}")

    # platformdirs warns on stderr when there is no runtime dir, and this log is a record, not a
    # terminal: give it one before anything imports a library that cares
    os.environ.setdefault("XDG_RUNTIME_DIR", str(Path(tempfile.gettempdir()) / f"runtime-{os.getuid()}"))
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    runtime.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)  # platformdirs wants 0700 and warns on stderr about anything else

    try:
        import pgserver  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - the honest message is "not measured here"
        print(f"\nno server on this interpreter ({type(exc).__name__}: {exc})")
        print("the offline half below is still worth reading; the mutation half is not measured.")
        code, out = run(SUITE)
        print(f"full stage-4 suite, offline: exit {code}")
        print(scrub(out))
        return 0 if code == 0 else 1

    psql_dir = str(Path(str(pgserver.__file__)).parent / "pginstall" / "bin")
    workdir = Path(tempfile.mkdtemp(prefix="capstone-evidence-pg-"))
    server = pgserver.get_server(workdir / "pgdata", cleanup_mode="stop")
    uri = str(server.get_uri())
    outside = {
        "PATH": f"{psql_dir}{os.pathsep}{os.environ['PATH']}",
        "CAPSTONE_SMOKE_DATABASE_URI": uri,
        "CAPSTONE_SMOKE_ALLOW_DESTROY": "1",
    }
    original = {
        FUNCTIONS_FILE: FUNCTIONS_FILE.read_text(encoding="utf-8"),
        MANIFEST: MANIFEST.read_text(encoding="utf-8"),
    }
    try:
        version = subprocess.run(
            [f"{psql_dir}/psql", "-X", "-q", "-t", uri, "-c", "SELECT version()"],
            capture_output=True, text=True,
        ).stdout.strip()
        print("\nserver: the cluster pgserver starts on this interpreter")
        print(f"        {version}")

        print("\n" + "-" * 72)
        print("1. the gates a reader has without the dev extra")
        print("-" * 72)
        if SYSTEM_PYTHON.is_file():
            code, out = smoke(python=str(SYSTEM_PYTHON))
            print(f"   smoke module under {SYSTEM_PYTHON}: exit {code}")
            print(scrub(out))
            # a skip is only the honest answer when the interpreter cannot start a cluster; a machine
            # whose system python has pgserver installed runs these ten for real, and that is a pass too
            if code:
                print("   EXPECTED exit 0 -- either ten skips or ten runs")
                problems += 1
        else:
            print(f"   (no {SYSTEM_PYTHON} here, so the without-pgserver half was not measured)")
        code, out = run(SUITE)
        print(f"   full stage-4 suite on the shipped tree: exit {code}")
        print(scrub(out))
        problems += bool(code)

        print("\n" + "-" * 72)
        print("2. a real server, both ways the module can be given one")
        print("-" * 72)
        code, out = smoke()
        print(f"   temp cluster started by the module itself: exit {code}")
        print(scrub(out))
        problems += bool(code)
        code, out = smoke(env=outside)
        print(f"   through CAPSTONE_SMOKE_DATABASE_URI (the reader's path): exit {code}")
        print(scrub(out))
        problems += bool(code)
        refused = {k: v for k, v in outside.items() if k != "CAPSTONE_SMOKE_ALLOW_DESTROY"}
        code, out = smoke(env=refused)
        print(f"   the same URI without CAPSTONE_SMOKE_ALLOW_DESTROY=1: exit {code}")
        refused_ok = not code and "skipped=" in out
        print('      | ' + ("refuses to touch a schema it was not told to drop" if refused_ok
                            else "NOT SKIPPED -- the guard is decorative"))
        problems += not refused_ok
        # a fresh apply of the two files the patch changes, so the listing below is "after a full
        # apply" and not whatever state the last test happened to leave the cluster in
        for name in ("004_as_of_functions.sql", "005_external_signal.sql"):
            applied = subprocess.run(
                [f"{psql_dir}/psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", uri, "-f", str(SQL / name)],
                capture_output=True,
                text=True,
            )
            problems += bool(applied.returncode)
            if applied.returncode:
                print(f"   {name} did not apply on its own:\n{scrub(applied.stderr)}")
        print("   \\df capstone.* after that apply:")
        listing = subprocess.run(
            [f"{psql_dir}/psql", "-X", "-q", uri, "-c", r"\df capstone.*"],
            capture_output=True,
            text=True,
        ).stdout
        # rstripped, because a psql table pads its columns and `git am` warns about trailing whitespace
        print("\n".join(f"      | {line}".rstrip() for line in listing.splitlines() if line.strip()))
        problems += "timestamp without time zone" not in listing

        print("\n" + "-" * 72)
        print(f"3. {len(MUTATIONS)} predicates that the offline gates cannot tell from the shipped one")
        print("-" * 72)
        for what, predicate, expected in MUTATIONS:
            text = original[FUNCTIONS_FILE]
            if text.count(GOOD) != 1:
                print(f"\n   !! {what}: the anchor is not unique, so the mutation was not applied")
                problems += 1
                continue
            FUNCTIONS_FILE.write_text(text.replace(GOOD, predicate, 1), encoding="utf-8")
            subprocess.run([sys.executable, str(CHECKSUM_SCRIPT)], cwd=STAGE, capture_output=True, text=True)
            if not SYSTEM_PYTHON.is_file():
                print(f"\n   --- {what}: no {SYSTEM_PYTHON}, so the offline half of this comparison "
                      "was not measured")
                problems += 1
                FUNCTIONS_FILE.write_text(text, encoding="utf-8")
                continue
            offline_code, offline_out = run(SUITE, python=str(SYSTEM_PYTHON))
            server_code, server_out = smoke()
            print(f"\n   --- {what} ({FUNCTIONS_FILE.name}, manifest regenerated to match)")
            print(
                f"       offline gates, {SYSTEM_PYTHON if SYSTEM_PYTHON.is_file() else 'no separate interpreter'}: exit {offline_code} "
                f"({'still green, as claimed' if not offline_code else 'red -- the offline suite does see this'})"
            )
            print(f"       real server ({sys.executable}): exit {server_code} "
                  f"({'red, as intended' if server_code else 'STILL GREEN -- nothing catches it'})")
            for line in scrub(server_out).splitlines():
                print(f"     {line}")
            if offline_code or not server_code or expected not in server_out:
                print(f"       EXPECTED offline green and a server failure mentioning {expected!r}")
                problems += 1
            FUNCTIONS_FILE.write_text(text, encoding="utf-8")

        print("\n" + "=" * 72)
        restored = FUNCTIONS_FILE.read_text(encoding="utf-8") == original[FUNCTIONS_FILE]
        print("restore: " + ("files are byte-identical to what they were" if restored else "FAILED"))
        problems += not restored
    finally:
        for file, text in original.items():
            file.write_text(text, encoding="utf-8")
        shutil.rmtree(workdir, ignore_errors=True)

    check = subprocess.run(
        [sys.executable, str(CHECKSUM_SCRIPT), "--check"], cwd=STAGE, capture_output=True, text=True
    )
    print(f"checksum tripwire after the restore: {check.stdout.strip() or check.stderr.strip()}")
    problems += bool(check.returncode)
    print(
        "\nverdict: "
        + (
            f"{len(MUTATIONS)} predicates invisible to the offline gates, all caught by the server"
            if not problems
            else f"{problems} problem(s) -- read the lines above, do not trust this one"
        )
    )
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
