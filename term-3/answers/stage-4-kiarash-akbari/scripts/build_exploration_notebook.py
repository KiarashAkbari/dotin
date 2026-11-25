#!/usr/bin/env python3
"""Generate ``01_exploration.ipynb`` by running its cells, never by hand.

The delivery section of ``04`` asks for two notebooks in place of ``solution.ipynb``, and 9.1's
evidence note is the sharp half of the requirement: the artifact has to be *produced*, not typed. A
hand-written notebook is a document with a ``.ipynb`` extension -- nobody can tell whether the
numbers in it were ever computed, which is the exact failure mode this stage exists to avoid. So
this script reads the cell bodies from ``docs/exploration_notebook_cells.md``, executes the code
cells in one namespace when told to, and writes the outputs it actually got into the JSON. There is
no path by which a cell's output can be typed in.

Why the prose lives in a Markdown file and not here: the layout guard forbids Persian in a ``.py``
source, and this notebook's prose is Persian, because the deliverable is Persian. Splitting the two
is the guard working as intended rather than being routed around -- the notebook's argument is in
Markdown, its arithmetic is in code, and the only writer of the ``.ipynb`` is this file.

What it refuses, and why the refusal is the point:

* **overwriting a notebook it did not write.** The repository's warning about
  ``stage-3/scripts/build_solution_notebook.py`` is about exactly this: a builder that runs in place
  destroys whichever hand-written pass somebody was proud of. A file without this script's name in
  its ``metadata`` is refused; ``--force`` overrides, and says so out loud, because a silent override
  is an override nobody reads;
* **pretending to have executed.** Without ``--execute`` the code cells carry ``execution_count:
  null`` and no outputs, a note cell at the top says the notebook has not been run, and the exit code
  is 3 -- so a Makefile or CI step cannot swallow the plan as a measurement;
* **writing a notebook whose cells cannot compile.** The cell bodies call into ``capstone``, so a
  renamed helper is a broken notebook three months from now. Every code cell is compiled before
  anything is written, and a failure exits 2 rather than producing an artifact that looks finished;
* **quietly failing a cell.** A cell that raises writes its traceback into the notebook as an ``error``
  output and exits 4. An exploration that found nothing interesting is a finding; an exploration whose
  author deleted the interesting error is a document.

The cells go through ``capstone.*`` instead of re-deriving things in notebook SQL: the label comes
from :func:`capstone.model.read_frame`, the audit is read by :func:`capstone.audit.load_audit` -- the
same reader the CLI uses -- so the notebook and ``capstone audit`` cannot disagree about what was
verified. A notebook that reimplements the pipeline is a second source of truth.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

STAGE = Path(__file__).resolve().parents[1]

#: written into the notebook's metadata; the guard against building over a hand-written notebook
#: reads this, so it is a marker and a version at once -- bump it when the cell list changes shape
BUILDER = "scripts/build_exploration_notebook.py"
MARKER_KEY = "capstone_builder"

#: where the cell bodies live, in the language the deliverable is written in
CELLS_FILE = STAGE / "docs" / "exploration_notebook_cells.md"

#: the sections checklist 9.1 names, in the order the notebook tells them. Each markdown cell ends
#: with ``<!--section:NAME-->``, which a reader never sees and a test can count -- so the check is
#: "the section exists", not "the Persian prose still says the words I guessed". Prose gets reworded;
#: an id in an HTML comment does not.
SECTIONS = (
    "intent", "context", "label", "missing", "time-audit", "external", "plots", "hypotheses",
    "failures",
)

#: appended to the "not executed" note cell, so a test or a doctor can check the claim in ASCII
NOT_RUN_MARK = "<!--capstone:not-executed-->"

#: the file's cell kinds, as written in the Markdown source
KINDS = {"md": "markdown", "code": "code", "note": "note"}

#: exit codes, so a caller can tell "not run yet" from "ran and found a problem"
EXIT_OK = 0
EXIT_BAD_CELLS = 2
EXIT_NOT_RUN = 3
EXIT_CELL_FAILED = 4

#: ``%% md`` / ``%% code`` / ``%% note`` start a cell; everything until the next one is its body
CELL_HEAD = re.compile(r"^%% (md|code|note)\s*$")


class BuilderError(Exception):
    """A build that must not produce a file: bad cells, or a notebook somebody else owns."""


def split_lines(text: str) -> list[str]:
    """The line list a notebook cell holds, with each line's own newline kept.

    A notebook stores ``source`` as a list of lines, and the shape is load-bearing: join it into one
    string and the cell is unreadable in a diff, drop the newlines and the rendered Markdown
    collapses paragraphs. ``splitlines(keepends=True)`` is the one call that gets both right.
    """
    return text.splitlines(keepends=True) or [""]


def read_cells(path: Path) -> list[tuple[str, str]]:
    """Parse the cell file into ``(kind, text)`` pairs, in order.

    A leading HTML comment is the file's own documentation and is skipped. The header line of each
    cell decides its kind; anything that is not a recognised ``%%`` line is body text, which keeps
    SQL, Python and Persian prose from needing any escaping.
    """
    if not path.is_file():
        raise BuilderError(
            f"no cell file at {path}. The notebook's prose and code live there; without it the "
            "builder would be inventing a deliverable, which is what it exists to prevent"
        )
    lines = path.read_text(encoding="utf-8").splitlines()
    index = 0
    while index < len(lines) and lines[index].strip() == "":
        index += 1
    if index < len(lines) and lines[index].startswith("<!--"):
        while index < len(lines) and not lines[index].rstrip().endswith("-->"):
            index += 1
        index += 1
    cells: list[tuple[str, list[str]]] = []
    for line in lines[index:]:
        head = CELL_HEAD.match(line)
        if head:
            cells.append((KINDS[head.group(1)], []))
            continue
        if cells:
            cells[-1][1].append(line)
    if not cells:
        raise BuilderError(f"{path} has no '%% md' or '%% code' lines, so there is no notebook to write")
    return [(kind, "\n".join(body).strip("\n")) for kind, body in cells]


def compile_cells(cells: list[tuple[str, str]]) -> list[str]:
    """Every syntax error in the notebook's code cells, before the file is written.

    Returns descriptions rather than raising, because the useful message lists all of them: a cell
    file with two problems is edited in one pass, and a builder that reports the first and stops
    turns a build into a guessing game.
    """
    problems: list[str] = []
    for number, (kind, text) in enumerate(cells, 1):
        if kind != "code":
            continue
        try:
            compile(text, f"{CELLS_FILE.name}#cell{number}", "exec")
        except SyntaxError as exc:
            problems.append(f"cell {number}: {exc.msg} (line {exc.lineno})")
    return problems


def with_section_markers(cells: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Append ``SECTIONS[i]`` to the i-th markdown cell, so the ids track the prose.

    Counting markdown cells rather than naming the id inside each block keeps the two from drifting
    apart: insert a section in the middle and the ids renumber themselves, and the check below is
    what says so. A file whose section count no longer matches 9.1's list is refused, because the
    ids are the only thing a reviewer can hold the notebook to.
    """
    out: list[tuple[str, str]] = []
    index = 0
    for kind, text in cells:
        if kind == "markdown" and index < len(SECTIONS):
            text = f"{text}\n\n<!--section:{SECTIONS[index]}-->"
            index += 1
        out.append((kind, text))
    if index != len(SECTIONS):
        raise BuilderError(
            f"the cell file has {index} '%% md' cells but checklist 9.1 names {len(SECTIONS)} "
            f"sections ({', '.join(SECTIONS)}); add the section in the Markdown, or say in the "
            "checklist why one is no longer delivered"
        )
    return out


