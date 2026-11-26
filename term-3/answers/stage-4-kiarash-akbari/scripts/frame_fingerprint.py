#!/usr/bin/env python3
"""Fingerprint every row and every column of ``capstone.v_feature_frame``.

A rewrite of the frame's SQL that is meant to change *how* the rows are computed and not *what* they
are needs a check that does not trust the rewrite: this prints three lines that two runs, one per
version of the view, must print identically.

* ``columns`` -- the view's own catalog row: every column's name and ``format_type`` (typmod
  included), in ``attnum`` order, hashed. A reordered, renamed or retyped column moves it.
* ``rows``    -- ``count(*)`` of the frame.
* ``digest``  -- ``md5`` over the ``md5`` of each row's text form (``f::text``: every column, in
  order, as PostgreSQL renders it), concatenated in ``order_id`` order. ``order_id`` is the frame's
  key, so the order is total and the digest does not depend on the plan.

Usage::

    CAPSTONE_DATABASE_URI=... python3 scripts/frame_fingerprint.py [--label before]

Nothing is written to the database; the URI is never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))

FRAME_VIEW = "capstone.v_feature_frame"

COLUMNS_SQL = (
    "SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute AS a"
    " WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
)
DIGEST_SQL = (
    "SELECT count(*), md5(string_agg(md5(f::text), '' ORDER BY f.order_id))"
    " FROM {frame} AS f"
)


def column_signature(columns: list[tuple[str, str]]) -> str:
    """One line per column, ``name type``, hashed -- the shape the frame promises its readers."""
    text = "\n".join(f"{name} {kind}" for name, kind in columns)
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    from capstone.features import connect

    import os

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", default="", help="a word printed in front of each line")
    args = parser.parse_args(argv)
    uri = (os.environ.get("CAPSTONE_DATABASE_URI") or "").strip()
    if not uri:
        print("error: CAPSTONE_DATABASE_URI is not set; nothing to fingerprint", file=sys.stderr)
        return 78
    connection = connect(uri, purpose="fingerprint the feature frame")
    try:
        cursor = connection.cursor()
        cursor.execute(COLUMNS_SQL, (FRAME_VIEW,))
        columns = [(str(name), str(kind)) for name, kind in cursor.fetchall()]
        cursor.execute(DIGEST_SQL.format(frame=FRAME_VIEW))
        rows, digest = cursor.fetchone()
    finally:
        connection.close()
    prefix = f"{args.label}: " if args.label else ""
    print(f"{prefix}columns {len(columns)} {column_signature(columns)}")
    print(f"{prefix}rows {int(rows)}")
    print(f"{prefix}digest {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
