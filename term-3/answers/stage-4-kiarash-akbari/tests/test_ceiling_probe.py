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
  its only remedy -- which is an instruction to delete the evidence that refuted the ANALYZE theory;
* a state that met the ceiling is called `credited`, `conditional` or `uncredited` by `attribution()`,
  from whether the plan moved and how many of its own repeats met it -- and only the first two exit 0;
* the plan comparison uses the plan's *shape* (estimates, workers, spill) and never its clock, and the
  transfer price comes from `features.FRAME_READ_SQL`, pinned, because `EXPLAIN` discards the rows;
* every state carries its own interval, and neither a pass nor a gap is believed until it clears it --
  `straddles_ceiling()` for a state against the ceiling, `gap_pair()` for a pair of states against each
  other. Both numbers are in the witness, because the reader who disagrees has to be able to check them.
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
        self.assertTrue(
            sentence.startswith("credited"), msg="the sentence opens with the kind the JSON records"
        )
        self.assertIn("has to issue it", sentence)
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
        self.assertTrue(sentence.startswith("credited"))
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


#: two plans that differ only in how many workers the top Gather launched, and one that spills
PLAN_ONE_WORKER = """Gather  (cost=22585.11..39899.80 rows=99252 width=261) (actual rows=99281 loops=1)
  Workers Planned: 1
  Workers Launched: 1
  Buffers: shared hit=17887
  ->  Hash Left Join  (cost=21585.11..28974.60 rows=58384 width=261) (actual rows=49640 loops=2)
        ->  Hash  (cost=4031.34..4031.34 rows=41355 width=74) (actual rows=49640 loops=2)
              Buckets: 131072  Batches: 1  Memory Usage: 11936kB
Planning Time: 2.500 ms
Execution Time: 452.304 ms
"""

PLAN_THREE_WORKERS = PLAN_ONE_WORKER.replace("Workers Planned: 1", "Workers Planned: 3").replace(
    "Workers Launched: 1", "Workers Launched: 3"
)

PLAN_SPILLING = PLAN_ONE_WORKER.replace("Batches: 1", "Batches: 4").replace(
    "Buffers: shared hit=17887", "Buffers: shared hit=17887 read=12 temp=908"
)


