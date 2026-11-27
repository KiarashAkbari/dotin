"""The two delivered notebooks, read against the delivery guide's notebook rules (00 section 2).

Offline and read-only: these tests open the committed ``.ipynb`` files and never execute them. They pin
what a grader checks without running anything -- a run from a fresh kernel with contiguous execution
counts, stored outputs, no traceback, no absolute path from the machine that ran it, one seed defined
at the top, narrative Markdown opening every section, and no database password in any output.
"""

from __future__ import annotations

import json
import re
import unittest

import helpers

STAGE = helpers.STAGE_ROOT
DELIVERED = ("01_exploration.ipynb", "02_system.ipynb")
#: a path on the machine that executed the notebook (00 section 2, rule 4)
ABSOLUTE = re.compile(r"(?<![\w.])(?:/(?:home|Users|tmp|workspace|root|mnt|var)/|[A-Za-z]:\\\\Users)")
#: ``scheme://user:password@`` -- a URI that still carries its secret
URI_WITH_PASSWORD = re.compile(r"[a-z][a-z0-9+.-]*://[^:/@\s\"']+:[^@\s\"']+@")


def load(name: str) -> dict:
    return json.loads((STAGE / name).read_text(encoding="utf-8"))


def output_text(output: dict) -> str:
    if "text" in output:
        text = output["text"]
    else:
        text = output.get("data", {}).get("text/plain", "")
    return "".join(text) if isinstance(text, list) else str(text)


class DeliveredNotebookTests(unittest.TestCase):
    def test_both_notebooks_are_delivered(self) -> None:
        for name in DELIVERED:
            with self.subTest(notebook=name):
                self.assertTrue((STAGE / name).is_file(), f"{name} is missing")

    def test_execution_counts_run_from_one_to_n_without_a_gap(self) -> None:
        for name in DELIVERED:
            with self.subTest(notebook=name):
                code = [cell for cell in load(name)["cells"] if cell["cell_type"] == "code"]
                self.assertTrue(code)
                self.assertEqual([cell.get("execution_count") for cell in code], list(range(1, len(code) + 1)))

    def test_outputs_are_stored_and_no_cell_failed(self) -> None:
        for name in DELIVERED:
            with self.subTest(notebook=name):
                code = [cell for cell in load(name)["cells"] if cell["cell_type"] == "code"]
                with_output = [cell for cell in code if cell.get("outputs")]
                self.assertGreaterEqual(len(with_output), len(code) - 1, "outputs were cleared")
                errors = [
                    output.get("ename")
                    for cell in code
                    for output in cell.get("outputs", [])
                    if output.get("output_type") == "error"
                ]
                self.assertEqual(errors, [])

    def test_no_absolute_path_and_no_password_in_any_cell(self) -> None:
        for name in DELIVERED:
            with self.subTest(notebook=name):
                for index, cell in enumerate(load(name)["cells"]):
                    texts = ["".join(cell.get("source", ""))]
                    texts += [output_text(output) for output in cell.get("outputs", [])]
                    for text in texts:
                        self.assertIsNone(ABSOLUTE.search(text), f"cell {index}: absolute path")
                        self.assertIsNone(URI_WITH_PASSWORD.search(text), f"cell {index}: a URI with a password")

    def test_the_seed_is_defined_once_in_the_first_code_cell(self) -> None:
        for name in DELIVERED:
            with self.subTest(notebook=name):
                code = [cell for cell in load(name)["cells"] if cell["cell_type"] == "code"]
                first = "".join(code[0]["source"])
                self.assertRegex(first, r"\bSEED\s*=", "rule 5: one seed, at the top")
                later = "".join("".join(cell["source"]) for cell in code[1:])
                self.assertNotRegex(later, r"\bSEED\s*=", "the seed is set once, not re-set further down")

    def test_every_section_opens_with_markdown(self) -> None:
        for name in DELIVERED:
            with self.subTest(notebook=name):
                cells = load(name)["cells"]
                self.assertEqual(cells[0]["cell_type"], "markdown", "rule 7: the notebook opens with its story")
                headings = [
                    i for i, cell in enumerate(cells)
                    if cell["cell_type"] == "markdown" and re.search(r"^#{1,3} ", "".join(cell["source"]), re.M)
                ]
                self.assertGreaterEqual(len(headings), 5, "too few narrated sections")


if __name__ == "__main__":
    unittest.main()
