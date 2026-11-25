"""The exploration notebook's builder: it may generate, and may not lie.

Checklist 9.1 asks for ``01_exploration.ipynb`` and its evidence note is the sharp part: the file has
to be *produced*, not typed. ``scripts/build_exploration_notebook.py`` is what produces it, so the
guarantees that make the sentence true are tested here rather than trusted from a docstring:

* an un-executed build says so -- a warning cell, ``execution_count: null``, no outputs, and exit 3,
  so a plan cannot be swallowed as a measurement by whoever calls it;
* an executed build records what actually came back, including a traceback when a cell raises, and
  exits 4 -- a captured failure is a result, a deleted one is a document;
* it refuses to overwrite a notebook it did not write, which is the repository's standing warning
  about ``build_solution_notebook.py`` turned into a check;
* every code cell compiles, and the plan holds the sections 9.1 names (EDA, the time audit,
  hypotheses, failures) -- because the notebook reaches the data through ``capstone.*``, a signature
  that drifts has to fail here, not silently in somebody's review.

What no test here can decide is whether the numbers in an executed notebook are *right*: that needs
the project database, which is on the student's machine. These tests cover the mechanism, and the
mechanism is the promise the checklist makes.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import helpers

STAGE = helpers.STAGE_ROOT
SCRIPT = STAGE / "scripts" / "build_exploration_notebook.py"


def load_builder():
    """Import the script as a module -- it is not on the package path, and it stays that way."""
    spec = importlib.util.spec_from_file_location("build_exploration_notebook", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def run_script(*args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )
    return result.returncode, result.stdout, result.stderr


def notebook(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.builder = load_builder()
        self.cells = self.builder.read_cells(self.builder.CELLS_FILE)

    def test_the_cell_file_is_pairs_of_a_kind_and_text(self) -> None:
        cells = self.cells
        self.assertTrue(cells, "the cell file produced no cells at all")
        kinds = set()
        for kind, text in cells:
            with self.subTest(kind=kind):
                self.assertIn(kind, {"markdown", "code", "note"})
                self.assertTrue(text.strip(), f"a {kind} cell is empty")
            kinds.add(kind)
        self.assertEqual(
            kinds,
            {"markdown", "code", "note"},
            "a kind missing from the file means one of the three rules in the builder is untested",
        )

    def test_every_code_cell_compiles(self) -> None:
        """Checked through the builder's own gate, so the test and the build cannot disagree."""
        self.assertEqual(self.builder.compile_cells(self.cells), [])

    def test_an_unparseable_cell_file_is_reported_before_any_file_is_written(self) -> None:
        """The gate, exercised with a real syntax error.

        A notebook with a broken cell is worse than no notebook: it looks like a measurement. So the
        builder refuses to write, and the refusal lists every cell rather than the first.
        """
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "cells.md"
            broken.write_text(
                "%% md\n# t\n\n%% code\nprint(1\n\n%% code\nx = )\n", encoding="utf-8"
            )
            cells = self.builder.read_cells(broken)
            problems = self.builder.compile_cells(cells)
            self.assertEqual(len(problems), 2, msg=str(problems))
            out = Path(tmp) / "01_exploration.ipynb"
            code, _, err = run_script("--cells", str(broken), "--out", str(out))
            self.assertEqual(code, 2, msg=err)
            self.assertFalse(out.exists(), msg="a notebook was written next to cells that do not compile")
        """A cell that cannot compile is a broken notebook waiting for a reader.

        The cell bodies call ``capstone`` functions, so this is the cheap half of the contract with
        phase 5's module: renaming a helper has to fail in a test, not in somebody's review of an
        artifact they cannot re-run.
        """
        for kind, text in self.cells:
            if kind != "code":
                continue
            with self.subTest(head=text.splitlines()[0][:40]):
                compile(text, "<cell>", "exec")

    def test_the_plan_covers_the_sections_the_checklist_names(self) -> None:
        """9.1 is a list of contents, so the list is checkable.

        The probe is an id in an HTML comment at each section's end, not a Persian phrase: the layout
        guard forbids Persian in this directory's sources, and an English test matching Persian prose
        would be a test that breaks on a rewording and survives a deletion -- backwards. The literal
        tuple is asserted too, because it is the transcription of 9.1's list and the thing a reader
        of this file needs to see to believe the section names mean anything.
        """
        self.assertEqual(
            self.builder.SECTIONS,
            (
                "intent", "context", "label", "missing", "time-audit", "external", "plots",
                "hypotheses", "failures",
            ),
            "the ids are 9.1's list: EDA (with its plots), the time audit, hypotheses tested on the "
            "training window, failures -- plus the three the phase actually has to show (label, "
            "missing values, the external signal)",
        )
        prose = "\n".join(text for kind, text in self.builder.with_section_markers(self.cells) if kind == "markdown")
        for name in self.builder.SECTIONS:
            with self.subTest(section=name):
                self.assertIn(f"<!--section:{name}-->", prose)

    def test_a_cell_file_with_too_few_sections_is_refused(self) -> None:
        """The renumbering guard, seen from the other side.

        ``with_section_markers`` pairs ids to markdown cells by position, so a file that loses a
        section would otherwise hand the last id to a cell two above the one it names -- a silent
        mismatch between the checklist row and the notebook. The builder raises instead.
        """
        with self.assertRaises(self.builder.BuilderError) as caught:
            self.builder.with_section_markers([("code", "print(1)\n")])
        self.assertIn("'%% md'", str(caught.exception))

    def test_a_missing_cell_file_is_a_refusal_and_not_an_empty_notebook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(self.builder.BuilderError) as caught:
                self.builder.read_cells(Path(tmp) / "nope.md")
            self.assertIn("no cell file", str(caught.exception).lower())

    def test_the_code_cells_use_capstone_rather_than_a_parallel_query(self) -> None:
        """The notebook must not be a second implementation of the pipeline.

        A cell with its own ``SELECT`` is how a notebook ends up disagreeing with ``capstone audit``
        about what was verified, which is the one disagreement this stage cannot ship.
        """
        offenders = []
        for kind, text in self.cells:
            if kind != "code":
                continue
            if "SELECT " in text.upper() and "read_frame" not in text:
                offenders.append(text.splitlines()[0][:48])
            # pandas itself is fine (the plots and tests work on the frame capstone returns); what is
            # not fine is reading a file or a table behind the pipeline's back
            if "read_csv" in text or "read_sql" in text or "read_parquet" in text:
                offenders.append(f"{text.splitlines()[0][:40]}: reads files the pipeline owns")
        self.assertEqual(offenders, [])


