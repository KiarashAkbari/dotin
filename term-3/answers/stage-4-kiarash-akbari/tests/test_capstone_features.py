"""The feature layer's driver contract: which module a URI means, and what a refusal says.

``features.build`` is still a placeholder in this tree, so there is nothing here about sequencing
or durations -- ROADMAP phase 4 writes those tests with the body. What is tested here is the half
that could not wait: the live SQL guards and the feature step both opened a connection by
``import psycopg2``, while stages 1, 2 and 3 of this repository ship ``psycopg==3.3.6`` and
``../stage-1-kiarash-akbari/config/default.toml`` carries a ``postgresql+psycopg://`` URI. On the
environment the project actually installs, that meant the database half of stage 4 skipped itself
and blamed a driver nobody uses, next to a green 145-test offline run.

The stub below is why these tests run without PostgreSQL: they assert which module gets imported
and what the URI looks like by the time it does, and no assertion here depends on a server.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))

from capstone import features
from capstone.errors import ConfigError



class DriverTests(unittest.TestCase):
    """Which module a URI means, and what the URI must look like once that is decided.

    The layer used to ``import psycopg2`` outright while the project's lock ships ``psycopg==3.3.6``
    (stages 2 and 3 pin it, ``stage-1/config/default.toml`` asks for ``postgresql+psycopg://``). On
    the environment every other stage installs, that meant the feature step and the live guards both
    died blaming a driver nobody in the repository uses -- so the mapping is the thing under test,
    not an implementation detail of a function that cannot be reached offline.
    """

    def test_the_dialect_names_the_module_and_is_then_dropped(self):
        self.assertEqual(
            features.driver_for("postgresql+psycopg://kiarash@localhost:5432/olist"),
            ("psycopg", "postgresql://kiarash@localhost:5432/olist"),
        )
        self.assertEqual(
            features.driver_for("postgresql+psycopg2://u@h/db"),
            ("psycopg2", "postgresql://u@h/db"),
        )

    def test_a_bare_postgresql_uri_follows_the_lock(self):
        """No dialect is not "no driver": it resolves to the one this project ships."""
        self.assertEqual(
            features.driver_for("postgresql://u@h:5433/olist"),
            ("psycopg", "postgresql://u@h:5433/olist"),
        )

    def test_a_dialect_left_on_the_uri_is_the_bug_this_strips(self):
        # psycopg and psycopg2 both reject "postgresql+psycopg://..." as an unknown scheme, so a
        # caller that forwards the config value verbatim gets an error about URIs, not about drivers
        module, dsn = features.driver_for("postgresql+psycopg://u@h/db")
        self.assertFalse(dsn.startswith("postgresql+"))
        self.assertEqual(module, "psycopg")

    def test_an_unshipped_driver_is_refused_by_name(self):
        with self.assertRaises(ConfigError) as caught:
            features.driver_for("postgresql+asyncpg://u@h/db")
        self.assertIn("asyncpg", str(caught.exception))
        self.assertIn("does not ship", str(caught.exception))

    def test_the_wrong_database_kind_says_nothing_was_guessed(self):
        with self.assertRaises(ConfigError) as caught:
            features.driver_for("sqlite:///olist.db")
        self.assertIn("nothing was guessed", str(caught.exception))

    def test_a_value_that_is_not_a_uri_at_all(self):
        with self.assertRaises(ConfigError) as caught:
            features.driver_for("localhost:5432/olist")
        self.assertIn("not a connection uri", str(caught.exception))

    def test_a_missing_driver_names_the_lock_and_the_way_out(self):
        """The refusal is the deliverable here: the code cannot install anything for the user."""
        from unittest import mock

        with mock.patch.object(features.importlib, "import_module") as fake:
            fake.side_effect = ModuleNotFoundError("No module named 'psycopg'")
            with self.assertRaises(ConfigError) as caught:
                features.connect("postgresql+psycopg://u@h/db", purpose="run the live SQL guards")
        message = str(caught.exception)
        self.assertIn("psycopg", message)
        self.assertIn("psycopg==3.3.6", message)
        self.assertIn("postgresql+psycopg2://", message, msg="the way out belongs in the error")
        self.assertIn("run the live SQL guards", message)
        fake.assert_called_once_with("psycopg")

    def test_autocommit_is_only_what_the_caller_asked_for(self):
        """Production paths commit explicitly; only the live tests may autocommit DDL."""
        from unittest import mock

        class Probe:
            autocommit = False

        probe = Probe()
        module = mock.Mock()
        module.connect.return_value = probe
        with mock.patch.object(features.importlib, "import_module", return_value=module):
            features.connect("postgresql://u@h/db")
        self.assertFalse(probe.autocommit)
        with mock.patch.object(features.importlib, "import_module", return_value=module):
            features.connect("postgresql://u@h/db", autocommit=True)
        self.assertTrue(probe.autocommit)


if __name__ == "__main__":
    unittest.main()
