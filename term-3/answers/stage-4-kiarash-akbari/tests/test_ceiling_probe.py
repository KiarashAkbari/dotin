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
  transfer price comes from `features.FRAME_READ_SQL`, pinned, because `EXPLAIN` discards the rows.
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
        before = self.script.verdict([903.347, 719.423, 682.384, 668.676, 668.552], 500)
        tuned = self.script.verdict([673.286, 677.286, 673.792, 584.003, 481.231], 500)
        self.assertEqual((before["best_ms"], before["below_ceiling"]), (668.552, 0))
        self.assertEqual((tuned["best_ms"], tuned["below_ceiling"], tuned["best_repeat"]), (481.231, 1, 5))
        text = self.script.describe(
            before, tuned, state="ANALYZE + SET max_parallel_workers_per_gather=4", plans_changed=False
        )
        self.assertIn("uncredited:", text)
        self.assertIn("1 of 5 repeats", text)
        self.assertIn("fastest as repeat 5 of 5", text)
        self.assertNotIn("session condition", text)
        credited = self.script.describe(
            before, tuned, state="ANALYZE + SET max_parallel_workers_per_gather=4", plans_changed=True
        )
        self.assertTrue(credited.startswith("credited"))
        self.assertIn("session condition", credited)

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
        self.assertIn('kind_setting in ("credited", "conditional")', head)

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

    def test_a_state_whose_fastest_repeat_was_its_last_one_has_not_converged(self) -> None:
        """His `after ANALYZE` line, verbatim: still falling when the timer ran out.

        Six-digit precision off his witness. The best repeat was number 5 of 5 and first-to-last was
        138.9 ms -- larger than the 80.6 ms the same run then credited to `--set`. A statistic that
        reads a descending sequence's last element as its "best of" is reading the clock, not the state.
        """
        warming = self.script.verdict([671.709, 672.355, 651.033, 567.871, 532.816], 500)
        self.assertTrue(self.script.still_warming(warming))
        self.assertAlmostEqual(self.script.warmth_ms(warming), 138.893)
        converged = self.script.verdict([452.561, 452.16, 454.608, 454.153, 452.394], 500)
        self.assertFalse(self.script.still_warming(converged))
        self.assertAlmostEqual(self.script.warmth_ms(converged), 0.167)
        armed_state = {**warming, "warmth_ms": 138.893, "still_warming": True}
        self.assertIn("still warming", self.script.state_line("after  ANALYZE", armed_state))
        self.assertNotIn("warming", self.script.state_line("plain", warming))

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
        """What the control buys: a number for the part of the drop that was already on its way there.

        The sentence is `describe()`'s only chance to confuse two intervals. `naive_ms` here is the
        after-ANALYZE -> tuned gap (80.7 ms), and his `before` state's best is a *different* 144.3 ms
        away from it, so the sentence must quote the split and no state's own best.
        """
        after_analyze = self.script.verdict([677.12, 671.709, 672.355, 651.033, 532.816], 500)
        tuned = self.script.verdict([452.561, 452.16, 454.608, 454.153, 452.394], 500)
        split = self.script.gain_split(
            naive_ms=after_analyze["best_ms"] - tuned["best_ms"],
            drift_ms=after_analyze["best_ms"] - 456.0,
        )
        self.assertAlmostEqual(
            after_analyze["best_ms"], 532.816, msg="the split's pair, not the before state"
        )
        text = self.script.describe(
            after_analyze,
            tuned,
            state="ANALYZE + SET max_parallel_workers_per_gather=2",
            plans_changed=True,
            split=split,
        )
        self.assertTrue(text.startswith("ordering"), msg=text[:60])
        self.assertIn(f"the {split['naive_ms']:.1f} ms between the untuned state", text)
        self.assertIn("came to within 3.8 ms of the tuned one", text)
        self.assertIn("is not enough to write the ceiling off", text)
        for quoted in (after_analyze["best_ms"], tuned["best_ms"]):
            self.assertNotIn(f"{quoted:.1f}", text, msg="one sentence, one gap")
        self.assertNotIn("conditional", text)

    def test_the_control_arm_runs_without_a_setting_and_invents_no_split(self) -> None:
        """`--control` alone is a legitimate question, and it has no gap to price.

        With no `--set` there is nothing to `RESET` and no condition whose delta could be split, so the
        arm still measures the last state again -- and the witness must not reach for a `gain split:` line
        that would be quoting a `None`.
        """
        self.assertEqual(self.script.reset_lines({}), [])
        head = self.source[self.source.index("def main(") :]
        self.assertLess(
            head.index("if settings:"),
            head.index("if args.control:"),
            msg="the arm is not nested inside the setting branch, so it runs without one",
        )
        self.assertLess(head.index("if split is not None:"), head.index("gain split:"))
        self.assertGreater(head.index("if split is not None:"), head.index("if control is not None:"))

    def test_the_control_is_measured_after_every_state_it_must_follow(self) -> None:
        head = self.source[self.source.index("def main(") :]
        for earlier, later in (
            ("read_tuned, _ = measure_read", "for statement in reset_lines(settings)"),
            ("for statement in reset_lines(settings)", "control_runs = measure("),
        ):
            self.assertLess(head.index(earlier), head.index(later), msg=f"{earlier} then {later}")
        self.assertIn('"control": control', head)
        self.assertIn('"gain_split": split', head)
        self.assertIn('kind_setting in ("credited", "conditional")', head)
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
