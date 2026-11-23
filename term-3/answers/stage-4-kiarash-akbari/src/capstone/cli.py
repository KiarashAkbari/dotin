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
    load_from_csv,
    summarise,
    template_rows,
)
from .chain import statuses
from .config import CapstoneConfig, load_config, load_contract
from .doctor import report as doctor_report
from .doctor import run as run_checks
from . import external as external_signal
from .errors import AuditError, CapstoneError, ConfigError
from .features import database_uri
from .exit_codes import ExitCode
from .predict import score_file

#: the local file first, the tracked contract second -- the same order stage 3 uses
#: a machine-local override first (gitignored), then the decided tracked contract, then the
#: undecided example -- so a fresh clone runs with the decisions recorded in decisions.md
CONFIG_CANDIDATES = ("capstone.toml", "capstone.default.toml", "capstone.example.toml")


def default_config_path(root: Path) -> Path:
    """Where the config lives, without anyone having to remember it."""
    for name in CONFIG_CANDIDATES:
        candidate = root / "config" / name
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"no config in {root / 'config'}; expected one of {', '.join(CONFIG_CANDIDATES)}. "
        "A fresh clone ships config/capstone.default.toml with the recorded decisions; "
        "restore it from git rather than deciding again"
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
    return _stage_root().parent / "stage-2" / "sql" / "001_create_olist_schema.sql"


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
    """Validate the column audit, say how much of the schema it covers, and mirror it into the DB.

    ``--load`` is the subcommand the chain depends on: ``sql/001`` creates ``capstone.column_audit``,
    ``sql/002``'s guard reads it and refuses everything when it is empty, and nothing else in this
    stage writes it. Without this the one-command chain dies at its third step on a missing
    prerequisite, which is a setup error a reviewer should never have to diagnose from a leak message.
    """
    config = _load(args, decided=False)
    if args.load:
        report = load_from_csv(config, csv_path=Path(args.audit) if args.audit else None)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        else:
            print(
                f"{report['table']}: {report['rows']} rows from {Path(report['csv']).name}"
                f" -- {report['inserted']} inserted, {report['unchanged']} unchanged,"
                f" {report['rewritten']} rewritten, {report['removed']} removed"
            )
            for key in report["changed_keys"]:
                print(f"  changed: {key}")
        return ExitCode.OK
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


def _run_features(config: CapstoneConfig) -> dict:
    """The one step whose inputs are self-contained: the config, and the audit beside it.

    The audit is handed over as a cross-check, not as the permission mechanism -- ``build`` asks
    the database for the frame's real column set and refuses there. The reading of the file moved
    into ``features.run``, which is what ``capstone.chain.run_step`` calls, so this command and
    the runner ask one question of one file instead of two.
    """
    from .features import run  # late: a step the interpreter may not be able to run

    return run(config)


def _run_load_audit(config: CapstoneConfig) -> dict:
    """The audit mirror as a chain step, so a fresh database is not a mystery at step three.

    Deliberately not folded into ``features``: the frame's guard treats the table as settled truth,
    and a layer that quietly repopulates the thing that decides its own permissions is how an audit
    stops being an audit. Separate step, separate timing line, separate exit code.
    """
    from .audit import load_from_csv  # late, for the same reason as features.build

    return load_from_csv(config)


#: step name -> how the CLI runs it. Absent means "implemented, but the state it needs has no
#: carrier yet", which is a phase-6 decision rather than something to guess at in a dispatcher.
STEP_ADAPTERS = {"load-audit": _run_load_audit, "features": _run_features}


def command_step(args: argparse.Namespace) -> int:
    """Run one layer of the chain from the command line, and print what it measured.

    Two front-ends, one implementation: ``scripts/run_chain.py`` walks the chain in-process through
    ``capstone.chain.run_step``, and this command is what a person types. Both go through
    ``STEP_ADAPTERS``, so a step cannot be runnable from the shell and broken from the runner, or
    the other way round. Readiness comes from ``require_ready``, so a placeholder answers with the
    roadmap phase and the checklist items it belongs to instead of an ``AttributeError``.

    One asymmetry, deliberate: this command loads the *decided* config, while ``audit --load`` gets
    by without the four student decisions. A mirror load is safe either way, but a chain step is
    part of a run that cannot finish without them -- and a step that accepted less than its runner
    would let the same work report two different readiness states.
    """
    from .chain import require_ready

    config = _load(args, decided=True)
    step = require_ready(args.step)
    adapter = STEP_ADAPTERS.get(step.name)
    if adapter is None:
        raise ConfigError(
            f"step {step.name!r} exists but has no command adapter: its inputs are not "
            "self-contained (model needs the frame, capacity-list needs the scores the model "
            "produced), and what carries that state -- the database, the manifest, or one process "
            "walking the whole chain -- is phase 6's decision, not a guess made here",
            context={"step": step.name, "phase": step.phase},
        )
    print(json.dumps(adapter(config), ensure_ascii=False, sort_keys=True, default=str))
    return ExitCode.OK


