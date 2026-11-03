import unittest
from pathlib import Path

from data_factory.config import load_config
from data_factory.contract import CONTRACT, UNIQUE_KEY, ReasonCode

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "default.toml"


class ContractTests(unittest.TestCase):
    def test_column_names_are_unique(self):
        names = [c.name for c in CONTRACT]
        self.assertEqual(len(names), len(set(names)))

    def test_unique_key_is_required(self):
        spec = next(c for c in CONTRACT if c.name == UNIQUE_KEY)
        self.assertFalse(spec.nullable)

    def test_nullable_columns_are_exactly_the_sparse_ones(self):
        nullable = {c.name for c in CONTRACT if c.nullable}
        self.assertEqual(nullable, {"closed_date", "incident_zip", "descriptor", "community_board"})

    def test_matches_configured_columns(self):
        config = load_config(CONFIG_PATH)
        self.assertEqual({c.name for c in CONTRACT}, set(config.source.columns))

    def test_reason_code_values_equal_names(self):
        for code in ReasonCode:
            self.assertEqual(code.value, code.name)