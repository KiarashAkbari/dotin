"""Restart & Run All solution.ipynb headless (fresh kernel), saving its outputs.

    OLIST_DB_PASSWORD=... [OLIST_CONFIG=...] python scripts/run_notebook.py
"""
from pathlib import Path
from time import perf_counter

import nbformat
from nbclient import NotebookClient

STAGE2 = Path(__file__).resolve().parents[1]
NOTEBOOK = STAGE2 / "solution.ipynb"


def main() -> None:
    notebook = nbformat.read(NOTEBOOK, as_version=4)
    started = perf_counter()
    NotebookClient(notebook, timeout=600, kernel_name="python3",
                   resources={"metadata": {"path": str(STAGE2)}}).execute()
    nbformat.write(notebook, NOTEBOOK)
    print(f"executed {NOTEBOOK.name} in {perf_counter() - started:.1f} s")


if __name__ == "__main__":
    main()
