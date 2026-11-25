"""code_version names the commit a run came from; the package version is the fallback."""

import subprocess
import unittest
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace
from unittest import mock

from data_factory import manifest


def fake_git(revision="abc1234def56", status=""):
    def run(args, **kwargs):
        if args[:2] == ["git", "rev-parse"]:
            return SimpleNamespace(stdout=f"{revision}\n")
        if args[:2] == ["git", "status"]:
            return SimpleNamespace(stdout=status)
        raise AssertionError(f"unexpected command {args}")

    return run


class CodeVersionTests(unittest.TestCase):
    def test_prefers_the_git_commit_over_the_package_version(self):
        with (
            mock.patch.object(manifest.subprocess, "run", side_effect=fake_git()),
            mock.patch.object(manifest.metadata, "version", return_value="0.1.0"),
        ):
            self.assertEqual(manifest.code_version(), "git:abc1234def56")

    def test_marks_uncommitted_changes_to_the_stage(self):
        with mock.patch.object(
            manifest.subprocess, "run", side_effect=fake_git(status=" M src/data_factory/cli.py\n")
        ):
            self.assertEqual(manifest.code_version(), "git:abc1234def56-dirty")

    def test_dirty_check_is_limited_to_the_stage_folder(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            return fake_git()(args, **kwargs)

        with mock.patch.object(manifest.subprocess, "run", side_effect=run):
            manifest.code_version()
        status = [args for args in calls if args[:2] == ["git", "status"]][0]
        self.assertEqual(status[-1], str(manifest.STAGE_DIR))
        self.assertTrue((manifest.STAGE_DIR / "src" / "data_factory").is_dir())

    def test_falls_back_to_the_installed_version_outside_a_checkout(self):
        not_a_repo = subprocess.CalledProcessError(128, ["git"])
        with (
            mock.patch.object(manifest.subprocess, "run", side_effect=not_a_repo),
            mock.patch.object(manifest.metadata, "version", return_value="9.9.9"),
        ):
            self.assertEqual(manifest.code_version(), "9.9.9")

    def test_unknown_only_when_nothing_is_available(self):
        with (
            mock.patch.object(manifest.subprocess, "run", side_effect=FileNotFoundError),
            mock.patch.object(manifest.metadata, "version", side_effect=PackageNotFoundError),
        ):
            self.assertEqual(manifest.code_version(), manifest.UNKNOWN_VERSION)

    def test_this_checkout_reports_a_commit(self):
        # The suite runs from a git checkout (editable install), so the real call
        # must name a commit, not "0.1.0".
        version = manifest.code_version()
        if manifest._git("rev-parse", "HEAD") is None:
            self.skipTest("not running from a git checkout")
        self.assertRegex(version, r"^git:[0-9a-f]{7,40}(-dirty)?$")


if __name__ == "__main__":
    unittest.main()