class AttributionTests(unittest.TestCase):
    """A number moving is not a condition working: the plan has to move too, and the repeats have to agree.

    The inputs here are his own live numbers from 2026-11-25 (`--set max_parallel_workers_per_gather=4`
    on the 99,281-row frame), which is the case that motivated the gate: the judged statistic went
    666.8 -> 481.2 ms, `Workers Launched` stayed at 1, and only the last of five repeats was under 500.
    """

    def setUp(self) -> None:
        self.script = load_script()

    def test_the_plan_is_compared_on_the_lines_that_would_move(self) -> None:
        self.assertEqual(self.script.workers_launched(PLAN_ONE_WORKER), [1])
        self.assertEqual(self.script.workers_launched("Seq Scan on x"), [])
        self.assertFalse(self.script.plan_changed(PLAN_ONE_WORKER, PLAN_ONE_WORKER))
        self.assertTrue(self.script.plan_changed(PLAN_ONE_WORKER, PLAN_THREE_WORKERS))
        self.assertTrue(self.script.plan_changed(PLAN_ONE_WORKER, PLAN_SPILLING))
        self.assertIn("Workers Launched: 1", "\n".join(self.script.plan_fingerprint(PLAN_ONE_WORKER)))

    def test_the_summary_says_whether_there_is_a_memory_story(self) -> None:
        self.assertIn("Workers Launched: 1", self.script.plan_summary(PLAN_ONE_WORKER))
        self.assertIn("no spill", self.script.plan_summary(PLAN_ONE_WORKER))
        self.assertIn("+ spill", self.script.plan_summary(PLAN_SPILLING))
        self.assertIn("execution 452.304 ms", self.script.plan_summary(PLAN_ONE_WORKER))

    def test_a_pass_with_an_unchanged_plan_is_an_outlier_not_a_verdict(self) -> None:
        """One repeat under the ceiling, in a pair that *could* have settled something: `uncredited`.

        The tuned state's spread is kept just under the 187.3 ms gap between the two bests, or the pair is
        `noise` before anyone gets to ask which repeat won -- `uncredited` is the narrower claim, so it is
        the one that has to clear the bound. That ordering is pinned in the last assertion below.
        """
        before = self.script.verdict([671.347, 670.423, 669.384, 668.676, 668.552], 500)
        tuned = self.script.verdict([660.0, 655.0, 658.0, 662.0, 481.231], 500)
        self.assertEqual((before["best_ms"], before["below_ceiling"]), (668.552, 0))
        self.assertEqual((tuned["best_ms"], tuned["below_ceiling"], tuned["best_repeat"]), (481.231, 1, 5))
        text = self.script.describe(
            before, tuned, state="ANALYZE + SET max_parallel_workers_per_gather=4", plans_changed=False
        )
        self.assertIn("uncredited:", text)
        self.assertIn("1 of 5 repeats", text)
        self.assertIn("fastest as repeat 5 of 5", text)
        self.assertNotIn("session condition", text)
        self.assertLess(
            self.script.interval_ms(tuned),
            self.script.gap_pair(before, tuned, first_name="a", second_name="b")["observed_ms"],
            msg="this pair has to clear the noise bound before `uncredited` is reachable at all",
        )
        credited = self.script.describe(
            before, tuned, state="ANALYZE + SET max_parallel_workers_per_gather=4", plans_changed=True
        )
        self.assertTrue(credited.startswith("credited"))
        self.assertIn("session condition", credited)
        wide = self.script.verdict([903.347, 719.423, 682.384, 668.676, 668.552], 500)
        loose = self.script.verdict([673.286, 677.286, 673.792, 584.003, 481.231], 500)
        self.assertEqual(
            self.script.describe(wide, loose, state="SET x", plans_changed=True).split(":")[0],
            "noise",
            msg="a 234.8 ms interval beats a 187.3 ms difference, whatever won",
        )

    def test_the_sentence_opens_with_the_kind_the_json_records(self) -> None:
        """The witness prints `(conditional)` beside a prose verdict, so the two words have to agree.

        `0039` inherited the word `artefact` for a credited state, which left a reader holding two
        vocabularies for one run -- and "artefact" reads as "not real" next to a number that reproduced.
        `refuted` is the one exception: a state where no repeat met the ceiling opens with the word the
        report acts on.
        """
        failing = self.script.verdict([663.4, 670.0], 500)
        four_of_five = self.script.verdict([443.1, 444.5, 447.5, 463.1, 552.8], 500)
        one_of_five = self.script.verdict([443.1, 552.8, 600.0, 640.0, 660.0], 500)
        cases = (
            ("credited", self.script.verdict([443.1, 444.5, 447.5, 463.1, 470.0], 500), "ANALYZE", True),
            ("credited", four_of_five, "ANALYZE + SET x=1", True),
            ("conditional", four_of_five, "ANALYZE + SET x=1", False),
            ("uncredited", one_of_five, "ANALYZE + SET x=1", False),
            ("refuted", failing, "ANALYZE + SET x=1", True),
        )
        for kind, after, state, plans_changed in cases:
            self.assertEqual(
                kind,
                self.script.attribution(
                    plans_changed=plans_changed,
                    below_ceiling=after["below_ceiling"],
                    repeats=len(after["runs_ms"]),
                ),
                msg=f"{kind}: the case is not built the way its name says",
            )
            text = self.script.describe(failing, after, state=state, plans_changed=plans_changed)
            expected = "query finding" if kind == "refuted" else kind
            self.assertTrue(
                text.startswith(expected),
                msg=f"the machine-readable field says {kind!r} but the prose opens with {text[:24]!r}",
            )

    def test_the_plan_comparison_ignores_the_clock(self) -> None:
        """The first version compared fingerprints with `Execution Time` in them, so it was never equal.

        Two runs of one identical plan differ in wall clock by definition; a gate that fires on that is
        not a gate. This is the bug his `--set` pair exposed: `plan moved across the setting: True`
        printed above a plan that was the same in every line that matters.
        """
        same_plan_other_clock = PLAN_ONE_WORKER.replace("Execution Time: 452.304", "Execution Time: 671.0")
        self.assertEqual(
            self.script.plan_shape(PLAN_ONE_WORKER), self.script.plan_shape(same_plan_other_clock)
        )
        self.assertFalse(self.script.plan_changed(PLAN_ONE_WORKER, same_plan_other_clock))
        self.assertIn("Execution Time: 452.304 ms", "\n".join(self.script.plan_fingerprint(PLAN_ONE_WORKER)))
        self.assertTrue(
            self.script.plan_changed(
                PLAN_ONE_WORKER, PLAN_ONE_WORKER.replace("rows=99252 width=261", "rows=41000 width=261")
            ),
            msg="estimates are what an ANALYZE moves, so they belong in the comparison",
        )
        self.assertNotIn("shared hit", "\n".join(self.script.plan_shape(PLAN_ONE_WORKER)))

    def test_the_shape_is_timing_free_by_construction(self) -> None:
        """The shape cannot move because the clock moved: no timing line names one of `PLAN_KEYS`.

        This pins the *reason*, not just the outcome -- widen `PLAN_KEYS` to include the word `Time` and
        `plan_shape` starts answering "did anything move?" with "the run took a different amount of time",
        which is the bug this whole round is about.
        """
        shape = self.script.plan_shape(PLAN_ONE_WORKER)
        self.assertFalse(
            [line for line in shape if any(key in line for key in self.script.PLAN_TIMING_LINES)],
            msg="the comparison key must not carry a duration",
        )
        self.assertIn("Workers Planned: 1", shape, msg="the number the ceiling finding turns on")
        added = [line for line in self.script.plan_fingerprint(PLAN_ONE_WORKER) if line not in shape]
        self.assertEqual([line.split(":")[0] for line in added], ["Planning Time", "Execution Time"])
        # ... and the witness prints the human-facing one, so the reader can diff what was compared
        lines = ["    fingerprint: " + "; ".join(self.script.plan_fingerprint(PLAN_ONE_WORKER))]
        self.assertIn("Execution Time: 452.304 ms", lines[0])
        self.assertIn("Workers Launched: 1", lines[0])

    def test_the_attribution_vocabulary_has_four_answers_and_two_exits(self) -> None:
        credited = self.script.attribution(plans_changed=True, below_ceiling=5, repeats=5)
        conditional = self.script.attribution(plans_changed=False, below_ceiling=4, repeats=5)
        uncredited = self.script.attribution(plans_changed=False, below_ceiling=1, repeats=5)
        refuted = self.script.attribution(plans_changed=True, below_ceiling=0, repeats=5)
        self.assertEqual(
            (credited, conditional, uncredited, refuted),
            ("credited", "conditional", "uncredited", "refuted"),
        )
        # exactly what his two runs were: 4 of 5, then 5 of 5, on an unchanged plan
        self.assertEqual(
            [
                self.script.attribution(plans_changed=False, below_ceiling=n, repeats=5)
                for n in (4, 5, 3, 2)
            ],
            ["conditional", "conditional", "conditional", "uncredited"],
            msg="a majority of the state's own repeats, not one fast sample, is what makes it conditional",
        )

    def test_a_conditional_pass_says_the_mechanism_is_unknown(self) -> None:
        before = self.script.verdict([682.906, 669.534, 715.054, 663.704, 660.678], 500)
        tuned = self.script.verdict([552.788, 463.106, 444.47, 443.09, 447.529], 500)
        text = self.script.describe(
            before, tuned, state="ANALYZE + SET max_parallel_workers_per_gather=4", plans_changed=False
        )
        self.assertIn("conditional pass:", text)
        self.assertIn("4 of 5 of its own repeats", text)
        self.assertIn("never issues", text)
        self.assertNotIn("artefact", text)
        self.assertIn("0/5 repeats under the ceiling", self.script.state_line("x", before))

    def test_a_read_sample_is_part_of_the_state(self) -> None:
        tuned = self.script.verdict([443.164, 452.706, 463.823, 488.09, 495.376], 500)
        self.assertNotIn("read with transfer", self.script.state_line("tuned", tuned))
        tuned["read_ms"] = 1011.4
        self.assertIn("read with transfer, one sample: 1011.4 ms", self.script.state_line("tuned", tuned))

    def test_the_witness_prints_how_much_of_a_state_is_fluke(self) -> None:
        tuned = self.script.verdict([673.286, 677.286, 673.792, 584.003, 481.231], 500)
        line = self.script.state_line("after  ANALYZE + SET x", tuned)
        self.assertIn("1/5 repeats under the ceiling", line)
        self.assertIn("fastest was repeat 5 of 5", line)

    def test_a_failing_state_says_so_for_both_states(self) -> None:
        before = self.script.verdict([668.6, 669.0, 670.5, 669.9, 667.0], 500)
        after = self.script.verdict([666.78, 670.456, 670.47, 669.904, 666.978], 500)
        text = self.script.describe(before, after)
        self.assertIn("query finding:", text)
        self.assertIn("no repeat in either state met it", text)



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

    def test_the_read_statement_is_the_one_features_defines(self) -> None:
        """`EXPLAIN` discards the rows on the server, so the transfer price has to come from his own SQL."""
        from capstone import features

        self.assertEqual(self.script.READ_SQL, features.FRAME_READ_SQL)
        self.assertNotIn("EXPLAIN", self.script.READ_SQL.upper())

    def test_the_plan_carries_buffers_and_is_read_after_the_analyze(self) -> None:
        self.assertIn("ANALYZE, BUFFERS", self.script.PLAN_SQL)
        body = self.source[self.source.index("def main(") :]
        self.assertLess(body.index("after_runs = measure"), body.index("plan_after = read_plan"))

    def test_every_state_gets_its_own_plan(self) -> None:
        """Attribution needs a plan *per state*; one plan at the end cannot say what a setting changed."""
        body = self.source[self.source.index("def main(") :]
        marks = [
            "before_runs = measure",
            "plan_before = read_plan",
            "for target in ANALYZE_TARGETS",
            "after_runs = measure",
            "plan_after = read_plan",
            "tuned_runs = measure",
            "plan_tuned = read_plan",
        ]
        positions = [body.index(mark) for mark in marks]
        self.assertEqual(
            positions, sorted(positions), msg="the plans must bracket the states they describe"
        )
        for read, state in (
            ("read_before, read_rows = measure_read", "plan_before = read_plan"),
            ("read_after, _ = measure_read", "plan_after = read_plan"),
            ("read_tuned, _ = measure_read", "plan_tuned = read_plan"),
        ):
            self.assertLess(body.index(state), body.index(read), msg=f"{state} then its own read")
        head = self.source[self.source.index("def main(") :]
        self.assertIn("code, note = exit_note(before, after, kind_setting, control, tuned)", head)
        self.assertLess(
            head.index("code, note = exit_note("),
            head.index("witness.write_text"),
            msg="the note has to be in the file, so the check reads the order",
        )
        rule = self.source[self.source.index("def exit_note(") : self.source.index("def ordering_sentence(")]
        self.assertIn('kind_setting in ("credited", "conditional")', rule)
        self.assertIn('not straddles_ceiling(state)', rule, msg="the exit rule is the prose rule")


