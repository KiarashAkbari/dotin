"""The stage-1 sibling is found from the package's own location, not from the cwd."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from credit import paths  # noqa: E402


class PathsTests(unittest.TestCase):
    def test_stage3_dir_is_this_folder(self):
        self.assertEqual(paths.STAGE3_DIR, ROOT)

    def test_stage1_is_the_named_sibling(self):
        self.assertEqual(paths.STAGE1_DIR, ROOT.parent / "stage-1-kiarash-akbari")
        self.assertTrue((paths.STAGE1_DIR / "config" / "default.toml").is_file())

    def test_the_default_config_does_not_depend_on_the_cwd(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(paths.STAGE1_CONFIG_ENV, None)
            here = Path.cwd()
            try:
                os.chdir(ROOT.parent)
                first = paths.stage1_config()
                os.chdir(ROOT / "tests")
                second = paths.stage1_config()
            finally:
                os.chdir(here)
        self.assertEqual(first, second)
        self.assertTrue(first.is_absolute())
        self.assertTrue(first.is_file())

    def test_the_override_wins(self):
        with mock.patch.dict(os.environ, {paths.STAGE1_CONFIG_ENV: "/elsewhere/x.toml"}):
            self.assertEqual(paths.stage1_config(), Path("/elsewhere/x.toml"))

    def test_no_source_names_the_old_folder(self):
        """`../stage-1/` was the bug; no code, test or lock line may bring it back."""
        offenders = []
        for folder in ("src", "scripts", "tests"):
            for path in (ROOT / folder).rglob("*.py"):
                if path == Path(__file__).resolve():
                    continue
                text = path.read_text(encoding="utf-8")
                if '"stage-1"' in text or "../stage-1/" in text:
                    offenders.append(str(path.relative_to(ROOT)))
        lock = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        if "-e ../stage-1\n" in lock:
            offenders.append("requirements.txt")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