def markdown(text: str) -> dict[str, Any]:
    return {"cell_type": "markdown", "metadata": {}, "source": split_lines(text)}


def code_cell(source: str, *, executed: bool, index: int, outputs: list[dict]) -> dict[str, Any]:
    return {
        "cell_type": "code",
        "execution_count": index if executed else None,
        "metadata": {},
        "outputs": outputs,
        "source": split_lines(source),
    }


def stream_output(text: str) -> dict[str, Any]:
    return {"name": "stdout", "output_type": "stream", "text": split_lines(text)}


def execute(cells: list[tuple[str, str]], *, start_namespace: dict[str, Any] | None = None) -> tuple[list[dict], int, list[str]]:
    """Run every code cell in one shared namespace, collecting the outputs the run produced.

    The namespace is shared on purpose: a notebook whose cells cannot see each other is a pile of
    scripts. Nothing is hidden from a cell either -- the same ``print`` a reader would type works --
    and a failing cell is reported with its traceback rather than skipped, because the exploration
    that found a problem is the one worth delivering.
    """
    rendered: list[dict] = []
    failures: list[str] = []
    namespace: dict[str, Any] = dict(start_namespace or {})
    namespace.setdefault("json", json)
    index = 0
    for kind, text in cells:
        if kind != "code":
            if kind == "markdown":
                rendered.append(markdown(text))
            continue
        index += 1
        outputs: list[dict] = []
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                exec(compile(text, f"<cell {index}>", "exec"), namespace)
            captured = buffer.getvalue()
            if captured:
                outputs.append(stream_output(captured))
        except Exception:  # noqa: BLE001 - a failing cell is a result, and it has to be visible
            name, value = sys.exc_info()[:2]
            rendered.append(
                code_cell(
                    text,
                    executed=True,
                    index=index,
                    outputs=outputs
                    + [
                        {
                            "ename": getattr(name, "__name__", "Exception"),
                            "evalue": str(value),
                            "output_type": "error",
                            "traceback": traceback.format_exc().splitlines(),
                        }
                    ],
                )
            )
            failures.append(f"cell {index}: {getattr(name, '__name__', 'Exception')}: {value}")
            continue
        rendered.append(code_cell(text, executed=True, index=index, outputs=outputs))
    return rendered, index, failures


