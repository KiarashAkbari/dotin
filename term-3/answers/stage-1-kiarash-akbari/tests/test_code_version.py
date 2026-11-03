import unittest
from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace
from unittest import mock

from data_factory import manifest


class CodeVersionTests(unittest.TestCase):
    def test_uses_installed_metadata_first(self):
        with mock.patch.object(manifest.metadata, "version", return_value="9.9.9"):
            self.assertEqual(manifest.code_version(), "9.9.9")

    def test_falls_back_to_git_revision_without_metadata(self):
        with mock.patch.object(manifest.metadata, "version", side_effect=PackageNotFoundError):
            with mock.patch.object(
                manifest.subprocess, "run", return_value=SimpleNamespace(stdout="abc1234\n")
            ):
                self.assertEqual(manifest.code_version(), "git:abc1234")

    def test_unknown_only_when_nothing_is_available(self):
        with mock.patch.object(manifest.metadata, "version", side_effect=PackageNotFoundError):
            with mock.patch.object(manifest.subprocess, "run", side_effect=FileNotFoundError):
                self.assertEqual(manifest.code_version(), manifest.UNKNOWN_VERSION)
