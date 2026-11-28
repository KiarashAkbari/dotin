"""A failing command prints one readable error line plus its context as JSON.

The context is what tells the user where to look: for `validate` on an empty
store it names the raw folder the command searched. This runs `main()` end to end
from a real TOML file, the way the console script does.
"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from config_helpers import write_config_toml

from data_factory.cli import _raw_store, main
from data_factory.config import load_config
from data_factory.exit_codes import ExitCode


class CliContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = write_config_toml(self.root / "config.toml")

    def _main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_validate_on_empty_store_prints_context(self):
        code, out, err = self._main("validate", "--config", str(self.config))

        self.assertEqual(code, int(ExitCode.DATA))
        self.assertEqual(out, "", "nothing is reported as done")
        lines = err.splitlines()
        self.assertEqual(len(lines), 2, err)
        self.assertEqual(lines[0], "error: No raw pages found; run 'fetch' first")
        context = json.loads(lines[1])
        expected = _raw_store(load_config(self.config)).directory
        self.assertEqual(context, {"dir": str(expected)})
        self.assertTrue(
            Path(context["dir"]).is_relative_to(self.root / "raw"),
            "the folder named is the one the config points at",
        )
        self.assertNotIn("Traceback", err)

    def test_validate_on_empty_store_writes_nothing(self):
        self._main("validate", "--config", str(self.config))
        self.assertFalse((self.root / "quarantine").exists() and any((self.root / "quarantine").iterdir()))
        self.assertFalse((self.root / "manifest.json").exists())

    def test_missing_config_file_is_a_config_error_with_its_path(self):
        missing = self.root / "absent.toml"
        code, _, err = self._main("validate", "--config", str(missing))
        self.assertEqual(code, int(ExitCode.CONFIG), err)
        self.assertTrue(err.startswith("error: "), err)
        self.assertIn("absent.toml", err)
        self.assertNotIn("Traceback", err)


if __name__ == "__main__":
    unittest.main()
