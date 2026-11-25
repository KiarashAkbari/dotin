#!/usr/bin/env python3
"""``run_chain.py`` -- the one command, written before the chain can run.

``04`` asks for a single command that walks receive → load → features → model → the
10 % list, inside 45 minutes from an empty environment, with each layer's time in the
manifest. That is not a notebook concern and not a phase-6 surprise: it is a loop over
``capstone.chain.CHAIN`` with a timer around each iteration, so writing it now fixes
the interface the phases have to grow into, and makes "the chain ran" mean "every step
returned" rather than "the last script in the folder finished".

Today the loop refuses at the first step whose package is not on the interpreter's
path, and writes a manifest that says so. ``--dry-run`` prints the plan and touches
nothing, which is what the doctor and the README point a stranger at.

One prerequisite used to live only in a README: ``capstone.column_audit`` is created by
``sql/001``, read by the guard in ``sql/002``, and *refuses everything while it is empty* --
fail-closed, which is the right instinct and the wrong error for a fresh database. It is now a step
of its own, ``load-audit``, ordered before ``features``, so "the chain ran" includes "the audit the
guard consults was loaded". Nothing needs to be pre-loaded by hand any more; to see what that step
did, run it on its own::

    python3 -m capstone load_audit --config config/capstone.toml

and the same load without Python, for whoever holds the database directly::

    python3 scripts/load_column_audit.py | psql "$CAPSTONE_DATABASE_URI" -v ON_ERROR_STOP=1 -f -
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

STAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_ROOT / "src"))

# The capstone modules are imported inside the functions that use them: the path above
# has to exist first, and a module-level import there is the one thing every style gate
# in this repository complains about. Import cost is paid once, on the first call.


def plan() -> list[dict]:
    """The steps with their entry point and readiness, for ``--dry-run`` and the report's appendix."""
    from capstone.chain import statuses

    rows = statuses()
    for row in rows:
        row["command"] = f"python -m capstone {row['name']} (via {row['entrypoint']})"
    return rows


INGEST_STEP = "ingest"
LOAD_STEP = "load-database"


def effective_skip(skip: list[str] | None) -> tuple[list[str], str | None]:
    """Which steps not to run, plus the note saying why that list is longer than what was asked.

    ``--skip load-database`` is how "the corpus is already in the database" is expressed, and it is
    the shape the runbook tells a reader to use. ``ingest`` is the *verify* half of that same load: it
    refuses when the CSV folder is missing, which is correct just before a load and wrong when the
    whole point is that the loading happened elsewhere. Measured on a machine holding 99,441 orders in
    PostgreSQL, with the CSVs one directory over inside another stage's tree: the documented skip died
    at the verify step and never reached ``model``, so the manifest recorded a failure nobody had made.
    The pair now travels together, and the note is printed because a step that disappears from a run
    plan silently is how a delivery starts lying about what it executed.
    """
    chosen = list(skip or [])
    if LOAD_STEP in chosen and INGEST_STEP not in chosen:
        chosen.append(INGEST_STEP)
        return chosen, (
            f"note: {INGEST_STEP} skipped with {LOAD_STEP} -- verifying a CSV folder is the load's "
            "first half, not a property of the database. If the corpus is elsewhere, point "
            f"CAPSTONE_OLIST_DIR at it (data/raw/olist is the default) and pass --skip {LOAD_STEP} "
            "alone to run the verify anyway"
        )
    return chosen, None


def execute(args: argparse.Namespace, timer) -> int:
    """Run each step in its own timed layer, and stop at the first failure.

    Every step is ``entrypoint(config)`` through :func:`capstone.chain.run_step`; the two stage-1
    steps are capstone functions that run stage 1's CLI from stage 1's folder
    (:mod:`capstone.ingest`). A step's own exit code is kept: a stage-1 failure leaves with stage
    1's code, a missing source with 69, not a flattened 1 (review B20). ``--skip`` exists for the
    smoke run on a database that is already loaded; the manifest records the skip, including the one
    :func:`effective_skip` adds on the reader's behalf.
    """
    from capstone import chain as capstone_chain
    from capstone.chain import CHAIN
    from capstone.exit_codes import ExitCode

    results: dict[str, object] = {}
    skipped, note = effective_skip(args.skip)
    if note is not None:
        print(note)
    for step in CHAIN:
        if step.name in skipped:
            results[step.name] = "skipped"
            continue
        with timer.layer(step.name):
            results[step.name] = capstone_chain.run_step(step, args.loaded_config)
        print(json.dumps({"step": step.name, "result": results[step.name]}, default=str))
    args.skip = skipped
    args.results = results
    return int(ExitCode.OK)


def main(argv: list[str] | None = None) -> int:
    from capstone.cli import default_config_path
    from capstone.config import load_config, load_contract
    from capstone.errors import CapstoneError
    from capstone.exit_codes import ExitCode
    from capstone.timing import ChainTimer

    parser = argparse.ArgumentParser(
        description="run the whole stage-4 chain, or print what blocks it"
    )
    parser.add_argument("--config", type=Path, help="capstone.toml to act on")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    parser.add_argument("--manifest", type=Path, help="where to write the timing manifest")
    parser.add_argument(
        "--skip",
        action="append",
        metavar="STEP",
        help="leave one step out (repeatable), e.g. --skip load-database on a loaded database",
    )
    args = parser.parse_args(argv)

    config_path = args.config or default_config_path(STAGE_ROOT)
    try:
        args.loaded_config = (
            load_config(config_path) if not args.dry_run else load_contract(config_path)
        )
    except CapstoneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(exc.exit_code)

    if args.dry_run:
        print(json.dumps(plan(), ensure_ascii=False, indent=2, sort_keys=True))
        return int(ExitCode.OK)

    timer = ChainTimer(budget_minutes=args.loaded_config.budget.chain_minutes)
    try:
        try:
            code = execute(args, timer)
        except CapstoneError as exc:
            print(f"error: {exc}", file=sys.stderr)
            code = int(exc.exit_code)
    finally:
        # the manifest is written in a `finally`, so an unexpected crash still leaves a
        # record behind: a run that wrote nothing is a run nobody can reconstruct, and
        # the traceback stays on stderr for whoever reads this file afterwards
        target = args.manifest or (
            args.loaded_config.features.curated_dir
            / f"chain_manifest_{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
        )
        written = timer.write_manifest(Path(target))
        print(f"manifest: {written}")
    if timer.over_budget:
        print(
            f"warning: the chain used {timer.seconds_used:.1f} s of a "
            f"{timer.budget_minutes} minute budget",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
