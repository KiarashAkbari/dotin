"""Config loading: shape is validated, and an undecided decision is a hard stop."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import helpers
from capstone.config import (
    NEGATIVE_MAX_SCORE,
    documented_decisions,
    load_config,
    load_contract,
)
from capstone.errors import ConfigError


def decisions_text(boundary: str = "2018-06-30") -> str:
    """The ``decisions.md`` a config naming a source and three policies implies."""
    return (
        "# decisions\n\n"
        "keep_and_label_zero, drop_before_training, worst_score_wins\n"
        f"boundary {boundary}; weather_daily https://example.invalid/weather\n"
    )


def decided_text(boundary: str = "2018-06-30") -> str:
    """A copy of the contract with the student's four decisions filled in."""
    return f"""
[time_guard]
decision_column = "order_approved_at"
audit_file = "column_audit.csv"

[features]
schema = "capstone"
sql_migrations_dir = "../sql"
curated_dir = "../data/curated"
feature_query_ms = 500

[model]
negative_max_score = {NEGATIVE_MAX_SCORE}
no_review = "keep_and_label_zero"
no_approved_at = "drop_before_training"
multi_review = "worst_score_wins"

[capacity]
share = 0.10
report_shares = [0.05, 0.10, 0.20]

[split]
boundary = "{boundary}"
last_month_as_secondary = true

[budget]
chain_minutes = 45

[external]
name = "weather_daily"
url = "https://example.invalid/weather"
token_env = "CAPSTONE_WEATHER_TOKEN"
join_on = "date"
"""


class TrackedContractTest(unittest.TestCase):
    """The example file is a contract, so its ceilings have to stay as the brief states."""

    def test_loads_shape_without_loading_decisions(self) -> None:
        config = load_contract(helpers.EXAMPLE_CONFIG)
        self.assertEqual(config.features.feature_query_ms, 500)
        self.assertEqual(config.budget.chain_minutes, 45)
        self.assertEqual(config.capacity.share, 0.10)
        self.assertEqual(config.capacity.report_shares, (0.05, 0.10, 0.20))
        self.assertEqual(config.time_guard.decision_column, "order_approved_at")

    def test_relative_paths_resolve_inside_stage_4(self) -> None:
        config = load_contract(helpers.EXAMPLE_CONFIG)
        stage = helpers.STAGE_ROOT.resolve()
        self.assertTrue(config.features.sql_migrations_dir.is_relative_to(stage))
        self.assertTrue(config.features.curated_dir.is_relative_to(stage))

    def test_the_example_is_still_undecided_by_design(self) -> None:
        """If this fails, someone wrote a decision into the tracked file instead of config."""
        config = load_contract(helpers.EXAMPLE_CONFIG)
        self.assertEqual(
            set(config.undecided_keys()),
            {
                "model.no_review",
                "model.no_approved_at",
                "model.multi_review",
                "split.boundary",
                "external.name",
            },
        )
        with self.assertRaises(ConfigError) as caught:
            load_config(helpers.EXAMPLE_CONFIG)
        self.assertIn("student decisions", str(caught.exception))


class TrackedDecidedConfigTest(unittest.TestCase):
    """config/capstone.default.toml is what a fresh clone runs, so it must load as decided."""

    def test_the_default_config_is_fully_decided(self) -> None:
        config = load_config(helpers.DEFAULT_CONFIG)
        self.assertEqual(config.undecided_keys(), ())
        self.assertEqual(config.model.no_review, "separate_population")
        self.assertEqual(config.model.no_approved_at, "drop_before_training")
        self.assertEqual(config.model.multi_review, "worst_score_wins")
        self.assertEqual(config.split.boundary, date(2018, 5, 31))

    def test_the_default_config_names_two_model_families_and_a_validation_window(self) -> None:
        config = load_config(helpers.DEFAULT_CONFIG)
        self.assertGreaterEqual(len({name.split(".")[0] for name in config.model.families}), 2)
        self.assertGreaterEqual(config.model.validation_months or 0, 1)
        self.assertIsNotNone(config.model.random_seed)

    def test_the_default_config_carries_no_credential(self) -> None:
        text = helpers.DEFAULT_CONFIG.read_text(encoding="utf-8").lower()
        for marker in ("password =", "postgresql://", "@localhost"):
            self.assertNotIn(marker, text)

    def test_every_default_value_is_recorded_in_decisions_md(self) -> None:
        config = load_config(helpers.DEFAULT_CONFIG)
        self.assertEqual(documented_decisions(helpers.STAGE_ROOT / "decisions.md", config), [])

    def test_a_family_list_with_a_malformed_name_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "config"
            root.mkdir()
            (Path(tmp) / "decisions.md").write_text(decisions_text(), encoding="utf-8")
            text = decided_text().replace(
                'multi_review = "worst_score_wins"',
                'multi_review = "worst_score_wins"\nfamilies = ["LogReg"]',
            )
            path = root / "capstone.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


class DecidedConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stage = Path(self._tmp.name)
        self.config_dir = self.stage / "config"
        self.config_dir.mkdir()
        (self.stage / "data" / "curated").mkdir(parents=True)
        self.decisions = self.stage / "decisions.md"
        self.decisions.write_text(decisions_text(), encoding="utf-8")
        self.path = self.config_dir / "capstone.toml"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_a_written_down_decision_loads(self) -> None:
        config = load_config(self.write(decided_text()))
        self.assertEqual(config.split.boundary, date(2018, 6, 30))
        self.assertEqual(config.model.no_approved_at, "drop_before_training")
        self.assertEqual(config.undecided_keys(), ())

    def test_a_single_undecided_key_still_blocks(self) -> None:
        text = decided_text().replace('multi_review = "worst_score_wins"', 'multi_review = "unset"')
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write(text))
        self.assertIn("model.multi_review", str(caught.exception))

    def test_absolute_paths_are_refused(self) -> None:
        text = decided_text().replace('curated_dir = "../data/curated"', 'curated_dir = "/tmp/out"')
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write(text))
        self.assertIn("relative", str(caught.exception))

    def test_a_retuned_label_is_refused(self) -> None:
        text = decided_text().replace(
            f"negative_max_score = {NEGATIVE_MAX_SCORE}", "negative_max_score = 3"
        )
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write(text))
        self.assertIn("1 or 2", str(caught.exception))

    def test_the_operational_share_must_appear_in_the_table(self) -> None:
        text = decided_text().replace(
            "report_shares = [0.05, 0.10, 0.20]", "report_shares = [0.05, 0.20]"
        )
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write(text))
        self.assertIn("report_shares", str(caught.exception))

    def test_a_half_declared_source_is_refused(self) -> None:
        text = decided_text().replace('url = "https://example.invalid/weather"', 'url = ""')
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write(text))
        self.assertIn("half declared", str(caught.exception))

    def test_a_source_without_a_recorded_decision_is_refused(self) -> None:
        """The API choice and its hypothesis are one decision, in one file."""
        self.write(decided_text())
        self.decisions.unlink()
        with self.assertRaises(ConfigError) as caught:
            load_config(self.path)
        self.assertIn("decisions.md", str(caught.exception))

    def test_documented_decisions_wants_the_names_it_was_given(self) -> None:
        self.write(decided_text())
        config = load_contract(self.path)
        empty = self.stage / "nothing_here.md"
        self.assertEqual(
            documented_decisions(empty, config), ["nothing_here.md is missing (checklist 9.4)"]
        )
        self.decisions.write_text(decisions_text().replace("2018-06-30", ""), encoding="utf-8")
        self.assertEqual(documented_decisions(self.decisions, config), ["2018-06-30"])

    def test_a_missing_section_is_reported_as_a_missing_section(self) -> None:
        text = decided_text().replace("[budget]", "[budge]")
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write(text))
        self.assertIn("[budget]", str(caught.exception))


_DEFAULTS = {"name": "weather_daily", "url": "https://example.invalid/weather",
             "token_env": "CAPSTONE_WEATHER_TOKEN", "join_on": "date"}


class ExternalDeclinedTest(unittest.TestCase):
    """``external`` is the one section where "no" is a complete answer.

    12.1 allows the project to report "we used no external signal, and this is what that
    costs" instead of fetching one. Until ``none`` was a legal value the only way to unblock
    the chain was to name a source -- so the config rewarded naming something this machine had
    not reached, which is precisely how an unverified URL turns into a feature.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.stage = Path(self._tmp.name)
        self.config_dir = self.stage / "config"
        self.config_dir.mkdir()
        (self.stage / "data" / "curated").mkdir(parents=True)
        self.path = self.config_dir / "capstone.toml"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, **external: str) -> Path:
        filled = {"name": "", "url": "", "token_env": "", "join_on": ""}
        filled.update(external)
        text = decided_text()
        for key, value in filled.items():
            text = text.replace(f'{key} = "{_DEFAULTS[key]}"', f'{key} = "{value}"')
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def test_declining_the_external_source_is_itself_a_decision(self) -> None:
        config = load_config(self.write(name="none"))
        self.assertEqual(config.external.name, "none")
        self.assertFalse(config.external.enabled)
        self.assertNotIn("external.name", config.undecided_keys())

    def test_an_empty_name_is_not_the_same_answer_as_none(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_config(self.write(name=""))
        self.assertIn("external.name", str(caught.exception))

    def test_none_and_a_configured_source_cannot_both_be_true(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            load_contract(self.write(name="none", url="https://example.invalid/weather"))
        self.assertIn("declining an external source", str(caught.exception))

    def test_declining_still_has_to_be_written_down(self) -> None:
        """The word ``none`` has to appear in decisions.md, like any other policy value.

        Without that, "none" would be a way to *silence* the external question instead of
        answering it: the check is only a presence check, but it is the difference between a
        recorded refusal and an empty field somebody forgot about.
        """
        config = load_config(self.write(name="none"))
        missing = self.stage / "decisions.md"
        self.assertEqual(
            documented_decisions(missing, config), ["decisions.md is missing (checklist 9.4)"]
        )
        missing.write_text(decisions_text() + "\nnone\n", encoding="utf-8")
        self.assertEqual(documented_decisions(missing, config), [])
        missing.write_text(
            (decisions_text() + "\nnone\n").replace("2018-06-30", ""), encoding="utf-8"
        )
        self.assertEqual(documented_decisions(missing, config), ["2018-06-30"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
