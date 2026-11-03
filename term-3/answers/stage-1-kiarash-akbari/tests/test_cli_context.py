import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from config_helpers import make_config
from data_factory.cli import main


class CliContextTests(unittest.TestCase):
    def test_validate_on_empty_store_prints_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            config_path = Path(tmp) / "config.toml"
            # main از فایل config می‌خواند؛ باید TOML بنویسیم
            ...