class ControlTests(unittest.TestCase):
    """The order a state is measured in is a condition too, so it gets measured, not argued about."""

    def setUp(self) -> None:
        self.script = load_script()
        self.source = (STAGE / "scripts" / "analyze_ceiling_feature.py").read_text(encoding="utf-8")

    def test_the_setting_comes_back_off_the_way_it_went_on(self) -> None:
        settings = self.script.parse_settings(["max_parallel_workers_per_gather=2", "work_mem=64MB"])
        self.assertEqual(
            self.script.reset_lines(settings),
            ["RESET work_mem", "RESET max_parallel_workers_per_gather"],
            msg="the reverse of `setting_lines`, so nested settings unwind in the order they were set",
        )
        joined = " ".join(self.script.reset_lines(settings))
        for forbidden in ("ALTER", "postgresql.conf", "RELOAD"):
            self.assertNotIn(forbidden, joined)
        for statement in self.script.reset_lines(settings):
            self.assertFalse(statement.startswith("SET "), msg=f"{statement} would set, not unset")
            self.assertFalse(statement.endswith("'"), msg="RESET takes no value to quote")
        self.assertEqual(self.script.reset_lines({}), [], msg="nothing to unwind when nothing was set")

    def test_a_control_run_is_a_different_measurement_and_gets_a_different_file(self) -> None:
        settings = self.script.parse_settings(["max_parallel_workers_per_gather=4"])
        self.assertEqual(self.script.tag_settings(settings, control=False), settings)
        self.assertEqual(self.script.tuning_tag(self.script.tag_settings(settings, control=False)), "411fdc")
        tagged = self.script.tag_settings(settings, control=True)
        self.assertEqual(tagged.get("control"), "same-state-last")
        base = self.script.DEFAULT_EVIDENCE
        plain = self.script.witness_path(base, settings)
        armed = self.script.witness_path(base, tagged)
        self.assertNotEqual(
            plain.name, armed.name, msg="a run with an extra arm may not overwrite one without"
        )
        self.assertTrue(self.script.TUNED_NAME.search(armed.stem), msg=armed.name)

    def test_a_straddling_state_refuses_the_finding_sentence_too(self) -> None:
        """The `finding:` line is the one the report copies, so it has to be the one that refuses.

        `0039` and `0040` both let a state whose best repeat beat the ceiling print "the frame already
        meets the ceiling", and his nine-repeat run then printed it for a state whose own samples spread
        467.3 to 599.8 ms around that very line. Decorating the sentence would have left the verb in
        place, so the branch returns a different kind word instead -- `noise`, the same one the gap
        between two states gets -- and names what to write instead.
        """
        straddling = self.script.verdict(
            [599.817, 506.688, 479.025, 478.786, 467.323, 473.393, 469.518, 500.936, 483.275], 500
        )
        text = self.script.describe(straddling, straddling)
        self.assertTrue(text.startswith("noise"), msg=text[:80])
        self.assertIn("467.3", text)
        self.assertIn("599.8", text)
        self.assertIn("6 of 9 repeats under", text)
        self.assertNotIn("already meets the ceiling", text)
        self.assertIn("The line is unresolved", text)
        tight = self.script.verdict([460.0, 470.0], 500.0)
        self.assertIn("context, not a fix", self.script.describe(tight, tight))

    def test_the_noise_bound_is_strict_and_says_which_way_it_fell(self) -> None:
        """A gap equal to the interval is not noise, and both outcomes are printed.

        The bound is `abs(gap) < spread`, not `<=`: two states 40 ms apart whose own repeats also spread
        40 ms are at the edge, and the edge has to be readable in the file rather than inferred from a
        sign someone chose. `attribution()` is judged on its own arguments here, because `gap_pair()`
        computes the same comparison a second time for the witness and the two must be pinned together.
        """
        wide = self.script.verdict([500.0, 520.0, 540.0], 500)
        edge = self.script.verdict([540.0, 560.0, 580.0], 500)
        pair = self.script.gap_pair(wide, edge, first_name="the untuned state", second_name="the next state")
        self.assertAlmostEqual(pair["observed_ms"], -40.0)
        self.assertAlmostEqual(pair["spread_ms"], 40.0)
        self.assertFalse(pair["inside_noise"], msg="40 against 40 is the edge, not inside")
        self.assertIn("the gap clears it", self.script.noise_line("the ANALYZE", pair))
        for delta, word in ((0.0, "conditional"), (-0.1, "noise")):
            with self.subTest(delta=delta):
                self.assertEqual(
                    self.script.attribution(
                        plans_changed=False,
                        below_ceiling=2,
                        repeats=3,
                        gap_ms=40.0 + delta,
                        spread_ms=40.0,
                    ),
                    word,
                )
        inside = self.script.gap_pair(
            wide, self.script.verdict([505.0, 520.0, 535.0], 500), first_name="a", second_name="b"
        )
        self.assertTrue(inside["inside_noise"])
        self.assertIn("the gap is inside the noise", self.script.noise_line("the setting", inside))
        self.assertIn(
            "no second state to compare against",
            self.script.noise_line("the setting", None),
            msg="a run without `--set` has no second state, and says so instead of printing zeros",
        )

    def test_the_bound_is_written_before_the_witness_is(self) -> None:
        """The lines exist to be *stored*, so the ordering inside `main()` is the thing worth pinning.

        `bounds_lines()` is pure and its output is checked above; what no unit test can reach is the one
        call site, which sits inside `main()` behind a database connection. Deleting it there leaves every
        other assertion green and turns the bound back into something a reader has to re-measure, so this
        is the one place where reading the source is the honest test -- and the ordering matters as much as
        the presence: a line appended after `witness.write_text` is a line printed and not stored.
        """
        body = (STAGE / "scripts" / "analyze_ceiling_feature.py").read_text(encoding="utf-8")
        body = body[body.index("def main(") :]
        self.assertIn("lines += bounds_lines(gap_analyze, gap_setting)", body)
        self.assertLess(
            body.index("lines += bounds_lines(gap_analyze, gap_setting)"),
            body.index("witness.write_text"),
            msg="the bound has to be in the file, not only on the terminal",
        )

    def test_both_bound_lines_are_in_the_witness(self) -> None:
        """The bound is only evidence if it is on disk next to the number it bounds.

        `0040` printed its plan comparison into a file; a reader who has to re-run to learn the noise the
        number had to clear has been told nothing. The second line is conditional on there being a second
        comparison at all -- a run without `--set` has an ANALYZE gap and no setting gap, and printing
        "no second state to compare against" under a heading that never mentioned one is noise of a
        different kind.
        """
        wide = self.script.verdict([500.0, 520.0, 540.0, 560.0], 500)
        near = self.script.verdict([505.0, 525.0, 545.0], 500)
        far = self.script.verdict([700.0, 705.0, 710.0], 500)
        analyze = self.script.gap_pair(wide, near, first_name="a", second_name="b")
        setting = self.script.gap_pair(near, far, first_name="a", second_name="b")
        self.assertTrue(analyze["inside_noise"], msg="the first pair is the wide one")
        self.assertFalse(setting["inside_noise"])
        lines = self.script.bounds_lines(analyze, setting)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("noise bound" in line for line in lines), msg=lines)
        self.assertIn("inside the noise", lines[0])
        self.assertIn("clears it", lines[1])
        self.assertEqual(len(self.script.bounds_lines(analyze, None)), 1)

    def test_a_states_own_interval_is_what_a_verdict_has_to_clear(self) -> None:
        """The replacement for "is it still falling?", and his run is why.

        First attempt said: a state is unmeasured when its fastest repeat is its last one. His warm
        session then read ``[599.8, 506.7, 479.0, 478.8, 467.3, 473.4, 469.5, 500.9, 483.3]`` for the
        *untuned* frame -- best at repeat 5 of 9, so "converged" by that rule -- while its best of 467.3 ms
        already beat a 500 ms ceiling that the same nine samples also missed twice. Whether the state had
        settled is the wrong question; whether the ceiling sits inside the state's own interval is the
        right one, and it needs no threshold.
        """
        straddling = self.script.verdict(
            [599.817, 506.688, 479.025, 478.786, 467.323, 473.393, 469.518, 500.936, 483.275], 500
        )
        self.assertAlmostEqual(straddling["spread_ms"], 132.494)
        self.assertAlmostEqual(straddling["ceiling_margin_ms"], 32.677)
        self.assertTrue(self.script.straddles_ceiling(straddling), msg="it both met and missed 500 ms")
        self.assertTrue(straddling["passes"], msg="and it still passed on the judged statistic")
        tight = self.script.verdict([452.561, 452.16, 454.608, 454.153, 452.394], 500)
        self.assertFalse(self.script.straddles_ceiling(tight))
        missed = self.script.verdict([671.709, 672.355, 651.033, 567.871, 532.816], 500)
        self.assertFalse(self.script.straddles_ceiling(missed), msg="0/5 under, and the interval says so")

    def test_drift_is_halves_because_a_run_need_not_be_monotone(self) -> None:
        """`first to last` was the first attempt at drift and his after-ANALYZE state broke it.

        That state read ``[472.9, 474.4, 473.6, 476.1, 641.1, 657.7, 584.1, 515.6, 521.0]``: endpoints say
        a 48 ms *improvement*, while the second half averaged 109.7 ms slower than the first. Averaging
        each half is what makes an outlier at either end unable to pick the sign.
        """
        rising = self.script.verdict(
            [472.862, 474.417, 473.553, 476.107, 641.069, 657.688, 584.05, 515.603, 521.037], 500
        )
        self.assertAlmostEqual(self.script.trend_ms(rising), 109.655)
        endpoint = rising["runs_ms"][0] - rising["runs_ms"][-1]
        self.assertLess(endpoint, 0.0, msg="first-minus-last reads a rising state as an improvement")
        tight_state = settled = self.script.verdict([452.561, 452.16, 454.608, 454.153, 452.394], 500)
        self.assertLess(abs(self.script.trend_ms(settled)), 5.0)
        self.assertEqual(self.script.trend_ms(self.script.verdict([500.0], 500)), 0.0)
        state = {
            **rising,
            "trend_ms": self.script.trend_ms(rising),
            "straddles_ceiling": self.script.straddles_ceiling(rising),
        }
        line = self.script.state_line("after  ANALYZE", state)
        self.assertIn("spread 184.8 ms, drift +109.7 ms", line)
        self.assertIn("the ceiling is inside this state's own interval", line)
        tight_line = self.script.state_line(
            "tuned",
            {
                **tight_state,
                "trend_ms": self.script.trend_ms(tight_state),
                "straddles_ceiling": self.script.straddles_ceiling(tight_state),
            },
        )
        self.assertIn("the ceiling is outside it", tight_line)

    def test_the_split_prices_the_order_instead_of_blaming_the_query(self) -> None:
        """`ordering` is what it sounds like: most of the gap was already on its way there.

        His `--set max_parallel_workers_per_gather=2` run: `after ANALYZE` best 532.8 ms, tuned best
        452.2 ms -> 80.6 ms credited to the setting. A control that finds the *untuned* state at 456 ms
        by the end of the session leaves 3.8 ms for the setting, which is not a fix.
        """
        after = self.script.verdict([532.816], 500)
        tuned = self.script.verdict([452.16], 500)
        control = self.script.verdict([456.0], 500)
        split = self.script.gain_split(
            naive_ms=after["best_ms"] - tuned["best_ms"], drift_ms=after["best_ms"] - control["best_ms"]
        )
        self.assertAlmostEqual(split["naive_ms"], 80.656)
        self.assertAlmostEqual(split["ordering_ms"], 76.816)
        self.assertAlmostEqual(split["attributed_ms"], 3.84)
        self.assertGreater(split["ordering_share"], 0.5)
        # a control that came out *slower* is not a licence to inflate the condition's share
        cold = self.script.gain_split(naive_ms=80.0, drift_ms=-20.0)
        self.assertEqual((cold["ordering_ms"], cold["attributed_ms"]), (0.0, 80.0))
        # and a gap larger than the state's own drift cannot be all ordering
        capped = self.script.gain_split(naive_ms=30.0, drift_ms=200.0)
        self.assertEqual((capped["ordering_ms"], capped["ordering_share"]), (30.0, 1.0))
        self.assertEqual(self.script.gain_split(naive_ms=0.0, drift_ms=10.0)["ordering_share"], 0.0)

    def test_a_gap_inside_the_states_own_spread_is_called_noise(self) -> None:
        """The cheapest refusal there is: no control arm, no extra run, one subtraction.

        His `--set max_parallel_workers_per_gather=2` pair: `after ANALYZE` best 532.8 ms with a spread of
        139.6 ms, tuned best 452.2 ms with a spread of 2.4 ms. The 80.7 ms difference is smaller than the
        interval the *first* state already covers, so the pair could not have settled whether the setting
        did anything -- and `ordering`, which needed a control run to say so, never gets asked.
        """
        after = self.script.verdict([671.709, 672.355, 651.033, 567.871, 532.816], 500)
        tuned = self.script.verdict([452.561, 452.16, 454.608, 454.153, 452.394], 500)
        gap = self.script.gap_pair(
            after, tuned, first_name="the ANALYZE state", second_name="the tuned state"
        )
        self.assertAlmostEqual(gap["observed_ms"], 80.656)
        self.assertAlmostEqual(gap["spread_ms"], 139.539)
        self.assertTrue(gap["inside_noise"])
        self.assertEqual(gap["spread_of"], "the ANALYZE state")
        self.assertEqual(
            self.script.attribution(
                plans_changed=True, below_ceiling=5, repeats=5, gap_ms=gap["observed_ms"],
                spread_ms=gap["spread_ms"],
            ),
            "noise",
            msg="a moved plan does not rescue a gap the spread already covers",
        )
        # and a wide enough gap is *not* noise: the rule is a bound, not a veto
        wide = self.script.gap_pair(
            self.script.verdict([700.0, 710.0, 705.0], 500),
            tuned,
            first_name="the untuned state",
            second_name="the tuned state",
        )
        self.assertFalse(wide["inside_noise"])
        exact = self.script.verdict([500.0, 520.0, 540.0], 500)  # spread 40.0
        self.assertFalse(
            self.script.gap_pair(
                exact,
                self.script.verdict([540.0, 560.0, 580.0], 500),
                first_name="a",
                second_name="b",
            )["inside_noise"],
            msg="a 40 ms gap against a 40 ms interval is the edge, and the edge is not noise",
        )
        self.assertEqual(
            self.script.attribution(
                plans_changed=True, below_ceiling=5, repeats=5, gap_ms=wide["observed_ms"],
                spread_ms=wide["spread_ms"],
            ),
            "credited",
        )

    def test_noise_outranks_the_other_four_and_only_refuted_outranks_it(self) -> None:
        """Ranking, in the order a reader should be told: is there a verdict at all, then whose it is."""
        five = dict(below_ceiling=5, repeats=5)
        for plans_changed, share, kind in (
            (True, 0.94, "ordering"),
            (False, 0.94, "ordering"),
            (True, 0.2, "credited"),
            (False, 0.2, "conditional"),
            (False, None, "conditional"),
            (True, None, "credited"),
        ):
            with self.subTest(plans_changed=plans_changed, share=share):
                self.assertEqual(
                    self.script.attribution(plans_changed=plans_changed, ordering_share=share, **five), kind
                )
        self.assertEqual(
            self.script.attribution(plans_changed=True, ordering_share=0.9, below_ceiling=0, repeats=5),
            "refuted",
            msg="a state nobody met is not made interesting by a control",
        )
        for share in (None, 0.9):
            for plans_changed in (True, False):
                with self.subTest(share=share, plans_changed=plans_changed):
                    self.assertEqual(
                        self.script.attribution(
                            plans_changed=plans_changed,
                            ordering_share=share,
                            gap_ms=20.0,
                            spread_ms=60.0,
                            **five,                        ),
                        "noise",
                    )
        self.assertEqual(
            self.script.attribution(
                plans_changed=True, gap_ms=20.0, spread_ms=60.0, below_ceiling=0, repeats=5
            ),
            "refuted",
            msg="nobody met the ceiling, so there is nothing to un-credit",
        )

    def test_the_fifth_word_is_the_one_that_survives_a_moved_plan(self) -> None:
        """A plan that moved *and* a gap the control explains is still not a pass.

        `credited` used to be the most a state could earn. If most of the improvement was already on its
        way there, crediting the condition -- even one whose plan changed -- is the same error one level
        up, so `ordering` outranks it and only `attributed_ms` would be left to argue from.
        """
        five = dict(below_ceiling=5, repeats=5)
        for plans_changed, share, kind in (
            (True, 0.94, "ordering"),
            (False, 0.94, "ordering"),
            (True, 0.2, "credited"),
            (False, 0.2, "conditional"),
            (False, None, "conditional"),
            (True, None, "credited"),
        ):
            with self.subTest(plans_changed=plans_changed, share=share):
                self.assertEqual(
                    self.script.attribution(plans_changed=plans_changed, ordering_share=share, **five), kind
                )
        self.assertEqual(
            self.script.attribution(plans_changed=True, ordering_share=0.9, below_ceiling=0, repeats=5),
            "refuted",
            msg="a state nobody met is not made interesting by a control",
        )

    def test_the_ordering_sentence_says_what_is_left(self) -> None:
        """The control arm still gets the last word when the states themselves are tight.

        Two states that each measure themselves to within a third of a millisecond, 80.7 ms apart, and a
        control that lands 3.8 ms from the tuned one: the gap clears the noise bound, so the question is
        still open -- and it is the control that closes it, at 95% ordering. His `=2` pair never reaches
        this branch, because its `after ANALYZE` state is 139 ms wide; that is the `noise` case, pinned by
        the test above.
        """
        after_analyze = self.script.verdict([532.9, 532.8, 533.0, 532.85, 532.95], 500)
        tuned = self.script.verdict([452.561, 452.16, 454.608, 454.153, 452.394], 500)
        control = self.script.verdict([456.0, 456.4, 455.9], 500)
        gap = self.script.gap_pair(
            after_analyze, tuned, first_name="the ANALYZE state", second_name="the tuned state"
        )
        self.assertFalse(gap["inside_noise"], msg="80.6 ms observed against 2.4 ms of spread")
        split = self.script.gain_split(
            naive_ms=after_analyze["best_ms"] - tuned["best_ms"],
            drift_ms=after_analyze["best_ms"] - control["best_ms"],
        )
        self.assertGreater(split["ordering_share"], 0.5)
        text = self.script.describe(
            after_analyze,
            tuned,
            state="ANALYZE + SET max_parallel_workers_per_gather=2",
            plans_changed=True,
            split=split,
            gap=gap,
        )
        self.assertTrue(text.startswith("ordering"), msg=text[:60])
        self.assertIn("came to within 3.7 ms of the tuned one", text)
        self.assertIn("So 76.9 ms (95%) of the gap is the tail of warming", text)
        self.assertIn("is not enough to write the ceiling off", text)
        self.assertNotIn("conditional", text)

    def test_the_noise_sentence_quotes_each_states_own_count(self) -> None:
        """One sentence, one denominator per fraction.

        `describe()` used to print the untuned state's `below_ceiling` over the *tuned* state's repeat
        count -- "1 of 5" for a nine-repeat state, which is the arithmetic this file keeps being written
        to prevent. Both pairs are now quoted, and the gap is quoted without a sign, because a widening is
        not a debt.
        """
        after_analyze = self.script.verdict([671.709, 672.355, 651.033, 567.871, 532.816], 500)
        tuned = self.script.verdict([452.561, 452.16, 454.608, 454.153, 452.394], 500)
        text = self.script.describe(
            after_analyze,
            tuned,
            state="ANALYZE + SET max_parallel_workers_per_gather=2",
            plans_changed=False,
        )
        self.assertTrue(text.startswith("noise"), msg=text[:60])
        self.assertIn("0/5 of the untuned repeats and 5/5 of these ones met the ceiling", text)
        self.assertIn("139.5 ms that the untuned state's own repeats spread", text)
        self.assertIn("80.7 ms between the two states", text)
        # seven repeats against five: the two fractions must not share a denominator
        seven = self.script.verdict([505.0, 507.0, 509.0, 506.0, 508.0, 505.5, 507.5], 500)
        uneven = self.script.describe(
            seven,
            self.script.verdict([498.0, 510.0, 520.0, 515.0, 512.0], 500),
            state="ANALYZE",
            plans_changed=False,
        )
        self.assertTrue(uneven.startswith("noise"), msg=uneven[:60])
        self.assertIn("0/7 of the untuned repeats and 1/5 of these ones met the ceiling", uneven)
        # a straddling *first* state never reaches that sentence: it is refused its own, earlier
        nine = self.script.verdict(
            [599.817, 506.688, 479.025, 478.786, 467.323, 473.393, 469.518, 500.936, 483.275], 500
        )
        self.assertIn("6 of 9 repeats under", self.script.describe(nine, tuned, state="ANALYZE"))
        self.assertNotIn("+", text.split(".")[0], msg="no sign on a gap this sentence refuses to interpret")

    def test_the_control_is_measured_after_every_state_it_must_follow(self) -> None:
        head = self.source[self.source.index("def main(") :]
        for earlier, later in (
            ("read_tuned, _ = measure_read", "for statement in reset_lines(settings)"),
            ("for statement in reset_lines(settings)", "control_runs = measure("),
        ):
            self.assertLess(head.index(earlier), head.index(later), msg=f"{earlier} then {later}")
        self.assertIn('"control": control', head)
        self.assertIn('"gain_split": split', head)
        self.assertIn("code, note = exit_note(before, after, kind_setting, control, tuned)", head)
        self.assertIn('"exit": {"code": code, "note": note}', head)
        self.assertIn("if tuned is not None and control is not None", head)
        self.assertLess(
            head.index("control_runs = measure("), head.index("finally:"), msg="inside the connection"
        )

    def test_the_witness_header_says_which_arm_ran(self) -> None:
        head = self.source[self.source.index("def main(") :]
        self.assertLess(
            head.index('"control: the last untuned state'),
            head.index('"companion: the same measurement without the setting'),
            msg="the arm first, then where the untuned numbers live",
        )
        self.assertIn("state_line(\"control (ANALYZE, no setting, measured last)\", control)", head)
        self.assertIn("gain split:", head)

    def test_the_setting_verdict_is_not_a_copy_of_the_analyze_verdict(self) -> None:
        """His `--control` run printed the same sentence twice, because the first state owned the branch.

        `with the setting applied:` is `describe(before, tuned)`; `before` there is the *untuned* state, and
        it both met and missed 500 ms, so the straddle refusal fired and the tuned state's own `0/9` -- plus
        the 100% ordering the control had just bought -- never reached a sentence. The pair has to be
        described by the numbers of the state it was about, and the price quoted when it exists.
        """
        v = self.script.verdict
        v = self.script.verdict
        before = v([830.23, 728.441, 736.218, 719.765, 693.183, 636.947, 547.974, 471.879, 476.166], 500)
        after = v([622.326, 576.355, 582.924, 722.45, 598.457, 576.468, 583.849, 570.534, 564.06], 500)
        tuned = v([638.315, 623.592, 550.806, 585.499, 598.837, 555.188, 542.311, 567.065, 609.728], 500)
        control = v([541.983, 540.242, 536.956, 653.44, 583.703, 557.223, 523.016, 561.638, 525.526], 500)
        split = self.script.gain_split(
            naive_ms=after["best_ms"] - tuned["best_ms"], drift_ms=after["best_ms"] - control["best_ms"]
        )
        self.assertAlmostEqual(split["ordering_share"], 1.0)
        self.assertAlmostEqual(split["attributed_ms"], 0.0)
        setting = self.script.describe(
            before,
            tuned,
            state="ANALYZE + SET max_parallel_workers_per_gather=2",
            plans_changed=False,
            split=split,
            gap=self.script.gap_pair(
                after, tuned, first_name="the ANALYZE state", second_name="the tuned state"
            ),
        )
        analyze = self.script.describe(before, after, plans_changed=False)
        self.assertNotEqual(setting, analyze, msg="two comparisons, two sentences")
        for expected in (
            "query finding: ANALYZE + SET max_parallel_workers_per_gather=2 moves the fastest repeat from "
            "471.9 ms to 542.3 ms",
            "21.7 ms that `ANALYZE + SET max_parallel_workers_per_gather=2` appears to recover is 100% "
            "ordering (0.0 ms left for the condition)",
            "2/9 of the untuned state and 0/9 of the state under test met the ceiling",
            "this run says neither `meets` nor `breaches`",
        ):
            self.assertIn(expected, setting, msg=expected[:48])
        self.assertIn("query finding: ANALYZE moves the fastest repeat from 471.9 ms to 564.1 ms", analyze)
        self.assertNotIn("control arm", analyze, msg="this comparison has no price to quote")

    def test_a_priced_ordering_outranks_the_straddle_refusal(self) -> None:
        """When the control explains the pair, it speaks first -- the straddle sentence is about state one.

        Same untuned state as above (best 471.9, straddling), but this time the tuned state is a majority
        pass, so the verdict is `ordering` rather than `refuted` -- and it must still not be replaced by the
        refusal that used to sit above it.
        """
        before = self.script.verdict(
            [830.23, 728.441, 736.218, 719.765, 693.183, 636.947, 547.974, 471.879, 476.166], 500
        )
        after = self.script.verdict([622.326, 600.0, 610.0, 615.0, 612.0], 500)
        tuned = self.script.verdict([452.561, 452.16, 454.608, 454.153, 452.394], 500)
        control = self.script.verdict([460.983, 462.242, 461.956], 500)
        split = self.script.gain_split(
            naive_ms=after["best_ms"] - tuned["best_ms"], drift_ms=after["best_ms"] - control["best_ms"]
        )
        self.assertTrue(before["passes"] and self.script.straddles_ceiling(before))
        text = self.script.describe(
            before,
            tuned,
            state="ANALYZE + SET max_parallel_workers_per_gather=2",
            plans_changed=True,
            split=split,
            gap=self.script.gap_pair(after, tuned, first_name="a", second_name="b"),
        )
        self.assertTrue(text.startswith("ordering"), msg=text[:70])
        self.assertIn("5 of 5 of its own repeats under 500 ms", text)
        self.assertIn("came to within 8.8 ms of the tuned one", text)
        self.assertNotIn("both met and missed", text)

    def test_a_straddling_pass_does_not_exit_green(self) -> None:
        """The exit code and the prose have to be refused by the same test.

        `0041` fixed the sentence and left `met = before["passes"] or after["passes"]` alone, so the run
        whose own words were "the line is unresolved on this machine" returned 0 -- and an exit code is what
        a harness, a Makefile and a green tick all read. A state counts here only when it met the ceiling
        *outside* its own interval, and the reason goes into the witness beside the numbers.
        """
        v = self.script.verdict
        before = v([830.23, 728.441, 736.218, 719.765, 693.183, 636.947, 547.974, 471.879, 476.166], 500)
        after = v([622.326, 576.355, 582.924, 722.45, 598.457, 576.468, 583.849, 570.534, 564.06], 500)
        control = v([541.983, 540.242, 536.956, 653.44, 583.703, 557.223, 523.016, 561.638, 525.526], 500)
        self.assertTrue(before["passes"], msg="the minimum alone used to be enough")
        self.assertTrue(self.script.straddles_ceiling(before))
        code, note = self.script.exit_note(before, after, "refuted", control)
        self.assertEqual(code, 1)
        self.assertIn("red: no state met 500 ms outside its own interval", note)
        self.assertIn("the control arm is what priced the gap", note)
        self.assertIn(
            "take more repeats on the same command", note, msg="a refusal has to end in something to type"
        )
        self.assertNotIn("--repeats 15", note, msg="15 was the count this very run used; a fixed number ages badly")
        self.assertIn("as the unresolved line the witness already spells out", note)
        self.assertIn("section 8 of `report.md`", note)
        bare = self.script.exit_note(before, after, "refuted", None)
        self.assertEqual(bare[0], 1)
        self.assertNotIn("control arm", bare[1], msg="no arm was taken; do not describe one")
        tight = v([452.561, 452.16, 454.608, 454.153, 452.394], 500)
        code0, note0 = self.script.exit_note(tight, after, "refuted", None)
        # `tight` is the first argument here, so it *is* the untuned state of the pair -- the note names it
        self.assertEqual(code0, 0)
        self.assertIn("the untuned state met", note0, msg="a green exit says what it was granted for")
        # a state outside the rule's reach used to be invisible: `0042` looked only at `before`/`after`
        self.assertEqual(self.script.exit_note(before, after, "refuted", None, tight)[0], 0)
        self.assertIn("the tuned state met", self.script.exit_note(before, after, "refuted", None, tight)[1])
        # `conditional` buys the exit on attribution alone, with no state meeting the line outside its
        # own interval: an empty `met` used to reach for met[-1] and raise IndexError instead of exiting
        self.assertEqual(self.script.exit_note(before, after, "conditional", None), (0, ""))
        failing = v([600.0, 610.0, 605.0], 500)
        self.assertEqual(self.script.exit_note(failing, after, "uncredited", None)[0], 1)

    def test_a_priced_pair_is_not_answered_by_the_untuned_state(self) -> None:
        """`0042` left the duplicate alive for the one case his 15-repeat run actually was.

        The gate excluded `refuted`, but this pair is `noise`: both sides met the ceiling, so
        `attribution()` answered `noise` for the 3.4 ms gap and the straddle branch on `before` fired first
        again -- printing the ANALYZE sentence a second time and leaving the tuned state's `15/15` unsaid.
        With a control arm in hand (`split` is not None) the pair has its own sentence whatever the kind
        word is; the straddle branch keeps only the pairs no arm priced.
        """
        v = self.script.verdict
        before = v(
            [716.385, 680.524, 692.036, 686.152, 738.026, 700.403, 667.655, 511.659, 466.878, 472.682,
             480.869, 472.146, 462.754, 527.872, 470.169],
            500,
        )
        after = v(
            [549.609, 463.633, 463.943, 469.557, 470.217, 458.018, 455.84, 452.293, 453.586, 463.704,
             450.818, 452.604, 448.124, 450.589, 471.88],
            500,
        )
        tuned = v(
            [456.104, 458.263, 460.473, 454.776, 452.988, 455.748, 463.038, 453.258, 456.072, 452.973,
             461.729, 461.984, 454.302, 452.674, 451.553],
            500,
        )
        control = v(
            [479.281, 455.514, 458.362, 457.486, 453.852, 455.491, 456.579, 453.605, 453.757, 454.358,
             462.349, 453.894, 455.881, 462.876, 457.367],
            500,
        )
        self.assertTrue(self.script.straddles_ceiling(before))
        self.assertFalse(self.script.straddles_ceiling(tuned), msg="15/15 inside an 11.5 ms band")
        split = self.script.gain_split(
            naive_ms=after["best_ms"] - tuned["best_ms"], drift_ms=after["best_ms"] - control["best_ms"]
        )
        self.assertAlmostEqual(split["naive_ms"], -3.429, places=3)
        self.assertEqual(split["ordering_share"], 0.0)
        setting = self.script.describe(
            before,
            tuned,
            state="ANALYZE + SET max_parallel_workers_per_gather=2",
            plans_changed=False,
            split=split,
            gap=self.script.gap_pair(
                after, tuned, first_name="the ANALYZE state", second_name="the tuned state"
            ),
        )
        self.assertTrue(setting.startswith("noise: the 3.4 ms between the two states"), msg=setting[:60])
        self.assertIn("6/15 of the untuned repeats and 15/15 of these ones met the ceiling", setting)
        self.assertIn("both sides met it, so this pair measured the width of a band", setting)
        self.assertIn("the control arm says there is nothing here to credit", setting)
        self.assertNotIn("The line is unresolved", setting, msg="that sentence belongs to the other pair")
        code, note = self.script.exit_note(before, after, "noise", control, tuned)
        self.assertEqual(code, 0)
        self.assertIn("note: the tuned state and the control met 500 ms outside its own interval", note)
        self.assertIn("this is not a fix *by the setting*", note)
        self.assertEqual(self.script.exit_note(before, after, "noise", None, None)[0], 1)

    def test_a_priced_noise_pair_shares_the_arms_number(self) -> None:
        """`noise` and `ordering` are the same doubt at two prices, and the witness must say which.

        A pair whose gap its own spread swallows, with a control arm that credits most of the gap to
        measuring later, is judged `noise` (the gap test is the first question `attribution()` asks). The
        price is the second, and it belongs in the same sentence: dropping the clause leaves a reader with
        "this pair never measured the condition" and no way to see what the arm actually paid.
        """
        v = self.script.verdict
        before = v([471.9, 830.2, 500.1], 500)
        after = v([472.3, 560.0, 549.0], 500)
        tuned = v([450.0, 560.0, 550.0], 500)
        control = v([458.0, 456.0, 460.0], 500)
        split = self.script.gain_split(
            naive_ms=after["best_ms"] - tuned["best_ms"], drift_ms=after["best_ms"] - control["best_ms"]
        )
        self.assertAlmostEqual(split["naive_ms"], 22.3, places=1)
        self.assertAlmostEqual(split["ordering_share"], 0.731, places=3)
        text = self.script.describe(
            before,
            tuned,
            state="ANALYZE + SET work_mem=64MB",
            plans_changed=False,
            split=split,
            gap=self.script.gap_pair(after, tuned, first_name="the ANALYZE state", second_name="the tuned state"),
        )
        self.assertTrue(text.startswith("noise: the 22.3 ms between the two states"), msg=text[:60])
        self.assertIn("and the control arm says 73% of the gap is ordering: 6.0 ms is what is left", text)
        self.assertNotIn("The line is unresolved", text)
        unpriced = self.script.describe(
            before,
            after,
            plans_changed=False,
            gap=self.script.gap_pair(before, after, first_name="x", second_name="y"),
        )
        self.assertIn("The line is unresolved", unpriced, msg="no arm: the straddle branch owns this pair")

    def test_the_gain_split_line_states_its_own_bound(self) -> None:
        """A priced gap smaller than the spread of the arms is not a price, and must not read like one.

        His `gain split: -3.4 ms observed = -3.4 ms credited to the condition` was two claims about a
        number neither arm could resolve -- `credited` implies a measurement, and the negative sign means
        the tuned state was *slower*. The line now carries the bound that made it unreadable.
        """
        src = (STAGE / "scripts" / "analyze_ceiling_feature.py").read_text(encoding="utf-8")
        body = src[src.index("def main(") :]
        self.assertIn("inside the {gap_setting['spread_ms']:.1f} ms spread of the states being compared", body)
        joined = " ".join(part.strip() for part in body.splitlines())
        self.assertIn("so this line prices a gap neither arm could have resolved", joined.replace('" "', ""))

    def test_the_plan_comparison_ignores_the_clock(self) -> None:
        """A summary a reader cannot check is a claim; a JSON key that lies about its content is worse.

        The first version stored the timing-free shape lists under `plan_fingerprints`, which is the name
        of the *other* list -- the one with the times in it -- so a reader diffing the two files was
        diffing something else than the gate had compared. Both halves are pinned here: the witness prints
        one fingerprint per state (that is where a human sees the two plans line by line), and the JSON
        calls the compared lists what they are.
        """
        head = self.source[self.source.index("def main(") :]
        self.assertEqual(
            head.count("'; '.join(plan_fingerprint"), 3, msg="one fingerprint per measured state"
        )
        for state in ("plan_before", "plan_after", "plan_tuned"):
            self.assertLess(
                head.index(f"plan_summary({state})"),
                head.index(f"'; '.join(plan_fingerprint({state}))"),
                msg=f"the readable summary first, then the lines behind it, for {state}",
            )
        self.assertIn('"plan_shapes_compared"', head)
        self.assertNotIn('"plan_fingerprints"', head, msg="that key holds shapes, and was renamed")

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
            head.index("witness = witness_path(args.evidence, tagged)"),
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
