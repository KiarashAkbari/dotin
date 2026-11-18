"""The runner's call convention: one step, one positional argument -- the loaded config.

``scripts/run_chain.py`` is what turns the six steps into one command, and until this round nothing
tested how it invokes a step, so the convention existed only as an accident. The runner called
``entrypoint(config, [])``, which fits nothing: ``audit.load_from_csv(config)`` and
``capacity.build_list(config, *, out=...)`` take one positional and died on a ``TypeError``, while
``features.build(config, audit)`` takes two and would accept the empty list as "the audit is empty"
-- a wrong answer rather than a crash. The pending steps looked fine only because ``@pending``
raises before its body can see the arguments at all.

So the contract is written down once, in :func:`capstone.chain.run_step`, and these tests hold every
capstone-owned step to it. Offline by construction: a load or a frame that reaches its own refusal --
about the driver or the URI -- is the step doing its job, and a ``TypeError`` from the call itself is
what is guarded.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))

from capstone import chain, features  # noqa: E402
from capstone.config import load_contract  # noqa: E402
from capstone.errors import CapstoneError, ConfigError, NotReadyError  # noqa: E402
from capstone.pending import MARKER  # noqa: E402

RUNNER = STAGE / "scripts" / "run_chain.py"


def load_runner():
    """Import the runner by path: it is a tool, not a module of the package."""
    spec = importlib.util.spec_from_file_location("run_chain", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class NullTimer:
    """``execute`` only needs ``.layer(name)`` to open and close."""

    def __init__(self) -> None:
        self.layers: list[str] = []

    @contextlib.contextmanager
    def layer(self, name: str):
        self.layers.append(name)
        yield


def _explode(*args, **kwargs):
    raise ReachedConnection("the step asked for a connection; this test refuses one")


def capstone_steps() -> list[chain.Step]:
    return [step for step in chain.CHAIN if step.owner == "capstone"]


class ReachedConnection(Exception):
    """Raised where a step would open its database connection.

    These tests must not touch a real PostgreSQL. That matters more on the student's machine than
    here: with ``CAPSTONE_DATABASE_URI`` exported, an unsealed test walking the chain would apply
    the migrations and ``TRUNCATE`` the audit table for real, and a green suite would be the least
    of the consequences. Patching the resolver is also what makes the assertion strong -- reaching
    this point proves the call landed and the config alone was enough.
    """


@contextlib.contextmanager
def no_database():
    def explode(*args, **kwargs):
        raise ReachedConnection("the step asked for a connection; this test refuses one")

    # the only seam both paths share: features.build binds ``connect=_connect`` as a default, and
    # ``_connect`` looks ``connect`` up in the module at call time -- so this catches the load too.
    # The URI is pinned rather than left to the environment: without it the step refuses one call
    # earlier, on the missing variable, and the test would pass without proving the call landed.
    environment = {"CAPSTONE_DATABASE_URI": "postgresql+psycopg://nobody@127.0.0.1:1/nowhere"}
    with mock.patch.dict(os.environ, environment), mock.patch.object(features, "connect", explode):
        yield


class EntrypointContractTests(unittest.TestCase):
    def test_every_step_can_be_called_with_the_config_alone(self):
        """The claim the runner makes about a step, checked against the step itself.

        Each entrypoint is expected to refuse here -- a placeholder with ``NotReadyError``, a live
        step with a ``ConfigError`` about the driver or about ``CAPSTONE_DATABASE_URI`` -- because
        that refusal is the step doing its own work. A ``TypeError`` would mean the arguments the
        runner passes do not fit the function the roadmap names, which is a bug in the plan, not in
        the environment, and would show up on the student's machine as a crash instead.
        """
        config = load_contract(STAGE / "config" / "capstone.example.toml")
        reached = 0
        refused = 0
        for step in capstone_steps():
            with self.subTest(step=step.name):
                try:
                    with no_database():
                        chain.run_step(step, config)
                except TypeError as exc:  # pragma: no cover - the failure under guard
                    self.fail(f"{step.entrypoint} does not fit the runner's call: {exc}")
                except ReachedConnection:
                    reached += 1
                except (NotReadyError, CapstoneError) as exc:
                    self.assertTrue(str(exc).strip(), "a refusal with no message routes nobody")
                    refused += 1
        # load-audit reaches its connection; features does not, because this tree's build is still
        # the pending placeholder. The count is the tree: the commit that lands the real build moves
        # this to 2 and 2, and no earlier commit may claim a step it cannot run.
        self.assertEqual(reached, 1, msg="load-audit should reach its connection")
        self.assertEqual(refused, 3, msg="features, model and capacity-list should refuse before that")

    def test_the_uri_comes_from_the_environment_the_config_never_stores_it(self):
        """Sealed here, unsealed on the student's machine: the refusal must be about the URI.

        A step that refuses with ``ConfigError`` while ``CAPSTONE_DATABASE_URI`` is unset is the
        same call landing correctly one level earlier, so the contract holds in either environment --
        but only the version with the variable present proves the config is what the step reads.
        """
        config = load_contract(STAGE / "config" / "capstone.example.toml")
        for step in capstone_steps():
            if not step.implemented():
                continue
            with self.subTest(step=step.name):
                with mock.patch.dict(os.environ, {}, clear=True):
                    with mock.patch.object(features, "connect", _explode):
                        with self.assertRaises(CapstoneError) as caught:
                            chain.run_step(step, config)
                self.assertIsInstance(caught.exception, ConfigError)
                self.assertIn("CAPSTONE_DATABASE_URI", str(caught.exception))

    def test_a_pending_step_refuses_before_its_signature_matters(self):
        """``@pending`` wraps with ``*args, **kwargs``, so a placeholder cannot hide an arity bug."""
        config = load_contract(STAGE / "config" / "capstone.example.toml")
        checked = 0
        for step in capstone_steps():
            function = chain.entrypoint_of(step)
            if function is None or getattr(function, MARKER, None) is None:
                continue
            checked += 1
            with self.subTest(step=step.name):
                with self.assertRaises(NotReadyError) as caught:
                    chain.run_step(step, config)
                self.assertEqual(caught.exception.context["step"], step.name)
        self.assertEqual(checked, 2, msg="model and capacity-list are the pending capstone steps")

    def test_an_entrypoint_that_names_nothing_says_which_step_it_was(self):
        """A typo in ``CHAIN`` used to look like a step that is merely not ready."""
        step = chain.Step(
            name="ghost", phase=9, entrypoint="capstone.features:not_there", checklist=("0.0",)
        )
        with self.assertRaises(ConfigError) as caught:
            chain.run_step(step, load_contract(STAGE / "config" / "capstone.example.toml"))
        self.assertIn("ghost", str(caught.exception))
        self.assertIn("not_there", str(caught.exception))

    def test_the_ready_steps_are_the_one_the_runner_can_finish(self):
        """load-audit exists; features, model and capacity-list do not, and each says so.

        The sibling tree pins this set to two. Here ``capstone.features:run`` does not exist yet, and
        readiness is derived from the code -- reporting it as ready would be the mistake this chain
        exists to prevent.
        """
        ready = {step.name for step in capstone_steps() if step.implemented()}
        self.assertEqual(ready, {"load-audit"})


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = load_runner()
        self.completed = mock.Mock(returncode=0, stdout="", stderr="")

    def fake_run(self, calls: list[list[str]]):
        def run(command, **kwargs):
            calls.append(list(command))
            return self.completed

        return run

    def test_execute_reaches_the_load_step_instead_of_its_signature(self):
        """The bug this round fixes, in one assertion.

        Stage 1's two steps are made to succeed and the connection is sealed, so what ``execute``
        raises comes from ``load-audit`` itself and touches nothing: in this sandbox a typed refusal
        about the driver or the URI, and on any machine the same refusal one call later. What it
        must never be is a ``TypeError`` about positional arguments -- that is the runner disagreeing
        with the step on every machine, including the one with everything installed, where a
        TypeError is exactly what this test was written after.
        """
        calls: list[list[str]] = []
        config = load_contract(STAGE / "config" / "capstone.example.toml")
        args = argparse.Namespace(loaded_config=config)
        timer = NullTimer()
        with mock.patch.object(self.runner.subprocess, "run", side_effect=self.fake_run(calls)):
            with no_database():
                try:
                    self.runner.execute(args, timer)
                    outcome = None
                except ReachedConnection:
                    # the load got as far as asking for a connection: the call landed, which is the
                    # whole claim under test, and the seal kept the student's database out of it
                    outcome = None
                except CapstoneError as exc:
                    outcome = exc
        if outcome is not None:
            self.assertNotIn("positional argument", str(outcome))
        self.assertEqual(len(calls), 2, msg="both stage-1 commands run before the load")
        self.assertEqual(timer.layers[:2], ["ingest", "load-database"])
        self.assertEqual(timer.layers[:3], ["ingest", "load-database", "load-audit"])

    def test_a_refusal_stops_the_walk_before_the_next_step(self):
        """The chain is a chain: a step after a refusal does not get to run."""
        calls: list[list[str]] = []

        class Refusal(CapstoneError):
            pass

        config = load_contract(STAGE / "config" / "capstone.example.toml")
        args = argparse.Namespace(loaded_config=config)
        with mock.patch.object(self.runner.subprocess, "run", side_effect=self.fake_run(calls)):
            with mock.patch.object(chain, "run_step", side_effect=Refusal("no")) as stepped:
                with self.assertRaises(Refusal):
                    self.runner.execute(args, NullTimer())
        self.assertEqual(stepped.call_count, 1)
        self.assertEqual(len(calls), 2)

    def test_the_timing_covers_every_step_up_to_the_refusal(self):
        """A manifest whose last line is missing is a manifest that hides where it stopped."""

        class Refusal(CapstoneError):
            pass

        def run_step(step, _config):
            if step.name == "model":
                raise Refusal("model is not written yet")

        calls: list[list[str]] = []
        config = load_contract(STAGE / "config" / "capstone.example.toml")
        args = argparse.Namespace(loaded_config=config)
        timer = NullTimer()
        with mock.patch.object(self.runner.subprocess, "run", side_effect=self.fake_run(calls)):
            with mock.patch.object(chain, "run_step", side_effect=run_step):
                with self.assertRaises(Refusal):
                    self.runner.execute(args, timer)
        self.assertEqual(
            timer.layers,
            ["ingest", "load-database", "load-audit", "features", "model"],
            msg="every step up to and including the refusal gets a layer, none after it",
        )


if __name__ == "__main__":
    unittest.main()
