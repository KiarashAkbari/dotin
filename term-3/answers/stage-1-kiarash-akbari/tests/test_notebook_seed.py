"""Delivery Guide rule 5: the seed is set once, at the top, and passed to the random step.

The notebook's only random step is the Spearman sample in section 7. SEED used to
be copied from the package in the middle of the notebook (`SEED = a.SEED`) and was
never passed to anything, so changing it at the top would have changed nothing.
"""

import ast
import json
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from data_factory import analysis

STAGE_ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = STAGE_ROOT / "solution.ipynb"
BUILDER = STAGE_ROOT / "scripts" / "build_solution_notebook.py"


def code_cells():
    cells = json.loads(NOTEBOOK.read_text(encoding="utf-8"))["cells"]
    return ["".join(cell["source"]) for cell in cells if cell["cell_type"] == "code"]


def seed_assignments(source):
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SEED" for target in node.targets)
    ]


def spearman_calls(source):
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "spearman"
    ]


class NotebookSeedTests(unittest.TestCase):
    def test_seed_is_a_literal_in_the_first_code_cell_and_nowhere_else(self):
        cells = code_cells()
        first = seed_assignments(cells[0])
        self.assertEqual(len(first), 1, "SEED is assigned once in the first code cell")
        self.assertIsInstance(first[0].value, ast.Constant)
        for index, source in enumerate(cells[1:], start=1):
            self.assertEqual(seed_assignments(source), [], f"code cell {index} reassigns SEED")

    def test_every_spearman_call_receives_the_seed(self):
        calls = [call for source in code_cells() for call in spearman_calls(source)]
        self.assertTrue(calls, "the notebook runs the Spearman section")
        for call in calls:
            seeds = [kw.value for kw in call.keywords if kw.arg == "seed"]
            self.assertEqual(len(seeds), 1, ast.unparse(call))
            self.assertIsInstance(seeds[0], ast.Name)
            self.assertEqual(seeds[0].id, "SEED")

    def test_the_builder_writes_the_same_seed_code(self):
        builder = BUILDER.read_text(encoding="utf-8")
        self.assertNotIn("SEED = a.SEED", builder)
        self.assertIn("a.spearman(df, seed=SEED)", builder)
        self.assertIn("SEED = 42", builder)

    def test_the_seed_decides_the_spearman_sample(self):
        rng = np.random.default_rng(0)
        size = analysis.CORRELATION_SAMPLE + 500
        frame = pd.DataFrame(
            {column: rng.normal(size=size) for column in analysis.CORRELATION_COLUMNS}
        )
        same = analysis.spearman(frame, seed=7)
        pd.testing.assert_frame_equal(same, analysis.spearman(frame, seed=7))
        self.assertFalse(same.equals(analysis.spearman(frame, seed=8)))


if __name__ == "__main__":
    unittest.main()
