"""The command line a stranger types first: doctor, audit, chain --dry-run, predict.

Phase 6 wants one command that runs the chain. Until then, the useful contract is the
*shape*: which subcommands exist, which exit code each one answers with, and that a
placeholder refuses with the phase and the checklist item rather than a traceback. A
grader who runs the scaffold should learn something true from it, and that is testable
today.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch

import helpers
from capstone import features, model, predict
from capstone.audit import AUDIT_HEADER
from capstone.chain import CHAIN, first_blocked, require_ready, statuses
from capstone.cli import default_config_path, main
from capstone.errors import CapstoneError, NotReadyError
from capstone.exit_codes import ExitCode
from capstone.pending import is_pending
from capstone.predict import check_schema, read_header, score_file


def run(*argv: str) -> tuple[int, str, str]:
    """Run the CLI and hand back (exit code, stdout, stderr) instead of printing."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ChainPlanTest(unittest.TestCase):
    def test_the_plan_is_the_six_steps_the_brief_describes(self) -> None:
        """The order is the point, and so is the step that was missing from it.

        ``load-audit`` sits between stage 1's load and this stage's frame: ``sql/001`` creates
        ``capstone.column_audit``, the guard in ``sql/002`` reads that table and refuses everything
        while it is empty, and nothing else in this stage writes it. A chain that starts from a fresh
        database therefore died at its third command on a setup error dressed up as a leak.
        """
        self.assertEqual(
            [step.name for step in CHAIN],
            ["ingest", "load-database", "load-audit", "features", "model", "capacity-list"],
        )

    def test_the_prediction_path_is_not_part_of_the_training_chain(self) -> None:
        self.assertNotIn("predict", [step.name for step in CHAIN])

    def test_dry_run_reports_what_blocks_and_why(self) -> None:
        code, out, _ = run("chain", "--dry-run")
        self.assertEqual(code, int(ExitCode.OK))
        # phase 4 landed, so the plan now blocks on the model layer and names the phase that owns
        # it; the features line has to say "ready" rather than merely appear
        self.assertIn("[ready  ] features", out)
        self.assertIn("[blocked] model", out)
        self.assertIn("phase 5", out)
        self.assertIn("6.1", out)

    def test_dry_run_json_is_machine_readable(self) -> None:
        code, out, _ = run("chain", "--dry-run", "--json")
        self.assertEqual(code, int(ExitCode.OK))
        rows = json.loads(out)
        self.assertEqual([row["name"] for row in rows], [step.name for step in CHAIN])
        self.assertTrue(all("reason" in row for row in rows))

    def test_readiness_is_derived_from_the_code_not_from_a_status_table(self) -> None:
        rows = {row["name"]: row for row in statuses()}
        # features flipped to ready when phase 4 wrote its body, and nothing in this file or in
        # chain.py was edited to make that happen -- which is the whole point of deriving it
        self.assertTrue(rows["features"]["ready"])
        self.assertEqual(rows["features"]["reason"], "")
        self.assertFalse(rows["model"]["ready"])
        self.assertIn("placeholder", rows["model"]["reason"])
        # Where the chain stops is a fact about the interpreter, not about this code: the two stage-1
        # steps count as ready exactly when ``find_spec`` can see that package, so in a venv that has
        # stage 1 installed the chain stops at ``model`` -- and the hard-coded "ingest" used to fail
        # there, which punished a working setup. So the test asks the same question ``implemented()``
        # asks and checks the answer that follows. Both branches assert: skipping the ready case
        # would let a chain that ignores stage 1 pass unnoticed.
        stage_one = find_spec("data_factory") is not None
        if stage_one:
            self.assertTrue(
                rows["ingest"]["ready"],
                msg="stage 1 is importable here, so ingest cannot be the blocker the chain names",
            )
            self.assertEqual(first_blocked().name, "model")
        else:
            self.assertEqual(first_blocked().name, "ingest")
            self.assertIn("not importable", rows["ingest"]["reason"])

    def test_the_placeholders_refuse_with_their_phase(self) -> None:
        # features.build is absent on purpose: its body landed with phase 4, and
        # LayerStatusTests in test_capstone_features.py pins that it no longer carries the marker
        for function in (model.train, predict._score_rows):
            with self.assertRaises(NotReadyError) as caught:
                function(None, [])  # type: ignore[arg-type]
            self.assertIn("ROADMAP phase", str(caught.exception))
            self.assertTrue(is_pending(function))

    def test_requiring_an_unknown_step_says_what_exists(self) -> None:
        with self.assertRaises(NotReadyError) as caught:
            require_ready("teleport")
        self.assertIn("capacity-list", str(caught.exception))


