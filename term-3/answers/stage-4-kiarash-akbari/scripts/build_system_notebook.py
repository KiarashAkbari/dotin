#!/usr/bin/env python3
"""Build and execute ``02_system.ipynb``: the final system, told from what the chain stored.

The notebook does not train anything of its own -- a second training path is how notebook numbers
drift away from the system's. It reads what the one command produced: the chain manifest (per-step
timing), the latest ``capstone.model_run`` row and ``capstone.order_score`` (model, evaluation,
drift, external), the follow-up list, and the two evidence files the harness scripts write
(cheating-model gap, sealed-cut rehearsal). Set ``CAPSTONE_NOTEBOOK_RUN_CHAIN=1`` to have its first
cell run ``scripts/run_chain.py`` itself before reading.

    python scripts/build_system_notebook.py            # build + execute in a fresh kernel
    python scripts/build_system_notebook.py --no-run   # build only
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import nbformat
from nbformat.v4 import new_code_cell, new_markdown_cell, new_notebook

STAGE = Path(__file__).resolve().parents[1]
TARGET = STAGE / "02_system.ipynb"

#: the cell bodies (Persian prose + code) live in Markdown: the layout guard forbids Persian in .py
CELLS_FILE = STAGE / "docs" / "system_notebook_cells.md"
CELL_HEAD = re.compile(r"^%% (md|code)\s*$")


def read_cells(path: Path = CELLS_FILE) -> list[tuple[str, str]]:
    """Parse ``%% md`` / ``%% code`` blocks into ``(kind, text)`` pairs, skipping the leading comment."""
    text = path.read_text(encoding="utf-8")
    if text.lstrip().startswith("<!--"):
        text = text[text.index("-->") + 3 :]
    cells: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        head = CELL_HEAD.match(line)
        if head:
            cells.append((head.group(1), []))
        elif cells:
            cells[-1][1].append(line)
    if not cells:
        raise SystemExit(f"{path} has no cells")
    return [(kind, "\n".join(body).strip("\n")) for kind, body in cells]


def build() -> nbformat.NotebookNode:
    notebook = new_notebook()
    notebook.cells = [
        new_markdown_cell(text) if kind == "md" else new_code_cell(text) for kind, text in read_cells()
    ]
    notebook.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    return notebook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-run", action="store_true")
    args = parser.parse_args(argv)
    notebook = build()
    if not args.no_run:
        from nbclient import NotebookClient

        NotebookClient(notebook, timeout=900, kernel_name="python3",
                       resources={"metadata": {"path": str(STAGE)}}).execute()
        notebook.metadata["capstone_executed"] = True
    nbformat.write(notebook, TARGET)
    print(f"wrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
