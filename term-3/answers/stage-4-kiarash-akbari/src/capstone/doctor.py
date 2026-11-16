"""What a stranger's machine is missing, in one command.

``00`` says the whole project is worthless if it does not run on the grader's machine,
and ``04`` asks for a README a stranger can follow. A doctor turns that from a hope
into a checklist with exit codes: it reads the same files the chain reads, and reports
each gap with the checklist item that owns it.

Nothing here fixes anything, and nothing here measures. Severity is the contract:
``blocking`` means the chain cannot run, ``warn`` means it can run but a deliverable is
missing, ``info`` is context for the report.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .audit import audit_problems, load_audit, summarise
from .chain import statuses
from .config import CapstoneConfig, documented_decisions, load_config
from .errors import CapstoneError
from .predict import expected_columns

#: the python floor the chain inherits from stage 1's pyproject
MIN_PYTHON = (3, 12)
#: deliverable files the brief names for this stage
DELIVERABLES = (
    ("README.md", "9.5"),
    ("report.md", "9.3"),
    ("decisions.md", "9.4"),
    ("manager_summary.md", "9.6"),
    ("requirements.txt", "2.9"),
    ("01_exploration.ipynb", "9.1"),
    ("02_system.ipynb", "9.2"),
)


@dataclass(frozen=True)
class Check:
    """One finding."""

    name: str
    ok: bool
    severity: str
    detail: str
    checklist: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "detail": self.detail,
            "checklist": self.checklist,
        }

    def as_line(self) -> str:
        mark = "ok  " if self.ok else ("FAIL" if self.severity == "blocking" else "warn")
        tail = f" (checklist {self.checklist})" if self.checklist else ""
        return f"[{mark}] {self.name}: {self.detail}{tail}"


def _python_check() -> Check:
    found = sys.version_info[:2]
    ok = found >= MIN_PYTHON
    return Check(
        "python",
        ok,
        "blocking" if not ok else "info",
        f"{found[0]}.{found[1]} (stage 1 declares >={'.'.join(map(str, MIN_PYTHON))}; tomllib "
        "needs 3.11 at minimum)",
        "2.9",
    )


def _config_check(config: CapstoneConfig, decided: bool) -> Check:
    name = config.source.name
    if decided:
        return Check("config", True, "info", f"{name}: every student decision is written down")
    return Check(
        "config",
        False,
        "blocking",
        f"{name} still carries undecided keys: {', '.join(config.undecided_keys())}",
        "3.3, 3.4, 3.6, 12.1",
    )


def _audit_check(config: CapstoneConfig) -> tuple[Check, list]:
    path = config.audit_path
    try:
        audit = load_audit(path)
    except CapstoneError as exc:
        return (
            Check("column audit", False, "blocking", str(exc), "4.1"),
            [],
        )
    problems = audit_problems(audit)
    counts = summarise(audit)
    detail = (
        f"{path.name}: {counts['rows']} rows over {counts['tables']} tables, "
        f"{counts['usable']} usable, {counts['forbidden']} forbidden, "
        f"{counts['unverified']} unverified"
    )
    if problems:
        detail += f"; {len(problems)} contradiction(s): {problems[0]}"
    return Check(
        "column audit", not problems, "blocking" if problems else "info", detail, "4.1"
    ), audit


def _sql_check(config: CapstoneConfig) -> Check:
    directory = config.features.sql_migrations_dir
    files = sorted(directory.glob("*.sql")) if directory.is_dir() else []
    if not files:
        return Check(
            "feature SQL",
            False,
            "blocking",
            f"no versioned .sql under {directory}; the feature layer has no home yet "
            "(a hand-built view in pgAdmin does not count)",
            "5.1, 5.5",
        )
    return Check(
        "feature SQL",
        True,
        "info",
        f"{len(files)} file(s): {', '.join(file.name for file in files)}",
        "5.5",
    )


def _chain_check() -> Check:
    rows = statuses()
    blocked = [row["name"] for row in rows if not row["ready"]]
    if blocked:
        return Check(
            "chain steps",
            False,
            "blocking",
            "not ready yet: " + ", ".join(blocked),
            "11.3",
        )
    return Check("chain steps", True, "info", "all five steps point at real code", "11.3")


def _input_contract_check(config: CapstoneConfig) -> Check:
    try:
        columns = expected_columns(config)
    except CapstoneError as exc:
        return Check("input schema", False, "warn", str(exc), "8.2")
    return Check(
        "input schema",
        True,
        "info",
        f"{len(columns)} columns expected in a fresh order file, read from the stage-2 DDL",
        "8.2",
    )


def _deliverables_check(root: Path, config: CapstoneConfig) -> list[Check]:
    checks = []
    for name, item in DELIVERABLES:
        path = root / name
        if path.is_file() and path.stat().st_size > 0:
            continue
        checks.append(
            Check(
                name,
                False,
                "warn" if name != "requirements.txt" else "blocking",
                "missing" if not path.is_file() else "present but empty",
                item,
            )
        )
    decisions = root / "decisions.md"
    try:
        load_config(config.source)
    except CapstoneError:
        return checks  # undecided keys are already reported by the config check
    gaps = documented_decisions(decisions, config)
    for gap in gaps:
        checks.append(Check("decisions.md covers it", False, "warn", gap, "9.4"))
    return checks


def run(config: CapstoneConfig) -> list[Check]:
    """Every check, blocking ones first -- the order a person fixes them in."""
    checks: list[Check] = [_python_check()]
    try:
        load_config(config.source)
        checks.append(_config_check(config, True))
    except CapstoneError:
        checks.append(_config_check(config, False))
    audit_check, _ = _audit_check(config)
    checks += [
        audit_check,
        _sql_check(config),
        _input_contract_check(config),
        _chain_check(),
    ]
    checks += _deliverables_check(config.root, config)
    checks.sort(key=lambda check: (check.ok, check.severity != "blocking"))
    return checks


def report(config: CapstoneConfig) -> str:
    """Human-readable output, one line per check."""
    lines = [check.as_line() for check in run(config)]
    return "\n".join(lines) if lines else "nothing to report"
