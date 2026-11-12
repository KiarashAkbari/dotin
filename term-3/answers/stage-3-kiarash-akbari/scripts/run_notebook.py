"""Run ``solution.ipynb`` the way the grader will, and fail if the result is not deliverable.

"Restart & Run All" is a claim about a file, so it can be checked by a program: every code
cell executed, the execution numbers are contiguous from 1, no cell errored, and no absolute
path is written into a cell. The notebook's own last cells write ``data/curated/
credit_evidence.json`` and fill ``report.md`` from it, so this script is how a clean
delivered notebook is *produced* on a machine that has the data file and the database - not
how one is described.

Run from the stage-3 directory, with the same interpreter the runbook uses:

    PYTHONPATH=src python scripts/run_notebook.py                  # execute, save, validate
    PYTHONPATH=src python scripts/run_notebook.py --check           # validate what is saved
    PYTHONPATH=src python scripts/run_notebook.py --out /tmp/nb.ipynb --budget-minutes 20

Exit codes: 0 clean, 1 the notebook is not deliverable, 2 the run itself failed.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

CODE = "code"
ERROR = "error"

#: an absolute path in a cell is the delivery guide's explicit no (Windows or unix)
ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|Users|mnt|Volumes|private)/)")


def code_cells(notebook) -> list:
    return [cell for cell in notebook.cells if cell.get("cell_type") == CODE]


def error_cells(notebook) -> list[tuple[int, str]]:
    """(cell index, exception name) for every code cell whose saved output is an error."""
    found: list[tuple[int, str]] = []
    for index, cell in enumerate(notebook.cells):
        if cell.get("cell_type") != CODE:
            continue
        for output in cell.get("outputs", []):
            if output.get("output_type") == ERROR:
                found.append(
                    (
                        index,
                        f"{output.get('ename', 'Error')}: {output.get('evalue', '')}",
                    )
                )
                break
    return found


def execution_sequence(notebook) -> list[object]:
    """The execution numbers in cell order; ``None`` where a cell was never run."""
    return [cell.get("execution_count") for cell in code_cells(notebook)]


def validate_executed(notebook) -> list[str]:
    """Everything that makes an executed notebook deliverable, as a list of complaints."""
    problems: list[str] = []
    for index, message in error_cells(notebook):
        problems.append(f"cell {index} still holds an error output: {message}")

    counts = execution_sequence(notebook)
    never = [i for i, count in enumerate(counts) if count is None]
    if never:
        problems.append(
            f"{len(never)} code cell(s) were never executed (positions {never[:6]})"
        )

    run = [count for count in counts if count is not None]
    if run != list(range(1, len(run) + 1)):
        problems.append(
            "execution counts are not 1..N in cell order: "
            f"first={run[:5]} last={run[-3:] if len(run) > 3 else ''} of {len(counts)} cells"
        )

    absolute = absolute_paths(notebook)
    if absolute:
        problems.append(
            f"{len(absolute)} cell(s) contain an absolute path (relative only): "
            f"{absolute[:3]}"
        )
    return problems


def absolute_paths(notebook) -> list[str]:
    """Every absolute path a cell source contains, with its cell index."""
    hits: list[str] = []
    for index, cell in enumerate(notebook.cells):
        source = "".join(cell.get("source", []))
        for match in ABSOLUTE_PATH.finditer(source):
            hits.append(f"cell {index}: {match.group(0)}")
    return hits


def _jupyter():
    """Import the notebook toolchain, or explain the alternative.

    ``nbclient`` and ``nbformat`` arrive with Jupyter, so they are the one pair of names
    this project does not pin: nothing graded imports them - not the package, not the
    notebook - and an environment built from requirements.txt can deliver everything by
    pressing Restart & Run All in the UI. Importing them here rather than at module scope
    is what keeps that true, and a test says so.
    """
    try:
        import nbformat
        from nbclient import NotebookClient
    except ImportError as exc:
        raise SystemExit(
            f"the runner needs nbclient/nbformat ({exc}); install jupyter, or run "
            "solution.ipynb with Restart & Run All in the notebook UI"
        ) from exc
    return nbformat, NotebookClient


def execute(
    notebook_path: Path, out_path: Path, *, timeout: int, kernel: str = "python3"
) -> float:
    """Run every cell in a fresh kernel, then save the notebook with its outputs."""
    nbformat, NotebookClient = _jupyter()

    notebook = nbformat.read(notebook_path, as_version=4)
    # the kernel's cwd decides every relative path in the notebook, so pin it to the
    # notebook's own directory rather than to wherever the shell happened to be
    client = NotebookClient(
        notebook,
        timeout=timeout,
        kernel_name=kernel,
        resources={"metadata": {"path": str(notebook_path.parent)}},
    )
    started = time.monotonic()
    client.execute()
    elapsed = time.monotonic() - started
    nbformat.write(notebook, out_path)
    return elapsed


def check(path: Path) -> list[str]:
    nbformat, _ = _jupyter()
    return validate_executed(nbformat.read(path, as_version=4))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/run_notebook.py")
    parser.add_argument("--notebook", default="solution.ipynb")
    parser.add_argument("--out", default=None, help="defaults to the notebook itself")
    parser.add_argument("--timeout", type=int, default=600, help="per-cell seconds")
    parser.add_argument(
        "--budget-minutes",
        type=float,
        default=20.0,
        help="the stage's time ceiling, measured here on this machine",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the saved notebook without running it",
    )
    args = parser.parse_args(argv)

    notebook = Path(args.notebook)
    out = Path(args.out) if args.out else notebook
    if not notebook.is_file():
        print(f"notebook not found: {notebook}", file=sys.stderr)
        return 2

    if args.check:
        elapsed, verb = None, "checked"
        problems = check(notebook)
    else:
        try:
            elapsed = execute(notebook, out, timeout=args.timeout, kernel="python3")
        except Exception as exc:  # noqa: BLE001 - a failed run is the outcome, not a crash
            print(f"run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        verb = "executed"
        problems = check(out)

    budget = (
        f" elapsed={elapsed:.1f}s budget={args.budget_minutes:g}min" if elapsed else ""
    )
    print(
        f"{verb} {out.name} code_cells={len(code_cells(_read(out)))} "
        f"errors={len(error_cells(_read(out)))}{budget} problems={len(problems)}"
    )
    for line in problems:
        print(f"  - {line}", file=sys.stderr)
    if elapsed is not None and elapsed > args.budget_minutes * 60:
        print(
            f"  - over budget: {elapsed / 60:.1f} min against {args.budget_minutes:g}",
            file=sys.stderr,
        )
        return 1
    return 1 if problems else 0


def _read(path: Path):
    nbformat, _ = _jupyter()
    return nbformat.read(path, as_version=4)


if __name__ == "__main__":
    raise SystemExit(main())
