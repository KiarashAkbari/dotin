"""The one command: every step is ``entrypoint(config)``, in the brief's order, timed, exit codes kept.

Offline: the steps themselves are replaced by recorders, so what is tested is the walk -- order,
call convention, stop-at-first-failure, timing manifest, the stage-1 exit code passed on (B20) and
the ingest step running stage 1 from stage 1's folder with an absolute config (B4). The real walk
against PostgreSQL is tests/test_end_to_end.py.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import helpers  # noqa: F401

from capstone import chain, ingest
from capstone.config import load_config
from capstone.errors import CapstoneError, ConfigError
from capstone.timing import ChainTimer

STAGE = Path(__file__).resolve().parents[1]
RUNNER = STAGE / "scripts" / "run_chain.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_chain", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SkipCoherenceTests(unittest.TestCase):
    """``--skip load-database`` means "already loaded", and the verify half belongs to the load.

    The runbook's already-loaded path died on a machine whose corpus was in PostgreSQL but not in
    ``data/raw/olist``: ``ingest`` refused before ``model`` ever ran, and the manifest wrote a failure
    that no step had caused. The pair now travels together -- and the note is printed, because a step
    vanishing from a plan in silence is how a run record starts to lie.
    """

    def setUp(self) -> None:
        self.runner = load_runner()

    def test_the_load_pair_skips_together_and_says_so(self) -> None:
        skipped, note = self.runner.effective_skip(["load-database"])
        self.assertEqual(skipped, ["load-database", "ingest"])
        self.assertIsNotNone(note)
        self.assertIn("ingest", note)
        self.assertIn("CAPSTONE_OLIST_DIR", note, "the note must carry the way out, not just the reason")
        self.assertIn("data/raw/olist", note)

    def test_an_explicit_ingest_skip_is_not_alone_and_not_double(self) -> None:
        skipped, note = self.runner.effective_skip(["load-database", "ingest"])
        self.assertEqual(skipped.count("ingest"), 1)
        self.assertIsNone(note, "nothing implied, nothing to announce")

    def test_no_skip_changes_the_plan_at_all(self) -> None:
        for asked in ([], None, ["external"]):
            with self.subTest(skip=asked):
                skipped, note = self.runner.effective_skip(asked)
                self.assertEqual(skipped, list(asked or []))
                self.assertIsNone(note)



class ChainShapeTests(unittest.TestCase):
    def test_the_order_is_receive_load_audit_features_external_model_list(self) -> None:
        self.assertEqual(
            [step.name for step in chain.CHAIN],
            ["ingest", "load-database", "load-audit", "features", "external", "model",
             "capacity-list"],
        )

    def test_every_step_is_ready_and_none_is_a_placeholder(self) -> None:
        self.assertEqual([row["name"] for row in chain.statuses() if not row["ready"]], [])
        self.assertIsNone(chain.first_blocked())

    def test_no_step_runs_the_nyc_311_fetch_any_more(self) -> None:
        for step in chain.CHAIN:
            self.assertNotIn("data_factory", step.entrypoint, step.name)

    def test_an_entrypoint_that_names_nothing_says_which_step_it_was(self) -> None:
        ghost = chain.Step(name="ghost", phase=9, entrypoint="capstone.nothing:here", checklist=())
        with self.assertRaises(ConfigError) as caught:
            chain.run_step(ghost, object())
        self.assertIn("ghost", str(caught.exception))


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = load_runner()
        self.config = load_config(helpers.DEFAULT_CONFIG)

    def args(self, **extra) -> argparse.Namespace:
        return argparse.Namespace(loaded_config=self.config, skip=extra.get("skip"))

    def test_every_step_is_called_once_with_the_config_alone_in_order(self) -> None:
        calls: list[tuple[str, tuple]] = []

        def record(step, *args):
            calls.append((step.name, args))
            return {"ok": True}

        timer = ChainTimer(budget_minutes=45)
        with mock.patch.object(chain, "run_step", side_effect=record):
            code = self.runner.execute(self.args(), timer)
        self.assertEqual(code, 0)
        self.assertEqual([name for name, _ in calls], [step.name for step in chain.CHAIN])
        self.assertTrue(all(args == (self.config,) for _, args in calls))
        self.assertEqual([layer.name for layer in timer.layers], [s.name for s in chain.CHAIN])

    def test_a_failure_stops_the_walk_and_is_timed_as_failed(self) -> None:
        def refuse(step, config):
            if step.name == "features":
                raise ConfigError("no database")
            return {}

        timer = ChainTimer(budget_minutes=45)
        with mock.patch.object(chain, "run_step", side_effect=refuse):
            with self.assertRaises(ConfigError):
                self.runner.execute(self.args(), timer)
        self.assertEqual(timer.layers[-1].name, "features")
        self.assertNotEqual(timer.layers[-1].status, "ok")
        self.assertEqual(timer.manifest()["total"]["status"], "failed", "B19: a failed layer fails")

    def test_skip_leaves_a_step_out_and_says_so(self) -> None:
        with mock.patch.object(chain, "run_step", return_value={}) as called:
            args = self.args(skip=["load-database"])
            self.runner.execute(args, ChainTimer(budget_minutes=45))
        self.assertNotIn("load-database", [call.args[0].name for call in called.call_args_list])
        self.assertEqual(args.results["load-database"], "skipped")

    def test_main_returns_the_step_s_own_exit_code_and_writes_the_manifest(self) -> None:
        failure = ingest.Stage1Failure("stage 1 said no", exit_code=78)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            chain, "run_step", side_effect=failure
        ):
            manifest = Path(tmp) / "m.json"
            code = self.runner.main(
                ["--config", str(helpers.DEFAULT_CONFIG), "--manifest", str(manifest)]
            )
            written = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(code, 78, "B20: stage 1's exit code is passed on, not flattened to 1")
        self.assertEqual(written["layers"][0]["name"], "ingest")

    def test_dry_run_lists_every_step_ready(self) -> None:
        with mock.patch("builtins.print") as printed:
            code = self.runner.main(["--config", str(helpers.DEFAULT_CONFIG), "--dry-run"])
        self.assertEqual(code, 0)
        plan = json.loads(printed.call_args_list[0].args[0])
        self.assertTrue(all(row["ready"] for row in plan))


class IngestStepTests(unittest.TestCase):
    def test_load_runs_stage_1_from_its_own_folder_and_parses_the_counts(self) -> None:
        config = load_config(helpers.DEFAULT_CONFIG)
        seen = {}

        def fake_run(command, **kwargs):
            seen.update(kwargs, command=command)
            return SimpleNamespace(returncode=0, stdout="table=orders loaded=3\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            from test_capstone_ingest import write_olist

            write_olist(Path(tmp))
            env = {ingest.DATABASE_URI_ENV: "postgresql://u:pw@h/db", ingest.OLIST_DIR_ENV: tmp}
            # clear=True: a real OLIST_DB_PASSWORD in the shell running the suite used to reach this
            # comparison, and assertEqual printed it in the failure report
            with mock.patch.dict("os.environ", env, clear=True):
                out = ingest.load(config, run=fake_run)
        self.assertEqual(out["loaded"], {"orders": 3})
        self.assertTrue(seen["cwd"].endswith("stage-1-kiarash-akbari"))
        self.assertTrue(
            seen["env"].get(ingest.STAGE1_PASSWORD_ENV) == "pw",
            "the child did not get the URI's password (value deliberately not shown)",
        )
        self.assertIn("--reset", seen["command"])

    def test_the_uri_password_wins_over_a_stale_shell_variable(self) -> None:
        """The incident behind this test: a sandbox run had OLIST_DB_PASSWORD exported from an
        earlier session; the child got that one, and the assertion above printed the real password."""
        config = load_config(helpers.DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as tmp:
            from test_capstone_ingest import write_olist

            write_olist(Path(tmp))
            environ = {
                ingest.DATABASE_URI_ENV: "postgresql://u:fresh-secret@h/db",
                ingest.OLIST_DIR_ENV: tmp,
                ingest.STAGE1_PASSWORD_ENV: "stale-secret",
            }
            child = ingest.subprocess_env(ingest.plan(config, environ), environ)
        self.assertTrue(child[ingest.STAGE1_PASSWORD_ENV] == "fresh-secret", "stale variable won")

    def test_a_stage_1_failure_never_carries_the_password(self) -> None:
        config = load_config(helpers.DEFAULT_CONFIG)
        secret = "hunter2-capstone"
        with tempfile.TemporaryDirectory() as tmp:
            from test_capstone_ingest import write_olist

            write_olist(Path(tmp))
            env = {
                ingest.DATABASE_URI_ENV: f"postgresql://u:{secret}@h/db",
                ingest.OLIST_DIR_ENV: tmp,
            }
            echo = (
                f"sqlalchemy: could not connect to postgresql+psycopg://u:{secret}@h/db "
                f"(password {secret} rejected)"
            )
            failing = lambda *a, **k: SimpleNamespace(returncode=69, stdout="", stderr=echo)  # noqa: E731
            with mock.patch.dict("os.environ", env, clear=True), self.assertRaises(CapstoneError) as caught:
                ingest.load(config, run=failing)
        text = str(caught.exception) + repr(caught.exception.context)
        self.assertFalse(secret in text, "the password reached the error message")
        self.assertIn("«redacted»", str(caught.exception))
        self.assertIn("could not connect", str(caught.exception), "the diagnosis itself must survive")


class RedactTests(unittest.TestCase):
    """`errors.redact` is applied to every CapstoneError, so the guarantee is the package's, not one call's."""

    def test_a_uri_in_any_message_loses_its_password(self) -> None:
        from capstone.errors import ConfigError, redact

        text = "bad uri postgresql://kiarash:s3cr3t-pass@localhost:5432/capstone here"
        self.assertEqual(
            redact(text, environ={}),
            "bad uri postgresql://kiarash:«redacted»@localhost:5432/capstone here",
        )
        error = ConfigError(text, context={"uri": text, "nested": ["x " + text]})
        self.assertNotIn("s3cr3t-pass", str(error))
        self.assertNotIn("s3cr3t-pass", repr(error.context))

    def test_a_bare_password_from_the_environment_is_removed_too(self) -> None:
        from capstone.errors import redact

        environ = {
            "CAPSTONE_DATABASE_URI": "postgresql://u:abc-123-xyz@h/db",
            "OLIST_DB_PASSWORD": "other-pass-9",
            "CAPSTONE_SMOKE_DATABASE_URI": "postgresql://u:smoke-pw-7@h/db2",
        }
        out = redact("abc-123-xyz / other-pass-9 / smoke-pw-7 / keep-me", environ=environ)
        self.assertEqual(out, "«redacted» / «redacted» / «redacted» / keep-me")

    def test_text_without_secrets_is_untouched(self) -> None:
        from capstone.errors import redact

        for text in ("", "plain sentence", "postgresql://u@h/db has no password"):
            with self.subTest(text=text):
                self.assertEqual(redact(text, environ={"OLIST_DB_PASSWORD": "pw"}), text)

    def test_a_stage_1_failure_keeps_its_exit_code(self) -> None:
        config = load_config(helpers.DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as tmp:
            from test_capstone_ingest import write_olist

            write_olist(Path(tmp))
            env = {ingest.DATABASE_URI_ENV: "postgresql://u@h/db", ingest.OLIST_DIR_ENV: tmp}
            failing = lambda *a, **k: SimpleNamespace(returncode=65, stdout="", stderr="bad row")  # noqa: E731
            with mock.patch.dict("os.environ", env), self.assertRaises(CapstoneError) as caught:
                ingest.load(config, run=failing)
        self.assertEqual(int(caught.exception.exit_code), 65)
        self.assertIn("bad row", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
