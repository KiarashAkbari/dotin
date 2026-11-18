#!/usr/bin/env python3
"""Write and check ``docs/evidence/sql-checksums.txt``, the SQL tamper tripwire.

``stage-4/sql/README.md`` section 4 requires one line per shipped file: a sha256 and the
file name. The point is that the *bytes* the reviewer applies are the bytes that were
reviewed -- an edit to a migration after it has been applied does not repair anybody's
database, it makes every later load fail (stage 3 learned that the hard way, and its
``test_credit_sql_checksums.py`` is the model for this tool).

Usage::

    python3 scripts/write_sql_checksums.py            # write the manifest
    python3 scripts/write_sql_checksums.py --check    # exit 1 if the manifest drifted

``--check`` prints the diff it found and never rewrites the file: a drift is a thing a
human decides about, not a thing a tool silently repairs.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1]
SQL_DIR = STAGE / "sql"
MANIFEST = STAGE / "docs" / "evidence" / "sql-checksums.txt"
HEADER = (
    "# sha256 of every file under stage-4/sql, one per line, sorted by path -- the order the "
    "# migrations are applied in. Regenerate with: python3 scripts/write_sql_checksums.py"
)


def checksum_lines(root: Path = SQL_DIR) -> list[str]:
    """Return ``"<sha256>  <name>"`` for every ``*.sql`` under *root*, sorted by name."""
    lines: list[str] = []
    for path in sorted(root.rglob("*.sql")):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(root.parent).as_posix()}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    args = parser.parse_args(argv)

    lines = checksum_lines()
    body = "\n".join([HEADER, *lines]) + "\n"
    if not args.check:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(body, encoding="utf-8")
        print(f"wrote {len(lines)} lines to {MANIFEST.relative_to(STAGE)}")
        return 0

    if not MANIFEST.exists():
        print(f"missing: {MANIFEST.relative_to(STAGE)} -- run without --check")
        return 1
    recorded = [
        line.strip()
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if recorded == [line.strip() for line in lines]:
        print(f"ok: {len(lines)} files match the manifest")
        return 0
    print("drift between the files and the manifest:")
    for line in sorted(set(recorded) ^ set(line.strip() for line in lines)):
        print(f"  {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
