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
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

STAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE_ROOT / "src"))

# The capstone modules are imported inside the functions that use them: the path above
# has to exist first, and a module-level import there is the one thing every style gate
# in this repository complains about. Import cost is paid once, on the first call.


def python_for_stage() -> str:
    """The interpreter the README documents: stage 1's venv, else the active one."""
    venv = STAGE_ROOT.parent / "stage-1-kiarash-akbari" / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else sys.executable


def external_step_command(step) -> list[str]:
    """Steps stage 1 owns run through stage 1's own CLI, with this stage's config."""
    if step.name == "ingest":
        return [python_for_stage(), "-m", "data_factory", "fetch"]
    if step.name == "load-database":
        return [python_for_stage(), "-m", "data_factory", "load-database"]
    return [python_for_stage(), "-m", "capstone", step.name.replace("-", "_")]


def plan() -> list[dict]:
    """The steps with their reason, for ``--dry-run`` and for the report's appendix."""
    from capstone.chain import CHAIN, statuses

    rows = statuses()
    for row, step in zip(rows, CHAIN, strict=True):
        if step.owner != "capstone":
            row["command"] = " ".join(external_step_command(step))
        else:
            row["command"] = f"python -m capstone {step.name} (via {step.entrypoint})"
    return rows


def execute(args: argparse.Namespace, timer) -> int:
    """Run each step, one layer each, and stop at the first refusal.

    ``timer`` is a :class:`capstone.timing.ChainTimer`, built by the caller so the
    manifest can still be written when this function raises.
    """
    from capstone import chain as capstone_chain
    from capstone.chain import CHAIN
    from capstone.errors import CapstoneError
    from capstone.exit_codes import ExitCode

    for step in CHAIN:
        with timer.layer(step.name):
            if step.owner != "capstone":
                command = external_step_command(step)
                result = subprocess.run(command, capture_output=True, text=True, check=False)
                if result.returncode != 0:
                    raise CapstoneError(
                        f"step {step.name!r} failed in stage 1 with exit "
                        f"{result.returncode}: {(result.stderr or result.stdout).strip()[:200]}",
                        context={"command": command},
                    )
                continue
            # one positional, the config: the contract lives in capstone.chain.run_step, and the
            # tests in tests/test_run_chain.py hold every step in CHAIN to it. Passing argv here is
            # what used to break -- `load_from_csv(config)` took the extra [] as a second argument
            # and died, while `features.build(config, audit)` accepted it as an empty audit.
            capstone_chain.run_step(step, args.loaded_config)
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
