"""Stage 1 must not import stage 3, and must not ship a command that needs it.

`load-credit` used to live here: it imported `credit.*` (a package stage 1 does not
depend on), used an undefined `CreditClassifiedRecord` on the first rejected row, and
inserted without clearing, so a rerun hit the primary key. Stage 3 owns the credit
ingest (`python -m credit.load`), built on these same primitives, so the duplicate
was removed instead of patched.
"""

import ast
import contextlib
import io
import unittest
from pathlib import Path

from data_factory import cli

PACKAGE_DIR = Path(cli.__file__).resolve().parent


class NoStage3DependencyTests(unittest.TestCase):
    def test_no_module_imports_the_stage3_package(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                for name in names:
                    with self.subTest(module=path.name, imports=name):
                        self.assertNotEqual(name.split(".")[0], "credit")

    def test_load_credit_is_not_a_command(self):
        self.assertNotIn("load-credit", cli.COMMANDS)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["load-credit"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