def command_external(args: argparse.Namespace) -> int:
    """The external signal: what is configured, and -- if asked -- the fetch, the load, the match rate.

    Four modes, in the order a person needs them. With no flags this prints the configuration and
    stops, which is the answer on a machine that cannot reach the endpoint. ``--fetch`` performs one
    request and reports how many rows came back, writing nothing and needing no database. ``--report``
    reads ``capstone.v_external_match_rate`` from the server. ``--load`` does both, storing through
    ``capstone.upsert_external_municipio`` first -- it fetches by itself rather than storing the empty
    list it was never handed. Every failure is a ``SourceError`` or a ``NotReadyError`` with a sentence
    attached, because an unavailable external source is a finding to report, not a crash to debug.
    """
    config = _load(args, decided=False)
    if not (args.fetch or args.load or args.report):
        try:
            uri = database_uri(config, purpose="report the external signal")
        except ConfigError:
            # "no database on this machine" is a state this command reports, not a failure; the
            # status block below says which half is missing instead of printing a traceback
            uri = None
        status = external_signal.status(config, uri=uri)
        if args.json:
            print(json.dumps(status, ensure_ascii=False, sort_keys=True))
        else:
            for key, value in status.items():
                print(f"{key}: {value}")
        return ExitCode.OK
    report = external_signal.refresh(
        config,
        fetch_rows=args.fetch or args.load,
        store=args.load,
        report=args.report or args.load,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return ExitCode.OK
    fetched = report["fetched"]
    print(f"fetched: {fetched} row(s)" if fetched is not None else "fetched: not attempted")
    reference = report.get("uf_reference")
    if reference:
        # the second GET is a cost the reader is owed a line about: "one request per run" is a policy,
        # and a policy that bends silently is a policy nobody believes two months later. The name count
        # rides along because the list now supplies the city half of the key as well, and a key built
        # from a list entry without a name keeps the answer's "City - UF" spelling instead -- a
        # difference that is invisible in the count of rows and decides whether the join matches.
        line = (
            f"      uf map: {reference['codes']} of {reference['entries']} municipality codes and"
            f" their names from {reference['url']}"
        )
        named, codes = reference.get("named"), reference["codes"]
        if named is not None and named != codes:
            line += f" ({codes - named} without a name, keyed on the answer's spelling)"
        print(line)
    if report["match_rate"] is None:
        print("match rate: not read (pass --report or --load to ask the database)")
    else:
        print(external_signal.describe(config, report["match_rate"], report["stored"]))
        for row in report["unmatched_cities"] or []:
            print(f"      unmatched: {row['city']}|{row['uf']} ({row['orders']} orders)")
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
    audit.add_argument(
        "--load",
        action="store_true",
        help="mirror the CSV into capstone.column_audit through CAPSTONE_DATABASE_URI (replaces"
        " the table, so a re-run is a no-op and a deleted CSV row disappears)",
    )
    audit.add_argument("--json", action="store_true")
    audit.set_defaults(func=command_audit)

    chain = sub.add_parser("chain", parents=(_CONFIG_PARENT,), help="run the whole chain (or report the blocked step)")
    chain.add_argument("--dry-run", action="store_true", help="print the plan without running it")
    chain.add_argument("--json", action="store_true")
    chain.set_defaults(func=command_chain)

    # one command per step this stage owns, generated from the chain itself: adding a step to
    # CHAIN without a command would leave scripts/run_chain.py calling a name argparse rejects
    from .chain import CHAIN

    for step in CHAIN:
        if step.owner != "capstone":
            continue
        name = step.name.replace("-", "_")
        # the underscore form is what argparse names a function; the hyphenated one is what
        # ``--dry-run`` prints and what a person copies out of the plan, so both have to parse
        step_parser = sub.add_parser(
            name,
            parents=(_CONFIG_PARENT,),
            aliases=[] if name == step.name else [step.name],
            help=f"run the {step.name!r} step of the chain (roadmap phase {step.phase})",
        )
        step_parser.set_defaults(func=command_step, step=step.name)

    external = sub.add_parser(
        "external",
        parents=(_CONFIG_PARENT,),
        help="the external signal: what is configured, then fetch and match rate on request",
    )
    external.add_argument("--fetch", action="store_true", help="call the configured endpoint once")
    external.add_argument(
        "--report",
        action="store_true",
        help="read capstone.v_external_match_rate from the database (implied by --load)",
    )
    external.add_argument(
        "--load",
        action="store_true",
        help="store what the fetch returned through capstone.upsert_external_municipio",
    )
    external.add_argument("--json", action="store_true")
    external.set_defaults(func=command_external)

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
