"""``python -m capstone`` -- the one command, before there is anything to run.

Two things make this worth writing at scaffold time instead of phase 6. The command
names and their exit codes are the interface the README and the notebooks will end up
documenting, so pinning them now is cheaper than renaming them later. And a step that
refuses with a phase number and a checklist item is a better answer than a traceback:
an evaluator who runs ``chain`` on the scaffold learns exactly what is missing.

Exit codes come from :mod:`capstone.exit_codes`, which mirrors stage 1's.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import (
    AUDIT_HEADER,
    audit_problems,
    columns_from_ddl,
    coverage,
    load_audit,
    summarise,
    template_rows,
)
from .chain import statuses
from .config import CapstoneConfig, load_config, load_contract
from .doctor import report as doctor_report
from .doctor import run as run_checks
from .errors import AuditError, CapstoneError, ConfigError
from .exit_codes import ExitCode
from .predict import score_file

#: the local file first, the tracked contract second -- the same order stage 3 uses
CONFIG_CANDIDATES = ("capstone.toml", "capstone.example.toml")


def default_config_path(root: Path) -> Path:
    """Where the config lives, without anyone having to remember it."""
    for name in CONFIG_CANDIDATES:
        candidate = root / "config" / name
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"no config in {root / 'config'}; expected one of {', '.join(CONFIG_CANDIDATES)}. "
        "A fresh clone ships only the example: copy it to capstone.toml and fill the "
        "student decisions in (ROADMAP phase 1)"
    )


def config_path_from(args: argparse.Namespace) -> Path:
    """The config this invocation should read, whichever side of the subcommand it came from.

    Two spellings are accepted, and both have to work because the help screen and the README
    show the second one: ``capstone --config F doctor`` (a global option) and
    ``capstone doctor --config F`` (an option of the command, which is what a person types
    when they read ``capstone doctor --help``). Only the subparser can overwrite the global
    value, so the more specific one wins, and an explicitly named file that does not exist is
    a config failure rather than a reason to quietly fall back to the default: falling back
    would make a typo look like an undecided contract, and a typo is much cheaper to report
    than to explain afterwards.
    """
    named = getattr(args, "config_cli", None) or getattr(args, "config", None)
    if named:
        path = Path(named)
        if not path.is_file():
            raise ConfigError(
                f"no config file at {path}; the name was given on the command line, so it is "
                "not replaced by the default"
            )
        return path
    return default_config_path(_stage_root())


def _load(args: argparse.Namespace, *, decided: bool) -> CapstoneConfig:
    path = config_path_from(args)
    return load_config(path) if decided else load_contract(path)


def _stage_root() -> Path:
    """``stage-4/`` -- resolved from this file so the command works from any directory."""
    return Path(__file__).resolve().parents[2]


def _ddl_path(config: CapstoneConfig, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    return _stage_root().parent / "stage-2-kiarash-akbari" / "sql" / "001_create_olist_schema.sql"


def command_doctor(args: argparse.Namespace) -> int:
    """Report what a fresh machine would trip over."""
    config = _load(args, decided=False)
    checks = run_checks(config)
    print(
        json.dumps([check.as_dict() for check in checks], ensure_ascii=False)
        if args.json
        else doctor_report(config)
    )
    blocking = [check for check in checks if not check.ok and check.severity == "blocking"]
    if not blocking:
        return ExitCode.OK
    # the first failure an evaluator would hit decides the code, so 78 and 65 stay tellable
    if any(check.name == "config" for check in blocking):
        return ExitCode.CONFIG
    if any(check.name == "column audit" for check in blocking):
        return ExitCode.DATA
    return ExitCode.NOT_READY


def command_audit(args: argparse.Namespace) -> int:
    """Validate the column audit, and say how much of the schema it covers."""
    config = _load(args, decided=False)
    if args.print_header:
        print(",".join(AUDIT_HEADER))
        return ExitCode.OK
    if args.template_out:
        rows = columns_from_ddl(_ddl_path(config, args.ddl))
        out = Path(args.template_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(template_rows(rows), encoding="utf-8")
        total = sum(len(columns) for columns in rows.values())
        print(f"wrote {out} with {total} unverified rows over {len(rows)} tables")
        return ExitCode.OK
    audit = load_audit(Path(args.audit) if args.audit else config.audit_path)
    problems = audit_problems(audit)
    summary = summarise(audit)
    if args.coverage:
        summary["coverage_missing"] = coverage(audit, columns_from_ddl(_ddl_path(config, args.ddl)))
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        for key, value in summary.items():
            print(f"{key}: {value}")
    if problems:
        for problem in problems:
            print(f"problem: {problem}", file=sys.stderr)
        return ExitCode.DATA
    if args.require_complete and summary.get("coverage_missing"):
        missing = summary["coverage_missing"]
        raise AuditError(
            "the audit is not complete; unaudited columns per table: "
            + ", ".join(f"{table}={count}" for table, count in sorted(missing.items())),
            context={"missing": missing},
        )
    return ExitCode.OK


def command_chain(args: argparse.Namespace) -> int:
    """Walk the five steps -- or say which one does not exist yet."""
    rows = statuses()
    if args.dry_run:
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            for row in rows:
                mark = "ready" if row["ready"] else "blocked"
                print(f"[{mark:7}] {row['name']:15} phase {row['phase']}  {row['entrypoint']}")
                if row["reason"]:
                    print(f"          {row['reason']}")
        return ExitCode.OK
    config = _load(args, decided=True)
    from .pending import is_pending  # late import: only the real run needs the marker

    for row in rows:
        module_name, _, attribute = row["entrypoint"].partition(":")
        try:
            module = __import__(module_name, fromlist=[attribute or module_name])
        except ImportError as exc:
            raise CapstoneError(
                f"step {row['name']!r} needs {module_name}, which this interpreter cannot "
                f"import ({exc}); stage 1's package is on the path via stage-1/src, see README.md"
            ) from exc
        function = getattr(module, attribute or "main", None)
        if function is None:
            raise CapstoneError(
                f"step {row['name']!r} points at {row['entrypoint']}, which is absent"
            )
        if is_pending(function):
            function(config, [])  # raises NotReadyError with the phase and the items
    return ExitCode.OK


def command_predict(args: argparse.Namespace) -> int:
    """Validate a fresh order file and hand it to the scoring layer."""
    config = _load(args, decided=True)
    rows = score_file(Path(args.input), config)
    print(json.dumps(rows, ensure_ascii=False))
    return ExitCode.OK


#: ``--config`` again, this time accepted *after* the command: ``capstone doctor --help`` is
#: what a person reads, and a option the help screen shows must not be rejected when used
_CONFIG_PARENT = argparse.ArgumentParser(add_help=False)
_CONFIG_PARENT.add_argument(
    "--config",
    dest="config_cli",
    help="path to capstone.toml, overriding the default config",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capstone",
        description="the stage-4 chain, one command per layer",
    )
    parser.add_argument(
        "--config",
        help="path to capstone.toml (default: stage-4/config/capstone.toml, else the example)",
    )
    # the same option on every subcommand, under a second dest: argparse's last writer wins,
    # so sharing one dest would let the subparser's default of None erase a global value
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", parents=(_CONFIG_PARENT,), help="what a stranger's machine is missing")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=command_doctor)

    audit = sub.add_parser("audit", parents=(_CONFIG_PARENT,), help="check the column time audit")
    audit.add_argument(
        "--audit", help="audit CSV path (default: the config's time_guard.audit_file)"
    )
    audit.add_argument("--ddl", help="DDL to measure coverage against (default: stage-2's 001)")
    audit.add_argument(
        "--coverage", action="store_true", help="also report columns missing from the audit"
    )
    audit.add_argument(
        "--require-complete", action="store_true", help="exit with DATA if any column is unaudited"
    )
    audit.add_argument("--print-header", action="store_true", help="print the CSV header and exit")
    audit.add_argument(
        "--template-out", help="write one unverified row per DDL column to this path"
    )
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=command_audit)

    chain = sub.add_parser("chain", parents=(_CONFIG_PARENT,), help="run the whole chain (or report the blocked step)")
    chain.add_argument("--dry-run", action="store_true", help="print the plan without running it")
    chain.add_argument("--json", action="store_true")
    chain.set_defaults(func=command_chain)

    predict = sub.add_parser("predict", parents=(_CONFIG_PARENT,), help="rank a fresh order file")
    predict.add_argument("--input", required=True, help="a CSV with the orders schema")
    predict.set_defaults(func=command_predict)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CapstoneError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(exc.exit_code)
    except FileNotFoundError as exc:  # argparse/open() slips
        print(f"error: {exc.filename or exc}", file=sys.stderr)
        return int(ExitCode.DATA)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