class UnexecutedBuildTests(unittest.TestCase):
    builder = None  # type: ignore[assignment]

    def setUp(self) -> None:
        self.builder = load_builder()

    def test_the_file_says_it_was_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "01_exploration.ipynb"
            code, stdout, stderr = run_script("--out", str(out))
            self.assertEqual(code, 3, msg=f"stdout={stdout}\nstderr={stderr}")
            self.assertIn("not executed", stdout)
            doc = notebook(out)
            first = "".join(doc["cells"][0]["source"])
            self.assertIn(
                self.builder.NOT_RUN_MARK,
                first,
                msg="the notebook's own first cell must carry the marker a doctor can grep",
            )
            # the marker is what lets the next build overwrite this one without --force, so it has to
            # be in the file the script wrote and not only in the object it assembled
            self.assertEqual(doc["metadata"]["capstone_builder"], self.builder.BUILDER)
            self.assertFalse(doc["metadata"]["capstone_executed"])
            for cell in doc["cells"]:
                if cell["cell_type"] == "code":
                    self.assertIsNone(cell["execution_count"])
                    self.assertEqual(cell["outputs"], [])

    def test_a_notebook_the_builder_did_not_write_is_never_overwritten(self) -> None:
        """The repository's warning about ``build_solution_notebook.py``, turned into an exit code.

        The hand-written notebook is the thing a builder destroys and nobody notices, so the check is
        on the file's metadata rather than on a flag somebody has to remember.
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "01_exploration.ipynb"
            foreign = json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
            out.write_text(foreign, encoding="utf-8")
            code, _, err = run_script("--out", str(out))
            self.assertEqual(code, 66, msg=err)
            self.assertIn("was not written by", err)
            self.assertIn("--force", err, msg="the refusal has to name the way out")
            # byte-for-byte, not "still exists": a refusal that rewrote the file with an empty
            # notebook would pass an existence check and destroy the thing it was guarding
            self.assertEqual(out.read_text(encoding="utf-8"), foreign, msg="the foreign file was touched")

            # the override is allowed, but it has to be said out loud: a silent --force is how a
            # builder ends up replacing the wrong file on somebody else's machine
            code, stdout, err = run_script("--out", str(out), "--force")
            self.assertEqual(code, 3, msg=err)
            self.assertIn(str(out), stdout)
            self.assertEqual(notebook(out)["metadata"]["capstone_builder"], "scripts/build_exploration_notebook.py")


class ExecutedBuildTests(unittest.TestCase):
    """The builder's execution path, driven with a plan nobody could confuse with the real one.

    The real cells need the project database, so a test that ran them would be a test of the
    sandbox's missing Postgres. Instead ``execute`` is handed tiny cells, including one that raises:
    what is under test is whether the builder reports faithfully, and that is decidable offline.
    """

    def setUp(self) -> None:
        self.builder = load_builder()

    def test_outputs_are_the_text_the_cells_printed(self) -> None:
        cells = [("code", "print('rows: 3')\n"), ("code", "print(1 + 1)\n")]
        rendered, index, failures = self.builder.execute(cells)
        self.assertEqual(index, 2)
        self.assertEqual(failures, [])
        printed = [
            "".join(output["text"])
            for cell in rendered
            for output in cell["outputs"]
            if output.get("output_type") == "stream"
        ]
        self.assertEqual(printed, ["rows: 3\n", "2\n"])
        self.assertEqual([cell["execution_count"] for cell in rendered], [1, 2])

    def test_cells_share_one_namespace(self) -> None:
        """A notebook whose cells cannot see each other is a pile of scripts, not a notebook."""
        rendered, _, failures = self.builder.execute(
            [("code", "count = 4\n"), ("code", "print(count * 2)\n")]
        )
        self.assertEqual(failures, [])
        self.assertIn("8", "".join(rendered[1]["outputs"][0]["text"]))

    def test_a_failing_cell_keeps_its_traceback_and_is_reported(self) -> None:
        rendered, _, failures = self.builder.execute(
            [
                ("code", "print('before')\n"),
                ("code", "raise ValueError('no frame in this sandbox')\n"),
            ]
        )
        self.assertEqual(len(failures), 1, msg=str(failures))
        self.assertIn("no frame in this sandbox", failures[0])
        outputs = rendered[1]["outputs"]
        self.assertEqual(outputs[-1]["output_type"], "error")
        self.assertEqual(outputs[-1]["ename"], "ValueError")
        self.assertTrue(any(line.startswith("Traceback") for line in outputs[-1]["traceback"]))
        # the stdout the failing cell managed to print is kept, exactly as a kernel keeps it
        self.assertEqual("".join(rendered[0]["outputs"][0]["text"]), "before\n")

    def test_the_executed_notebook_is_valid_json_with_the_builder_marker(self) -> None:
        rendered, _, _ = self.builder.execute([("markdown", "# title"), ("code", "print(1)\n")])
        doc = self.builder.render(rendered, executed=True)
        self.assertEqual(doc["metadata"][self.builder.MARKER_KEY], self.builder.BUILDER)
        self.assertTrue(doc["metadata"]["capstone_executed"])
        self.assertEqual(doc["nbformat"], 4)
        self.assertEqual([cell["cell_type"] for cell in doc["cells"]], ["markdown", "code"])
        # every cell id is unique: nbformat 4.5 requires ids, and a duplicate makes jupyter refuse
        # the file, which is the loudest possible way for a deliverable to be missing
        ids = [cell["id"] for cell in doc["cells"]]
        self.assertEqual(len(ids), len(set(ids)))
        json.dumps(doc)

    def test_a_note_cell_survives_only_into_an_unexecuted_notebook(self) -> None:
        """The "this was not run" warning cannot be deleted, and cannot lie.

        It lives in the cell file as ``%% note``, which is a kind the executor ignores: the day the
        notebook really runs, the warning disappears because the fact it states stopped being true --
        not because somebody removed a line. The marker inside it is what a test greps, so the
        sentence can be reworded in Persian without breaking the check.
        """
        cells = [
            ("note", "NOT RUN"),
            # seven markdown cells, because the section ids are the builder's contract with 9.1 and
            # a fixture that dodges that contract would test a shape the builder never produces
            *[(f"markdown", f"## section {n}") for n in range(len(self.builder.SECTIONS))],
            ("code", "print(1)\n"),
        ]
        doc, failures = self.builder.build(cells, executed=False)
        self.assertEqual(failures, [])
        self.assertEqual(doc["cells"][0]["cell_type"], "markdown")
        self.assertIn(self.builder.NOT_RUN_MARK, "".join(doc["cells"][0]["source"]))
        executed, _, _ = self.builder.execute(self.builder.with_section_markers(cells))
        self.assertNotIn("NOT RUN", json.dumps(executed))
        self.assertEqual(
            [cell["cell_type"] for cell in doc["cells"]][:2],
            ["markdown", "markdown"],
            "the note is a markdown cell in the document; a 'note' cell type is not nbformat",
        )


class ScriptSurfaceTests(unittest.TestCase):
    """The script is a deliverable too: English prose in code, Persian only in the notebook."""

    def test_the_builder_is_a_module_with_a_docstring(self) -> None:
        builder = load_builder()
        self.assertTrue((builder.__doc__ or "").strip())
        self.assertRegex(builder.__doc__.strip().splitlines()[0], r"[A-Za-z]")

    def test_the_exit_codes_are_the_ones_the_docstring_promises(self) -> None:
        builder = load_builder()
        self.assertEqual(builder.EXIT_NOT_RUN, 3)
        self.assertEqual(builder.EXIT_CELL_FAILED, 4)
        self.assertEqual(builder.EXIT_OK, 0)

    def test_the_script_default_lands_where_the_layout_guard_looks(self) -> None:
        """``stage-4/01_exploration.ipynb``, and not a ``notebooks/`` directory.

        The layout guard names the two notebook deliverables exactly and refuses a wider set; a
        subdirectory would need that guard loosened, which is the hole this whole arrangement
        exists to keep shut. The default is checked at source level rather than by running
        ``--help``, so a reworded usage line cannot hide a moved file.
        """
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('STAGE / "01_exploration.ipynb"', text)
        self.assertNotIn('"notebooks"', text)
        self.assertNotIn('Path("notebooks")', text)


if __name__ == "__main__":
    unittest.main()
