import pathlib
q = pathlib.Path("tests/test_ceiling_probe.py"); s = q.read_text(encoding="utf-8")
reps = [
('self.assertIn("code, note = exit_note(before, after, kind_setting, control)", head)',
 'self.assertIn("code, note = exit_note(before, after, kind_setting, control, tuned)", head)'),
('        self.assertIn("--repeats 15", note, msg="a refusal has to end in something to type")',
 '        self.assertIn(\n            "take more repeats on the same command", note, msg="a refusal has to end in something to type"\n        )\n        self.assertNotIn("--repeats 15", note, msg="15 was the count this very run used; a fixed number ages badly")'),
('        tight = v([452.561, 452.16, 454.608, 454.153, 452.394], 500)\n        self.assertEqual(self.script.exit_note(tight, after, "refuted", None), (0, ""))',
 '        tight = v([452.561, 452.16, 454.608, 454.153, 452.394], 500)\n        self.assertEqual(self.script.exit_note(tight, after, "refuted", None), (0, ""))\n'
 '        # a state outside the rule\'s reach used to be invisible: `0042` looked only at `before`/`after`\n'
 '        self.assertEqual(self.script.exit_note(before, after, "refuted", None, tight)[0], 0)\n'
 '        self.assertIn("the tuned state met", self.script.exit_note(before, after, "refuted", None, tight)[1])'),
]
for a, b in reps:
    n = s.count(a)
    print(("OK  " if n == 1 else "MISS"), n, a[:52].replace("\n", "\\n"))
    if n != 1:
        raise SystemExit(1)
    s = s.replace(a, b)

anchor = "    def test_the_plan_comparison_ignores_the_clock(self) -> None:"
assert s.count(anchor) == 1
new = '''    def test_a_priced_pair_is_not_answered_by_the_untuned_state(self) -> None:
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
        self.assertFalse(self.script.straddles_ceiling(tuned), msg="15/15 inside 11.5 ms of band")
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
        self.assertIn("note: the tuned state, and the control met 500 ms outside its own interval", note)
        self.assertIn("this is not a fix *by the setting*", note)
        self.assertEqual(self.script.exit_note(before, after, "noise", None, None)[0], 1)

'''
s = s.replace(anchor, new + anchor)
q.write_text(s, encoding="utf-8")
print("tests updated")
