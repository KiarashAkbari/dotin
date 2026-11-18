#!/usr/bin/env python3
"""Run the phase-4 SQL guards red on purpose, then green, and keep both logs.

Every check in ``tests/test_capstone_sql_guards.py`` that runs without a database is a claim
about text: "no star expansion", "every object is replaceable", "no feature reads a column the
audit does not allow". A green run proves such a claim only if the check would have fired on a
file that breaks it, and nobody knows that without trying. So this script writes the forbidden
variants listed in ``MUTATIONS``, runs the matching test for each, and restores the bytes -- the
count is printed from the list, so this paragraph cannot go out of date.

It mutates the real ``stage-4/sql`` directory because that is what the tests read -- there is
no "load from here" switch to pull, and adding one would be a second, untested path. The
restore is in a ``finally`` and the last thing the script does is compare the directory against
the committed checksum manifest, which is the same tripwire the reviewer gets: if this script
is interrupted, the next test run says so.

Usage::

    python3 docs/evidence/sql-guards-run-red-green.py            # red+green, printed
    python3 docs/evidence/sql-guards-run-red-green.py | tee docs/evidence/sql-guards-run.txt
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STAGE = Path(__file__).resolve().parents[2]
SQL = STAGE / "sql"
FRAME_FILE = SQL / "003_feature_views.sql"
SCHEMA_FILE = SQL / "001_capstone_schema.sql"
FUNCTIONS_FILE = SQL / "004_as_of_functions.sql"

#: (what is broken, which file, the old text, the new text, which test must notice)
MUTATIONS = [
    (
        "a feature reads the label",
        FRAME_FILE,
        "FROM capstone.v_decision_moment AS a\nJOIN olist.orders AS o ON o.order_id = a.order_id",
        "FROM capstone.v_decision_moment AS a\nJOIN olist.orders AS o ON o.order_id = a.order_id\n"
        "LEFT JOIN (SELECT order_id, review_score FROM olist.order_reviews) AS leaky"
        " ON leaky.order_id = a.order_id",
        [
            "tests.test_capstone_sql_guards.StaticSqlContractTests"
            ".test_no_feature_reads_a_column_the_audit_does_not_allow",
            "tests.test_capstone_sql_guards.StaticSqlContractTests"
            ".test_the_feature_side_never_names_the_reviews_table",
        ],
    ),
    (
        "a frame built with SELECT *",
        FRAME_FILE,
        "CREATE OR REPLACE VIEW capstone.v_zip_centroid AS\nSELECT\n",
        "CREATE OR REPLACE VIEW capstone.v_zip_centroid AS\nSELECT *\n",
        "tests.test_capstone_sql_guards.StaticSqlContractTests"
        ".test_no_star_expansion_and_no_write_against_the_source_schema",
    ),
    (
        "a table that cannot be re-created",
        SCHEMA_FILE,
        "CREATE TABLE IF NOT EXISTS capstone.build_run (",
        "CREATE TABLE capstone.build_run (",
        "tests.test_capstone_sql_guards.StaticSqlContractTests.test_every_object_is_replaceable",
    ),
    (
        "a comment naming a signature nothing creates",
        FUNCTIONS_FILE,
        "COMMENT ON FUNCTION capstone.customer_order_volume_before(text, timestamp) IS",
        "COMMENT ON FUNCTION capstone.customer_order_volume_before(text, timestamptz) IS",
        "tests.test_capstone_sql_guards.StaticSqlContractTests"
        ".test_a_comment_never_names_a_signature_the_directory_does_not_create",
    ),
]

def run_test(dotted: str) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", dotted],
        cwd=STAGE,
        env={"PYTHONPATH": "src:tests", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def main() -> int:
    backup = Path(tempfile.mkdtemp(prefix="capstone-sql-"))
    original = {}
    failures = 0
    try:
        for file in (FRAME_FILE, SCHEMA_FILE, FUNCTIONS_FILE):
            original[file] = file.read_text(encoding="utf-8")
            shutil.copy2(file, backup / file.name)

        print("=" * 72)
        print(f"RED: {len(MUTATIONS)} forbidden files, each should break exactly its own guard")
        print("=" * 72)
        for what, file, old, new, dotted in MUTATIONS:
            dotted_list = dotted if isinstance(dotted, list) else [dotted]
            text = original[file]
            if text.count(old) != 1:
                print(f"\n!! {what}: anchor not unique in {file.name} -- mutation not applied")
                failures += 1
                continue
            file.write_text(text.replace(old, new, 1), encoding="utf-8")
            print(f"\n--- {what} ({file.name})")
            any_red = False
            for name in dotted_list:
                code, out = run_test(name)
                any_red = any_red or bool(code)
                print(
                    f"    {name.rsplit('.', 1)[-1]:55s} exit {code} "
                    f"({'red, as intended' if code else 'still green'})"
                )
                if code:
                    for line in [x for x in out.splitlines() if x.strip()][-2:]:
                        print(f"      | {line[:118]}")
            if not any_red:
                print("    STILL GREEN: none of these guards bites -- the check is decorative")
                failures += 1
            file.write_text(original[file], encoding="utf-8")

        print()
        print("=" * 72)
        print("GREEN: the directory as shipped, full stage-4 suite")
        print("=" * 72)
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=STAGE,
            env={"PYTHONPATH": "src:tests", "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )
        print(proc.stdout.strip()[-400:] or proc.stderr.strip()[-400:])
        code = run_test(
            "tests.test_capstone_sql_guards.StaticSqlContractTests"
            ".test_no_shipped_migration_is_edited_in_place"
        )[0]
        print(
            "\nchecksum tripwire after the restore: "
            + ("matches the manifest again" if code == 0 else "STILL DRIFTED -- restore failed")
        )
        if code:
            failures += 1
    finally:
        for file, text in original.items():
            file.write_text(text, encoding="utf-8")
        shutil.rmtree(backup, ignore_errors=True)

    verdict = f"all {len(MUTATIONS)} guards fired; suite green" if not failures else f"{failures} problem(s)"
    print(f"\nverdict: {verdict}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