def render(rendered: list[dict], *, executed: bool) -> dict[str, Any]:
    """Assemble the document, with this builder's name in the metadata and unique cell ids.

    ``id`` on every cell is what nbformat 4.5 requires; a duplicate makes Jupyter refuse the file,
    which is the loudest possible way for a deliverable to be missing, so the ids are positional and
    generated here rather than written by hand.
    """
    cells = []
    for position, cell in enumerate(rendered):
        item = dict(cell)
        item["id"] = f"capstone-{position:02d}"
        cells.append(item)
    return {
        "cells": cells,
        "metadata": {
            MARKER_KEY: BUILDER,
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "pygments_lexer": "ipython3"},
            "capstone_executed": executed,
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def run_in_kernel(document: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Execute the un-executed document top to bottom in a fresh Jupyter kernel (nbclient).

    The ``exec`` path below runs every cell in this interpreter's namespace, which is fast but is
    not "Restart & Run All" and cannot render a figure (review B: no plots). This is the real thing:
    one new kernel, cells in order, outputs (images included) as the kernel produced them.
    """
    import nbformat
    from nbclient import NotebookClient

    notebook = nbformat.from_dict(document)
    NotebookClient(
        notebook, timeout=1200, kernel_name="python3", allow_errors=True,
        resources={"metadata": {"path": str(STAGE)}},
    ).execute()
    failures = [
        f"cell {index}: {output.get('ename')}: {output.get('evalue')}"
        for index, cell in enumerate(notebook.cells)
        if cell.cell_type == "code"
        for output in cell.get("outputs", [])
        if output.get("output_type") == "error"
    ]
    result = json.loads(nbformat.writes(notebook))
    result["metadata"]["capstone_executed"] = True
    result["metadata"]["capstone_kernel"] = True
    return result, failures


def build(
    cells: list[tuple[str, str]], *, executed: bool, kernel: bool = False
) -> tuple[dict[str, Any], list[str]]:
    """The whole document, from the parsed cell file: compile-check, then run or decline to."""
    problems = compile_cells(cells)
    if problems:
        raise BuilderError(
            "these code cells do not compile, so no notebook is written:\n  "
            + "\n  ".join(problems)
            + "\nthe cells call into capstone, so this is usually a helper that was renamed -- "
            "fix the cell file, do not delete the section"
        )
    marked = with_section_markers(cells)
    if executed and kernel:
        plain = [
            markdown(text) if kind == "markdown" else code_cell(text, executed=False, index=0, outputs=[])
            for kind, text in marked
            if kind != "note"
        ]
        return run_in_kernel(render(plain, executed=False))
    if executed:
        rendered, _, failures = execute(marked)
        return render(rendered, executed=True), failures
    rendered = [
        markdown(text) if kind == "markdown" else code_cell(text, executed=False, index=0, outputs=[])
        for kind, text in marked
        if kind != "note"
    ]
    notes = [text for kind, text in marked if kind == "note"]
    if notes:
        # the warning is a note cell in the Markdown file, so an executed build cannot inherit it
        # by accident -- and an un-executed one has no way to leave it out
        rendered.insert(0, markdown(f"{notes[0]}\n\n{NOT_RUN_MARK}"))
    else:
        rendered.insert(
            0,
            markdown(
                "> This notebook has **not been executed**. Its cells were generated from\n"
                f"> ``{CELLS_FILE.relative_to(STAGE).as_posix()}`` and nothing was run, so there are no\n"
                "> outputs and every ``execution_count`` is null. Checklist 9.1 wants the executed\n"
                "> artifact, not the plan: run\n"
                ">\n"
                ">     CAPSTONE_DATABASE_URI=... python3 scripts/build_exploration_notebook.py --execute\n"
                f"\n{NOT_RUN_MARK}"
            ),
        )
    return render(rendered, executed=False), []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        type=Path,
        # the stage root, not a ``notebooks/`` directory: the layout guard names the two notebook
        # deliverables exactly (``stage-4/01_exploration.ipynb``) and refuses a wider set on
        # purpose, so a subdirectory path would need that guard loosened -- the one hole this file
        # exists not to open
        default=STAGE / "01_exploration.ipynb",
    )
    parser.add_argument("--cells", type=Path, default=CELLS_FILE)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="run the cells and write their real outputs (this is what makes the file a deliverable)",
    )
    parser.add_argument(
        "--kernel",
        action="store_true",
        help="with --execute: run in a fresh Jupyter kernel (Restart & Run All), so figures render",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing notebook even if this script did not write it",
    )
    args = parser.parse_args(argv)

    if args.out.exists() and not args.force:
        try:
            existing = json.loads(args.out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        if existing.get("metadata", {}).get(MARKER_KEY) != BUILDER:
            print(
                f"error: {args.out} exists and was not written by {BUILDER} (its metadata has no "
                f"'{MARKER_KEY}'). A builder that overwrites a hand-written notebook is the failure the "
                "repository's warning is about. Move it aside, or pass --force if it really is yours to "
                "replace.",
                file=sys.stderr,
            )
            return int(66)

    try:
        document, failures = build(read_cells(args.cells), executed=args.execute, kernel=args.kernel)
    except BuilderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(EXIT_BAD_CELLS)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(document, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}")
    if not args.execute:
        print(
            "not executed: the notebook says so in its own first cell, and this run exits "
            f"{EXIT_NOT_RUN} so a script around it cannot mistake the plan for the measurement"
        )
        return int(EXIT_NOT_RUN)
    if failures:
        print(f"{len(failures)} cell(s) failed; their tracebacks are in the notebook, where the reader sees them:")
        for line in failures:
            print(f"  {line}")
        return int(EXIT_CELL_FAILED)
    return int(EXIT_OK)


if __name__ == "__main__":
    raise SystemExit(main())
