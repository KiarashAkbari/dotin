"""Shared paths for the stage-4 tests.

The three earlier stages each put their own ``src`` on ``sys.path`` from inside the
test modules, because the package is not installed -- stage 3 documents the same
trick. Keeping it in one place here means a test file imports ``helpers`` and gets a
correct path, rather than each test guessing how many ``parents[N]`` it needs.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAGE_ROOT = HERE.parent
SRC = STAGE_ROOT / "src"
#: ``None`` outside a git checkout (an exported or zipped folder): the tests that need git skip
#: on it instead of every module failing to import with StopIteration
REPO_ROOT = next((p for p in STAGE_ROOT.parents if (p / ".git").exists()), None)

for candidate in (HERE, SRC):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

#: the tracked contract: valid shape, deliberately undecided decisions
EXAMPLE_CONFIG = STAGE_ROOT / "config" / "capstone.example.toml"
#: the tracked decided contract a fresh clone runs with (decisions.md holds the reasoning)
DEFAULT_CONFIG = STAGE_ROOT / "config" / "capstone.default.toml"
#: stage 2's DDL, which is where the audit's denominator comes from
STAGE2_DDL = STAGE_ROOT.parent / "stage-2-kiarash-akbari" / "sql" / "001_create_olist_schema.sql"


def write_audit(path: Path, rows: list[str]) -> Path:
    """Write an audit file with the fixed header and the given body rows."""
    header = "table,column,available_at,at_approval,source,verdict"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def load_example_contract():
    """The tracked contract, shape-checked -- the object every scaffold test needs."""
    from capstone.config import load_contract

    return load_contract(EXAMPLE_CONFIG)


def columns_from_stage2_ddl(table: str) -> list[str]:
    """The column list of one ``olist`` table, from stage 2's DDL."""
    from capstone.audit import columns_from_ddl

    return columns_from_ddl(STAGE2_DDL)[table]
