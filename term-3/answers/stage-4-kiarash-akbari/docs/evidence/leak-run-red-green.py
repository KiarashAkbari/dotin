"""Red/green harness for the anti-leak test (checklist 10.3).

usage: python3 stage-4/docs/evidence/leak-run-red-green.py red|green   > leak-<mode>-run.txt

The claim under test is small but load-bearing: ``capstone.audit.require_allowed`` refuses
a column the audit table does not allow, and ``tests/test_leak_gate.py`` notices when it
stops refusing. A test that passes for the wrong reason is the failure mode this script
exists to rule out, so the same three checks run twice:

* ``green`` -- the tracked package, unmodified. Expected: the clean frame is accepted, the
  frame with the label column added is refused, and the test module passes.
* ``red`` -- a copy of ``src/`` in which the guard's refusal is replaced by a no-op, so the
  leaked column walks straight in. Expected: the refusal is missing *and* the test module
  fails. A red run in which nothing fails would mean the test is decorative, and the
  harness reports that as its own error rather than hiding it.

The repository is never written to: ``red`` copies ``stage-4/src`` to a scratch directory
and patches the copy. The sha256 of the patched file is printed so the log shows which
source the failing run actually imported.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MODE = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
if MODE not in {"red", "green"}:
    raise SystemExit("usage: leak-run-red-green.py red|green")

STAGE_ROOT = Path(__file__).resolve().parents[2]
SRC = STAGE_ROOT / "src"
TESTS = STAGE_ROOT / "tests"
AUDIT = STAGE_ROOT / "docs" / "evidence" / "column_audit.csv"
TARGET = SRC / "capstone" / "audit.py"

#: the loop header that does the refusing, matched exactly so the patch fails loudly if
#: the guard is ever restructured under this script's feet
REAL = """    for key in used:
        row = by_key.get(key)
        if key in permitted:
            continue"""
NEUTERED = """    for key in used:
        row = by_key.get(key)
        if True:  # the neutered guard: every column is treated as permitted
            continue"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


src_root = SRC
if MODE == "red":
    work = Path(tempfile.mkdtemp(prefix="leak-red-"))
    shutil.copytree(SRC, work / "src", ignore=shutil.ignore_patterns("__pycache__"))
    target = work / "src" / "capstone" / "audit.py"
    text = target.read_text(encoding="utf-8")
    if text.count(REAL) != 1:
        raise SystemExit(f"red patch target found {text.count(REAL)} times, expected exactly 1")
    target.write_text(text.replace(REAL, NEUTERED), encoding="utf-8")
    src_root = work / "src"
    print(f"red patch    : capstone/audit.py in {src_root}")
    print(f"             : sha256={sha256(target)} (tracked file is {sha256(TARGET)}); the")
    print("               refusal branch is now unreachable, so a leak passes the gate")
else:
    print(f"green source : {TARGET} sha256={sha256(TARGET)} (unmodified)")

CHILD = """
import sys, unittest
from pathlib import Path
#: the tracked ``src`` is named first and the source under test second, so the copy that
#: wins is the one this harness chose -- ``tests/helpers.py`` refuses to re-insert a path
#: that is already present, which is what makes the red run import the patched copy
sys.path.insert(0, REAL_SRC)
sys.path.insert(0, SRC_ROOT)
sys.path.insert(0, TESTS_ROOT)

from capstone.audit import load_audit, require_allowed
from capstone.errors import AuditError

MODE = sys.argv[1]
audit = load_audit(Path(AUDIT_PATH))
allowed = sorted(row.key for row in audit if row.verdict == "allowed")
forbidden = sorted(row.key for row in audit if row.verdict == "forbidden")
print(f"audit read   : {len(allowed)} allowed, {len(forbidden)} forbidden of {len(audit)} columns")
print()

outcomes = {}

print("### 1. the clean frame: every column the audit allows")
try:
    require_allowed(audit, allowed)
    outcomes["clean_frame_accepted"] = True
    print(f"   accepted ({len(allowed)} columns)")
except AuditError as exc:
    outcomes["clean_frame_accepted"] = False
    print(f"   REFUSED: {str(exc).splitlines()[0]}")
print()

print("### 2. the leak: order_reviews.review_score added to that frame")
try:
    require_allowed(audit, [*allowed, "order_reviews.review_score"])
    outcomes["leak_refused"] = False
    print("   ACCEPTED -- the guard let the label through")
except AuditError as exc:
    outcomes["leak_refused"] = True
    print(f"   refused: {str(exc).splitlines()[0]}")
print()

print("### 3. the test module: tests/test_leak_gate.py")
suite = unittest.defaultTestLoader.loadTestsFromName("test_leak_gate")
result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
outcomes["tests_passed"] = result.wasSuccessful()
print(f"   Ran {result.testsRun} tests: failures={len(result.failures)} errors={len(result.errors)}")

expected = {
    "green": {"clean_frame_accepted": True, "leak_refused": True, "tests_passed": True},
    "red": {"clean_frame_accepted": True, "leak_refused": False, "tests_passed": False},
}[MODE]
print()
print("expectation  :", " ".join(
    f"{k}={v}{'OK' if outcomes[k] == v else 'MISMATCH'}" for k, v in expected.items()))
raise SystemExit(0 if outcomes == expected else 1)
"""

runner = (
    CHILD.replace("SRC_ROOT", repr(str(src_root)))
    .replace("REAL_SRC", repr(str(SRC)))
    .replace("TESTS_ROOT", repr(str(TESTS)))
    .replace("AUDIT_PATH", repr(str(AUDIT)))
)

print(f"mode         : {MODE}")
print(f"python       : {sys.version.split()[0]}")
print(f"audit file   : {AUDIT.relative_to(STAGE_ROOT)} sha256={sha256(AUDIT)}")
print("-" * 79)
proc = subprocess.run(
    [sys.executable, "-c", runner, MODE], capture_output=True, text=True, check=False
)
sys.stdout.write(proc.stdout)
sys.stdout.write(proc.stderr)
print("-" * 79)
print(f"exit code    : {proc.returncode}  (0 = this mode behaved as its expectation says)")
raise SystemExit(proc.returncode)
