"""Write a fixture CSV with the real file's shape, for a run that has no Kaggle download.

Two things make this a test and not a demo. The values come from ``tests/fixtures.py``,
the same generator the offline tests pin, so its defects are countable rather than
random; and the first header cell is blanked to the empty string, which is how
``cs-training.csv`` actually arrives - that column is a row index, not a named field. A
fixture with a prettier header would have proved nothing about the header contract,
which is the thing that took two rounds to get right.

Run from the stage-3 directory:

    PYTHONPATH=src:tests python scripts/make_fixture.py                # data/raw/cs-training.csv
    PYTHONPATH=src:tests python scripts/make_fixture.py --rows 150000 --seed 20261103
    PYTHONPATH=src:tests python scripts/make_fixture.py --keep-header  # assume the real header

It prints the sha256 of what it wrote, because the load manifest records that hash and
every number in the evidence files is tied to the file that hash names. The hash is not a
promise across library versions: the bytes come from a dataframe write, so a different
pandas or numpy can render the same values differently and print a different hash. That is
not a bug to chase: the fixture is a stand-in for a run that has no Kaggle download, and on
a re-execution the values - not the bytes - are what reproduce. Both were measured, not
assumed: a fresh cluster with pandas 3.1.0 and numpy 2.4.6 regenerated this file with a
different hash and the same numbers in every field of the evidence except the two that name
the input and its timestamp (docs/evidence/fixture/run.txt).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for candidate in (ROOT / "src", ROOT / "tests", ROOT.parents[1] / "stage-1" / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import fixtures  # the offline generator the test suite pins

DEFAULT_OUT = ROOT / "data" / "raw" / "cs-training.csv"
#: pinned against the generator's own default by a test, so a change there is visible here
DEFAULT_SEED = 20261103


def blank_index_header(path: Path) -> Path:
    """Rewrite only the header line so its first cell is empty; values untouched.

    Reading the whole file back through ``csv.writer`` would re-quote and re-terminate
    every row, so the hash of a "fixture" would depend on this script's writer instead
    of on the data. Everything after the first line is copied byte for byte.
    """
    with path.open("r", encoding="utf-8", newline="") as handle:
        header = handle.readline()
        rest = handle.read()
    if not header.strip():
        raise SystemExit(f"{path} has no header line to rewrite")
    fields = next(csv.reader([header]))
    if fields[0] == "":
        return path  # already the shape of the real file
    fields[0] = ""
    rebuilt = io.StringIO()
    csv.writer(rebuilt, lineterminator="").writerow(fields)
    path.write_text(rebuilt.getvalue() + "\n" + rest, encoding="utf-8")
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/make_fixture.py")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--rows", type=int, default=150_000)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--keep-header",
        action="store_true",
        help="leave the header as fixtures.py writes it (Id) instead of blanking cell 0",
    )
    args = parser.parse_args(argv)
    out = Path(args.out)
    if out.exists():
        print(f"refusing to overwrite {out}; move it aside first")
        return 1
    fixtures.write_csv(out, rows=args.rows, seed=args.seed)
    if not args.keep_header:
        blank_index_header(out)
    print(f"wrote {out} rows={args.rows} seed={args.seed} sha256={sha256(out)}")
    print(
        "next: PYTHONPATH=src python -m credit.load --reset --credit-config config/credit.toml"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
