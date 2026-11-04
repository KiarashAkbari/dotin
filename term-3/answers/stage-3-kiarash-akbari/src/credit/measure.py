"""Run stage 3's measurement queries and freeze what they said.

The two files in ``sql/queries`` are the evidence for the two claims the challenge
grades hardest - that the odd values are codes rather than counts, and that
missingness carries information. They were written as .sql so a reader can run them
with psql and see the same numbers; this module runs them through the same
SQLAlchemy connection the notebook uses, so the notebook's numbers and the .sql
files cannot disagree.

:func:`write_json` is how those numbers reach ``report.md``: one file, sorted keys,
written atomically. Sorting is safe *here* because an evidence file is a set of
findings - order carries no meaning - unlike ``credit.prep``'s persisted statistics,
where key order is the fitted column order and must survive a round trip.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

from .tables import md_table

#: the queries, by the short name the notebook and the evidence file use
QUERIES: dict[str, str] = {
    "sentinel_spikes": "queries/credit_sentinel_spikes.sql",
    "missingness_parity": "queries/credit_missingness_parity.sql",
}
#: a table in the notebook is only useful up to a point; the file keeps everything
PREVIEW_ROWS = 20


def query_path(sql_dir: Path, name: str) -> Path:
    try:
        file = QUERIES[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown measurement {name!r}; expected one of {sorted(QUERIES)}"
        ) from exc
    path = Path(sql_dir) / file
    if not path.is_file():
        raise FileNotFoundError(f"measurement query not found: {path}")
    return path


def read_statements(sql_dir: Path, name: str) -> list[str]:
    """The file split into runnable statements, comment lines dropped.

    These files are written for ``psql -f`` first, which means several SELECTs in one
    file - one table per question. Splitting on ``;`` is safe for files of this shape:
    no semicolon appears inside a string literal, and dropping comments first is what
    keeps a ``--;`` in prose from looking like a boundary. The test suite pins each
    file's statement count, so adding a query without looking at the index cannot
    silently change what the notebook prints.
    """
    text = query_path(sql_dir, name).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    statements = [part.strip() for part in code.split(";") if part.strip()]
    if not statements:
        raise ValueError(f"{QUERIES[name]} contains no statement")
    return statements


def statement_count(sql_dir: Path, name: str) -> int:
    return len(read_statements(Path(sql_dir), name))


def read_query(sql_dir: Path, name: str, *, statement: int = 0) -> str:
    """One statement from a measurement file, by position, without its semicolon."""
    statements = read_statements(sql_dir, name)
    if not 0 <= statement < len(statements):
        raise ValueError(
            f"{QUERIES[name]} has {len(statements)} statements, asked for index {statement}"
        )
    return statements[statement]


def run_query(
    engine: sa.Engine,
    name: str,
    *,
    sql_dir: Path = Path("sql"),
    statement: int = 0,
) -> pd.DataFrame:
    """Execute one statement of one measurement and return it as a frame.

    Column names are what the notebook prints, so they survive exactly as the query
    aliases them; the numbers belong to the .sql file, not to this module.
    """
    sql = read_query(Path(sql_dir), name, statement=statement)
    with engine.connect() as connection:
        result = connection.execute(sa.text(sql))
        columns = list(result.keys())
        rows = result.fetchall()
    frame = pd.DataFrame.from_records([tuple(row) for row in rows], columns=columns)
    return frame.infer_objects()


def frame_evidence(frame: pd.DataFrame) -> dict[str, object]:
    """Shape, preview and the full row set - enough for the report, small enough to diff."""
    return {
        "rows": len(frame),
        "columns": list(frame.columns),
        "preview": json.loads(frame.head(PREVIEW_ROWS).to_json(orient="records")),
        "table": md_table(frame) if len(frame) else "(empty result)",
    }


def load_run_provenance(
    engine: sa.Engine, *, schema: str = "credit"
) -> dict[str, object]:
    """The ingest's own record of what produced these numbers.

    Quoted rather than remembered: the report's first claim is about the file that
    was loaded, its hash and its reject count, and the database has that row.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
        raise ValueError(f"schema {schema!r} is not a lowercase SQL identifier")
    sql = (
        f"SELECT source_kind, file_name, sha256, rows_loaded, rejected_count, fingerprint, "
        f"loaded_at FROM {schema}.load_run ORDER BY run_id DESC LIMIT 1"
    )
    with engine.connect() as connection:
        row = connection.execute(sa.text(sql)).mappings().first()
    if row is None:
        raise ValueError(
            f"no load_run row in {schema}; run python -m credit.load first"
        )
    out = dict(row)
    out["loaded_at"] = str(out["loaded_at"])
    return out


def collect(
    engine: sa.Engine,
    *,
    sql_dir: Path = Path("sql"),
    schema: str = "credit",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Every measured number of this stage's analysis-first half, in one payload."""
    evidence: dict[str, object] = {"queries": {}, "load_run": {}}
    for name in QUERIES:
        frame = run_query(engine, name, sql_dir=sql_dir)
        entry = frame_evidence(frame)
        entry["statements_in_file"] = statement_count(Path(sql_dir), name)
        evidence["queries"][name] = entry
    evidence["load_run"] = load_run_provenance(engine, schema=schema)
    if extra:
        evidence.update(extra)
    return evidence


def write_json(path: Path, payload: dict) -> Path:
    """Write JSON atomically, with sorted keys, so two runs can be diffed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def numbers(evidence: dict) -> dict[str, float | int]:
    """Flatten the payload into ``dotted.path -> number`` for traceability checks."""
    found: dict[str, float | int] = {}

    def walk(prefix: str, node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(f"{prefix}.{key}" if prefix else str(key), value)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(f"{prefix}[{index}]", value)
        elif isinstance(node, bool):
            found[prefix] = int(node)
        elif isinstance(node, (int, float)):
            found[prefix] = node

    walk("", evidence)
    return found
