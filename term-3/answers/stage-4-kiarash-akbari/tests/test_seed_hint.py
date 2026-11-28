"""A config without a seed must say which file was read and where the recorded seed lives.

On the student's ThinkPad (2026-11-28) an old local config/capstone.toml without
random_seed replaced capstone.default.toml whole (the loader reads one file, it does
not merge), and the model step stopped with no hint about which file had been read.
"""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path

import helpers
from capstone.config import load_contract
from capstone.errors import InputError
from capstone.system import experiment

CONFIG_DIR = helpers.STAGE_ROOT / "config"


class SeedHint(unittest.TestCase):
    def test_default_contract_carries_a_seed(self):
        self.assertIsNotNone(load_contract(CONFIG_DIR / "capstone.default.toml").model.random_seed)

    def test_missing_seed_names_the_file_and_the_default(self):
        config = load_contract(CONFIG_DIR / "capstone.default.toml")
        local = Path("/machine/stage-4/config/capstone.toml")
        config = dataclasses.replace(
            config,
            source=local,
            model=dataclasses.replace(config.model, random_seed=None),
        )
        with self.assertRaises(InputError) as caught:
            experiment(None, config)  # the guard fires before the frame is touched
        message = str(caught.exception)
        self.assertIn(str(local), message)
        self.assertIn("capstone.default.toml", message)
        self.assertIn("not the example", message)

    def test_example_header_points_at_the_default(self):
        head = (CONFIG_DIR / "capstone.example.toml").read_text(encoding="utf-8")[:1200]
        self.assertIn("copying capstone.default.toml, never this file", head)


if __name__ == "__main__":
    unittest.main()
