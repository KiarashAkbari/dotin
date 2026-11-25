"""The ceiling's error line asks for a stored plan; this is the script that stores it.

`capstone features` fails with "Store the EXPLAIN (ANALYZE) output with the number and fix the query",
and until now that sentence had no tool behind it -- a reader had to hand-assemble a psql invocation to
find out whether a breach is a property of the query or of the statistics under it. That distinction is
not academic here: `sql/006` rebuilds `capstone.mv_external_density` on every apply, and a refreshed
materialised view has no rows in `pg_statistic`, so the frame is planned against an empty table. Two
live measurements, one day apart and on the same definition: 477 ms with a mixed external table, 663.4 ms
after it was narrowed to the registered census.

Five rules, all testable without a database:

* the script judges the same statistic the feature step judges (`features.TIMING_BASIS`), not the mean;
* `ANALYZE` goes on the three relations the bridge owns, and the materialised view is skipped when the
  bridge has not been applied -- a fresh database must not be stopped by its own probe;
* the plan is read *after* the ANALYZE, because that is the state a reader has to reproduce;
* the witness refuses to overwrite a file that measured a different ceiling, row count or tuning, which
  is the rule `scripts/cheating_model_gap.py` already applies to a split boundary;
* and a refusal has to end in something to type, so the state picks the filename (`--set` writes a
  `.tuned-<digest>` witness beside the untuned one) and the refusal names the dimension that moved.
  Measured on his machine, 2026-11-25: a `--set` run against the 0036 witness refused with `--force` as
  its only remedy -- which is an instruction to delete the evidence that refuted the ANALYZE theory.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import helpers

STAGE = helpers.STAGE_ROOT


def load_script():
    path = STAGE / "scripts" / "analyze_ceiling_feature.py"
    spec = importlib.util.spec_from_file_location("analyze_ceiling_feature", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class VerdictTests(unittest.TestCase):
    def test_the_best_repeat_is_the_judged_statistic(self) -> None:
        script = load_script()
        out = script.verdict([700.0, 460.0, 690.0, 480.0, 505.0], 500.0)
        self.assertEqual(out["best_ms"], 460.0)
        self.assertEqual(out["mean_ms"], 567.0)
        self.assertTrue(out["passes"], "0030's basis: one fast run is the verdict, the mean is context")

    def test_a_state_fails_when_no_repeat_beats_the_ceiling(self) -> None:
        script = load_script()
        self.assertFalse(script.verdict([663.4, 700.0, 690.0, 710.0, 680.0], 500.0)["passes"])

    def test_a_setting_that_fixes_it_is_named_as_a_condition_not_a_cure(self) -> None:
        script = load_script()
        failing = script.verdict([664.8, 670.0], 500.0)
        tuned = script.verdict([431.0, 440.0], 500.0)
        sentence = script.describe(failing, tuned, state="ANALYZE + SET max_parallel_workers_per_gather=4")
        self.assertIn("artefact", sentence)
        self.assertIn("session condition", sentence)
        self.assertIn("condition the number was measured under", sentence)
        still_failing = script.verdict([660.0, 665.0], 500.0)
        self.assertIn("query finding", script.describe(failing, still_failing, state="ANALYZE + SET x=1"))

    def test_the_three_readings_of_one_run_say_different_things(self) -> None:
        """The sentence is the deliverable, and each shape has a different consequence for the report."""
        script = load_script()
        passed = script.verdict([460.0, 470.0], 500.0)
        self.assertIn("context, not a fix", script.describe(passed, passed))

        artefact = script.verdict([663.4, 670.0], 500.0)
        fixed = script.verdict([430.0, 440.0], 500.0)
        sentence = script.describe(artefact, fixed)
        self.assertIn("artefact", sentence)
        self.assertIn("stale plan", sentence)
        self.assertIn("keep those statistics warm", sentence)

        still = script.verdict([663.4, 670.0], 500.0)
        self.assertIn("query finding", script.describe(still, still))
        self.assertIn("measured shortfall", script.describe(still, still))


class SettingsTests(unittest.TestCase):
    """`--set` is a measurement condition, so it is validated and it is written into the witness."""

    def setUp(self) -> None:
        self.script = load_script()

    def test_a_plain_pair_is_accepted(self) -> None:
        self.assertEqual(
            self.script.parse_settings(["max_parallel_workers_per_gather=4"]),
            {"max_parallel_workers_per_gather": "4"},
        )

    def test_a_second_statement_in_the_value_is_refused(self) -> None:
        for bad in ("", "noequals", "work_mem=", "drop table x=1", "a.b=1", "work_mem=1; select 2 =x"):
            with self.subTest(pair=bad):
                if bad == "work_mem=1; select 2 =x":
                    # the name is legal here; the value is what must stay inert
                    self.assertEqual(
                        self.script.setting_lines(self.script.parse_settings([bad])),
                        ["SET work_mem = '1; select 2 =x'"],
                        msg="the value is quoted and single quotes are doubled, never executed twice",
                    )
                    continue
                with self.assertRaises(ValueError):
                    self.script.parse_settings([bad])

    def test_the_tuning_is_part_of_what_a_witness_claims(self) -> None:
        source = Path(STAGE / "scripts" / "analyze_ceiling_feature.py").read_text(encoding="utf-8")
        self.assertIn("tuned by ", source)
        body = source[source.index("def main(") :]
        self.assertLess(body.index("setting_lines(settings)"), body.index("tuned_runs = measure"))
        self.assertLess(body.index("after_runs = measure"), body.index("setting_lines(settings)"))


class WitnessStateTests(unittest.TestCase):
    """A refusal has to end in something to type, so the measurement state picks the filename."""

    def setUp(self) -> None:
        self.script = load_script()
        self.stored = {"ceiling_ms": "500", "frame_rows": "99281", "tuning": ""}
        self.claims = {
            "ceiling_ms": "500",
            "frame_rows": "99281",
            "tuning": "max_parallel_workers_per_gather=4",
        }

    def test_the_tag_is_a_digest_of_the_sorted_settings(self) -> None:
        import hashlib

        settings = {"work_mem": "64MB", "max_parallel_workers_per_gather": "4"}
        want = hashlib.sha256(b"max_parallel_workers_per_gather=4;work_mem=64MB").hexdigest()[:6]
        self.assertEqual(self.script.tuning_tag(settings), want)
        self.assertEqual(self.script.tuning_tag(dict(reversed(list(settings.items())))), want)
        self.assertEqual(self.script.tuning_tag({}), "", msg="no setting, no suffix")

    def test_the_refusal_names_every_dimension_that_moved(self) -> None:
        self.assertEqual(
            self.script.state_difference(self.stored, self.claims),
            ["tuning: nothing there, max_parallel_workers_per_gather=4 here"],
        )
        self.assertEqual(self.script.state_difference(self.claims, self.claims), [])
        moved = self.script.state_difference(
            self.stored, {**self.claims, "ceiling_ms": "400", "frame_rows": "98000"}
        )
        self.assertEqual([line.split(":")[0] for line in moved], ["ceiling", "frame rows", "tuning"])

    def test_a_refusal_with_one_remedy_only_invites_deleting_the_first_witness(self) -> None:
        """What his `--set` run actually hit: `--force` as the only line of the refusal."""
        base = self.script.DEFAULT_EVIDENCE
        settings = self.script.parse_settings(["max_parallel_workers_per_gather=4"])
        tuned = self.script.witness_path(base, settings)
        self.assertNotEqual(tuned, base)
        text = self.script.refusal(base, self.stored, self.claims, tuned)
        self.assertIn("what differs: tuning: nothing there", text)
        self.assertIn("belongs at docs/evidence/ceiling-feature-analysis.tuned-411fdc.txt", text)
        self.assertNotIn("--force", text, msg="another file exists, so overwriting is not the advice")

    def test_a_refusal_with_nowhere_else_to_write_says_so(self) -> None:
        settings = self.script.parse_settings(["max_parallel_workers_per_gather=4"])
        tuned = self.script.witness_path(self.script.DEFAULT_EVIDENCE, settings)
        text = self.script.refusal(
            tuned, {**self.claims, "tuning": "max_parallel_workers_per_gather=2"}, self.claims, tuned
        )
        self.assertIn("--force", text)
        self.assertIn("what differs: tuning", text)
        self.assertNotIn("belongs at", text, msg="it would name the file it is already writing")


class ScriptShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = (STAGE / "scripts" / "analyze_ceiling_feature.py").read_text(encoding="utf-8")
        self.script = load_script()

    def test_the_targets_are_the_bridge_and_the_frame_it_plans(self) -> None:
        from capstone import features

        self.assertEqual(
            list(self.script.ANALYZE_TARGETS),
            [
                "capstone.external_municipio",
                "capstone.mv_external_density",
                features.FRAME_VIEW,
            ],
            msg="a rename of the frame view must show up here, not only in the SQL",
        )

    def test_the_plan_carries_buffers_and_is_read_after_the_analyze(self) -> None:
        self.assertIn("ANALYZE, BUFFERS", self.script.PLAN_SQL)
        body = self.source[self.source.index("def main(") :]
        self.assertLess(body.index("after_runs = measure"), body.index("PLAN_SQL.format"))

    def test_the_snapshot_is_probed_before_it_is_analyzed(self) -> None:
        """A database without `006` has no materialised view, and `ANALYZE` on it would be an error."""
        body = self.source[self.source.index("for target in ANALYZE_TARGETS") :]
        self.assertLess(body.index("to_regclass"), body.index("ANALYZE {target}"))
        self.assertIn("continue", body)

    def test_it_judges_the_same_basis_and_refuses_a_one_run_protocol(self) -> None:
        self.assertIn("features.TIMING_BASIS", self.source)
        self.assertIn("if args.repeats < 2", self.source)
        self.assertIn("--force", self.source)

    def test_a_witness_from_another_measurement_is_not_overwritten_in_silence(self) -> None:
        body = self.source[self.source.index("if witness.exists()") :]
        self.assertIn("ceiling (\\d+) ms; frame rows (\\d+)(?:; tuned by ([^\\n]*))?", body)
        self.assertIn("return 2", body)
        self.assertIn("not args.force", body[: body.index("print(") + 400])
        # the guard reads the file the run is about to write, not the base name it was handed
        head = self.source[self.source.index("def main(") :]
        self.assertLess(
            head.index("witness = witness_path(args.evidence, settings)"),
            head.index("if witness.exists()"),
            msg="the state decides the name before the stored witness is read",
        )
        self.assertIn("witness.write_text(", head)

    def test_a_tuned_witness_lands_beside_the_untuned_one(self) -> None:
        """`--set` may not be a reason to overwrite the number the report cites."""
        script = self.script
        base = script.DEFAULT_EVIDENCE
        settings = script.parse_settings(["max_parallel_workers_per_gather=4"])
        tuned = script.witness_path(base, settings)
        self.assertNotEqual(tuned, base)
        self.assertEqual(tuned.suffix, base.suffix)
        self.assertTrue(tuned.stem.startswith(base.stem))
        self.assertEqual(tuned.name, "ceiling-feature-analysis.tuned-411fdc.txt")
        self.assertEqual(script.witness_path(tuned, settings), tuned, msg="rewriting its own file is fine")
        self.assertEqual(script.witness_path(base, {}), base, msg="an untuned run keeps the old name")


if __name__ == "__main__":
    unittest.main()