class SchemaGateTest(unittest.TestCase):
    """The part of the prediction path that can work before the model exists."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.expected = ["order_id", "customer_id", "order_status"]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_matching_header_passes(self) -> None:
        self.assertEqual(check_schema(self.expected, self.expected, origin="in.csv"), None)

    def test_a_missing_column_is_named_with_the_file(self) -> None:
        with self.assertRaises(CapstoneError) as caught:
            check_schema(["order_id", "customer_id"], self.expected, origin="sealed_cut.csv")
        message = str(caught.exception)
        self.assertIn("sealed_cut.csv", message)
        self.assertIn("missing column(s): order_status", message)

    def test_an_extra_column_is_named_too(self) -> None:
        with self.assertRaises(CapstoneError) as caught:
            check_schema(
                [*self.expected, "order_delivered_customer_date"], self.expected, origin="in.csv"
            )
        self.assertIn("unexpected column(s)", str(caught.exception))

    def test_the_header_case_is_normalised_before_comparing(self) -> None:
        path = self.root / "fresh.csv"
        path.write_text("Order_ID,Customer_ID,Order_Status\n", encoding="utf-8")
        self.assertEqual(read_header(path), self.expected)

    def test_an_empty_file_is_refused_by_name(self) -> None:
        path = self.root / "empty.csv"
        path.write_text("", encoding="utf-8")
        with self.assertRaises(CapstoneError) as caught:
            read_header(path)
        self.assertIn("empty.csv is empty", str(caught.exception))

    def test_a_missing_file_is_refused_before_anything_else(self) -> None:
        with self.assertRaises(CapstoneError) as caught:
            read_header(self.root / "nope.csv")
        self.assertIn("no input file", str(caught.exception))

    def test_a_good_file_reaches_the_unwritten_model_and_stops_there(self) -> None:
        """The gate runs, the model does not: two different errors, in that order."""
        columns = helpers.columns_from_stage2_ddl("orders")
        path = self.root / "sealed.csv"
        path.write_text(",".join(columns) + "\n", encoding="utf-8")
        config = helpers.load_example_contract()
        with self.assertRaises(NotReadyError) as caught:
            score_file(path, config)
        self.assertIn("phase 6", str(caught.exception))

    def test_a_bad_file_is_refused_even_though_the_model_is_missing(self) -> None:
        path = self.root / "sealed.csv"
        path.write_text("order_id,basket_size\n", encoding="utf-8")
        config = helpers.load_example_contract()
        with self.assertRaises(CapstoneError) as caught:
            score_file(path, config)
        self.assertNotIsInstance(caught.exception, NotReadyError)
        self.assertIn("basket_size", str(caught.exception))


class DoctorTest(unittest.TestCase):
    """The doctor, against the tracked contract.

    Every run here names ``config/capstone.example.toml`` explicitly. The default resolves to
    ``config/capstone.toml``, which is ignored on purpose and exists the moment a student
    records their decisions -- so an unpinned run would report a different, equally true set
    of gaps depending on whether this particular machine has decided anything yet. The
    decided world gets its own test below instead of being an accident.
    """

    def test_doctor_lists_the_blocking_gaps_by_checklist_item(self) -> None:
        code, out, _ = run("doctor", "--config", str(helpers.EXAMPLE_CONFIG))
        self.assertEqual(code, int(ExitCode.CONFIG))
        self.assertIn("checklist 4.1", out)
        self.assertIn("column audit", out)
        names = {
            line.split("]", 1)[1].strip().split(":", 1)[0]
            for line in out.splitlines()
            if line.startswith(("[FAIL]", "[warn]"))
        }
        # A deliverable is reported while it is missing and not after it is written:
        # the doctor reads the tree, so it cannot drift into claiming a gap that closed.
        for deliverable in ("report.md", "decisions.md", "manager_summary.md"):
            with self.subTest(deliverable=deliverable):
                self.assertEqual(
                    deliverable in names, not (helpers.STAGE_ROOT / deliverable).exists()
                )
        self.assertTrue(
            names & {"01_exploration.ipynb", "02_system.ipynb", "requirements.txt"},
            "the doctor must still warn about what the scaffold round did not write",
        )

    def test_doctor_json_is_parseable_and_complete(self) -> None:
        code, out, _ = run("doctor", "--json", "--config", str(helpers.EXAMPLE_CONFIG))
        rows = json.loads(out)
        self.assertEqual(code, int(ExitCode.CONFIG))
        self.assertEqual(
            {row["name"] for row in rows}
            >= {"python", "config", "column audit", "feature SQL", "chain steps"},
            True,
        )
        self.assertTrue(all({"ok", "severity", "detail"} <= set(row) for row in rows))

    def test_blocking_checks_sort_before_the_rest(self) -> None:
        rows = json.loads(run("doctor", "--json", "--config", str(helpers.EXAMPLE_CONFIG))[1])
        severities = [row["severity"] for row in rows if not row["ok"]]
        self.assertEqual(severities[0], "blocking")

    def test_a_fresh_clone_reads_the_tracked_example(self) -> None:
        """A clone has no decided config, so the default is the example -- and nothing else.

        Asserted against a directory shaped like a fresh clone, not against this working tree:
        ``config/capstone.toml`` is ignored precisely because writing it down is the student's
        job, so the tree may or may not hold one and neither state is a test failure.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            shutil.copy2(helpers.EXAMPLE_CONFIG, root / "config" / "capstone.example.toml")
            self.assertEqual(
                default_config_path(root), root / "config" / "capstone.example.toml"
            )

    def test_a_missing_named_config_is_a_config_failure_not_a_fallback(self) -> None:
        """A typo in ``--config`` must not silently read somebody else's contract."""
        code, _, err = run("doctor", "--config", "does-not-exist.toml")
        self.assertEqual(code, int(ExitCode.CONFIG))
        self.assertIn("does-not-exist.toml", err)

    def test_config_is_accepted_on_either_side_of_the_subcommand(self) -> None:
        """``capstone --config F doctor`` and ``capstone doctor --config F`` are the same run.

        The README and ``--help`` both show the second form, so both have to work; sharing one
        argparse dest between them would let the subparser's default erase the global value,
        which is why the subcommand's copy has its own dest.
        """
        _, before, _ = run("--config", str(helpers.EXAMPLE_CONFIG), "doctor")
        _, after, _ = run("doctor", "--config", str(helpers.EXAMPLE_CONFIG))
        self.assertEqual(before, after)
        self.assertIn("undecided keys", before)

    def test_the_decided_world_moves_past_the_config_check(self) -> None:
        """Recording the decisions changes what blocks the chain, and the doctor sees it.

        The other half of checklist 3.3/3.4/3.6/12.1: a decided config must stop being reported
        as undecided, and the next blocker has to become the missing data contract -- writing
        decisions down does not run a chain either. ``external.name = "none"`` is used here
        because it is the one answer that names no URL: an unverified URL is the failure mode
        decisions.md item 8 warns about, and this test should not model it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # the loader resolves the config's relative paths and refuses one pointing at a
            # directory that does not exist, so a temp stage needs the dirs it names
            (root / "config").mkdir(parents=True)
            (root / "data" / "curated").mkdir(parents=True)
            (root / "sql").mkdir(parents=True)
            decided = helpers.EXAMPLE_CONFIG.read_text(encoding="utf-8")
            for policy in ("no_review", "no_approved_at", "multi_review"):
                decided = decided.replace(f'{policy} = "unset"', f'{policy} = "flag_only"')
            decided = decided.replace('boundary = ""', 'boundary = "2018-07-31"')
            decided = decided.replace('name = ""', 'name = "none"', 1)
            config_path = root / "config" / "capstone.toml"
            config_path.write_text(decided, encoding="utf-8")

            code, out, _ = run("doctor", "--config", str(config_path))
            self.assertNotIn("[FAIL] config", out)
            self.assertIn("[ok  ] config", out)
            self.assertEqual(code, int(ExitCode.DATA))
            self.assertIn("column audit", out)

            (root / "docs" / "evidence").mkdir(parents=True)
            helpers.write_audit(root / "docs" / "evidence" / "column_audit.csv", [])
            code, out, _ = run("doctor", "--config", str(config_path))
            self.assertIn("[ok  ] config", out)
            self.assertEqual(code, int(ExitCode.NOT_READY))
            self.assertIn("chain steps", out)

    def test_a_local_config_wins_over_the_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            local = root / "config" / "capstone.toml"
            local.write_text("x = 1\n", encoding="utf-8")
            self.assertEqual(default_config_path(root), local)


class AuditCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_print_header_is_the_contract_the_audit_file_must_use(self) -> None:
        code, out, _ = run("audit", "--print-header")
        self.assertEqual(code, int(ExitCode.OK))
        self.assertEqual(out.strip().split(","), list(AUDIT_HEADER))

    def test_a_missing_audit_exits_with_the_data_code_and_names_the_file(self) -> None:
        code, _, err = run("audit", "--audit", str(self.root / "nope.csv"))
        self.assertEqual(code, int(ExitCode.DATA))
        self.assertIn("nope.csv", err)

    def test_contradictions_are_printed_one_per_line(self) -> None:
        path = helpers.write_audit(
            self.root / "audit.csv",
            [
                "orders,order_status,approval,TRUE,source,allowed",
                "orders,order_status,delivery,FALSE,source,forbidden",
            ],
        )
        code, _, err = run("audit", "--audit", str(path))
        self.assertEqual(code, int(ExitCode.DATA))
        self.assertIn("contradicting verdicts", err)

    def test_require_complete_turns_a_partial_audit_into_a_failure(self) -> None:
        path = helpers.write_audit(
            self.root / "audit.csv", ["orders,order_id,purchase,TRUE,ddl,allowed"]
        )
        code, out, err = run(
            "audit",
            "--audit",
            str(path),
            "--ddl",
            str(helpers.STAGE2_DDL),
            "--coverage",
            "--require-complete",
        )
        self.assertEqual(code, int(ExitCode.DATA))
        self.assertIn("coverage_missing", out + err)

    def test_a_template_can_be_written_and_then_validated(self) -> None:
        out_path = self.root / "column_audit.csv"
        code, out, _ = run(
            "audit", "--ddl", str(helpers.STAGE2_DDL), "--template-out", str(out_path)
        )
        self.assertEqual(code, int(ExitCode.OK))
        self.assertIn("unverified rows", out)
        rows = out_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(rows[0], ",".join(AUDIT_HEADER))
        self.assertTrue(all(line.endswith("unverified") for line in rows[1:]))


class ErrorShapeTest(unittest.TestCase):
    def test_errors_come_out_on_stderr_with_their_exit_code(self) -> None:
        """A missing input is data (65). ``1`` means the program crashed, and it did not.

        This test used to accept either code, which let a base ``CapstoneError`` through with
        ``UNEXPECTED``: the shell reported the same number a traceback would, and the doctor's
        own docstring says 1 must stay reserved for that. So the assertion is exact now, and
        the test below keeps it exact for every deliberate error class.
        """
        # Isolate input-error handling from config-policy validation.
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "orders.csv"
            with patch(
                "capstone.cli._load",
                return_value=helpers.load_example_contract(),
            ):
                code, out, err = run("predict", "--input", str(missing))
        self.assertEqual(out, "")
        self.assertIn("error:", err)
        self.assertEqual(code, int(ExitCode.DATA))
        self.assertIn(str(missing), err)

    def test_no_deliberate_error_exits_with_the_crash_code(self) -> None:
        """Every ``CapstoneError`` subclass names a code of its own, and none names ``1``."""
        from capstone import errors

        subclasses = {
            name: value
            for name, value in vars(errors).items()
            if isinstance(value, type)
            and issubclass(value, CapstoneError)
            and value is not CapstoneError
        }
        self.assertTrue(subclasses)
        self.assertEqual(
            {name: int(value.exit_code) for name, value in subclasses.items() if value.exit_code == ExitCode.UNEXPECTED},
            {},
        )

    def test_an_unusable_command_is_refused_by_argparse(self) -> None:
        """2 is argparse's own usage code; the project's codes start at 64 on purpose."""
        with self.assertRaises(SystemExit) as caught:
            run("nope")
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
