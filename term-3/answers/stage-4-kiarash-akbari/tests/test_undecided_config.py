"""A config that cannot be honoured says so at the key that is missing.

Measured on a live run (2026-11-25): `run_chain.py` read a `config/capstone.toml` whose
`[model] families` was empty, and the model step died six frames deeper with
`ValueError: max() iterable argument is empty` -- no file, no key, no way out, and only after the
feature frame had been read from Postgres. The same config then made the ablation witness print an
empty table under the sentence "an interval that contains 0 means ...", which is how a run that
measured nothing starts life as a finding.

Two rules come out of that, and both are bookkeeping, so both are testable without a database:

* the roster is checked where it is read, and the error names the section, the shape, and the count;
* a seed that was never written down is reported as missing, never as `0` -- which is what
  `int(config.model.random_seed or 0)` did in three scripts.
"""

from __future__ import annotations

import dataclasses
import re
import unittest
from unittest import mock

import helpers

try:  # the model layer needs the pinned stack; the suite says so instead of erroring
    from capstone import system
    from capstone.config import load_config
    from capstone.errors import InputError

    system = system  # noqa: PLW0127 -- imported for the skipUnless below
except ModuleNotFoundError:  # pragma: no cover
    system = None

STAGE = helpers.STAGE_ROOT


def decided_config():
    from capstone.config import load_config

    return load_config(helpers.DEFAULT_CONFIG)


class RosterGuardTest(unittest.TestCase):
    def test_the_tracked_contract_has_a_roster_of_two_or_more(self) -> None:
        self.assertGreaterEqual(len(system.roster_of(decided_config())), 2)

    def test_an_empty_roster_is_refused_where_it_is_read(self) -> None:
        config = dataclasses.replace(
            decided_config(),
            model=dataclasses.replace(decided_config().model, families=()),
        )
        with self.assertRaises(InputError) as caught:
            system.roster_of(config)
        text = str(caught.exception)
        self.assertIn("[model] families", text, "the error has to name the key")
        self.assertIn("got 0", text, "and how far short it is")

    def test_one_family_is_also_refused(self) -> None:
        """A roster of one cannot select anything: `max()` over it is a choice with no alternative."""
        config = dataclasses.replace(
            decided_config(),
            model=dataclasses.replace(decided_config().model, families=("hgb.d3",)),
        )
        with self.assertRaises(InputError) as caught:
            system.roster_of(config)
        self.assertIn("at least two", str(caught.exception))
        self.assertIn("got 1", str(caught.exception))


@unittest.skipUnless(system is not None, "the model layer needs numpy/pandas/scikit-learn")
class ExperimentGuardTest(unittest.TestCase):
    """The guard is *inside* `experiment`, and it fires before any frame is read.

    `roster_of` and `seed_label` being correct is not enough: with the guard removed from the call
    site the suite stayed green when this was first written, because every test here calls the helper
    directly. So the test drives `experiment()` itself, with `read_frame` mocked to fail loudly --
    a bad config must cost no query.
    """

    def system(self):
        from capstone import system

        return system

    def frame_read(self):
        raise AssertionError("a config that cannot be honoured must not read the frame")

    def test_empty_roster_is_refused_at_the_entry(self) -> None:
        from capstone.errors import InputError

        system = self.system()
        config = dataclasses.replace(
            decided_config(), model=dataclasses.replace(decided_config().model, families=())
        )
        with mock.patch.object(system, "read_frame", side_effect=self.frame_read):
            with self.assertRaises(InputError) as caught:
                system.experiment(object(), config)
        self.assertIn("[model] families", str(caught.exception))

    def test_a_missing_seed_is_refused_at_the_entry(self) -> None:
        from capstone.errors import InputError

        system = self.system()
        config = dataclasses.replace(
            decided_config(), model=dataclasses.replace(decided_config().model, random_seed=None)
        )
        with mock.patch.object(system, "read_frame", side_effect=self.frame_read):
            with self.assertRaises(InputError) as caught:
                system.experiment(object(), config)
        self.assertIn("random_seed", str(caught.exception))
        self.assertIn("seed not set in config", str(caught.exception), "and it says so in words")

    def test_the_guard_precedes_the_import_and_the_read(self) -> None:
        source = (STAGE / "src" / "capstone" / "system.py").read_text(encoding="utf-8")
        body = source[source.index("def experiment(") :]
        self.assertLess(body.index("roster_of(config)"), body.index("import numpy"))
        self.assertLess(body.index("roster_of(config)"), body.index("time_split(frame"))
        self.assertNotIn(
            "list(config.model.families)",
            body,
            "the guard has to be the only way `experiment` learns its roster: read it inline and the "
            "empty case walks back into `max()`",
        )


class SeedLabelTest(unittest.TestCase):
    def test_a_written_seed_is_printed_as_itself(self) -> None:
        self.assertEqual(system.seed_label(decided_config()), "seed 20181")

    def test_a_missing_seed_says_missing_rather_than_zero(self) -> None:
        config = dataclasses.replace(
            decided_config(), model=dataclasses.replace(decided_config().model, random_seed=None)
        )
        self.assertEqual(system.seed_label(config), "seed not set in config")

    def test_no_script_invents_a_seed_anymore(self) -> None:
        """`int(config.model.random_seed or 0)` printed `seed 0` for an undecided key, in triplicate."""
        offenders = []
        for rel in ("src/capstone/system.py", "docs/evidence/model-ablation-run.py",
                    "scripts/cheating_model_gap.py", "scripts/sealed_cut_rehearsal.py"):
            text = (STAGE / rel).read_text(encoding="utf-8")
            if re.search(r"random_seed or 0", text):
                offenders.append(rel)
        self.assertEqual(offenders, [], f"these still substitute a seed: {offenders}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
