"""The performance probe's verdict: it must judge what the step judges, and say so.

``docs/evidence/frame-performance-run.py`` is the script a reader runs to fill checklist 5.3, and it
labels one of its rows "timed (what capstone features judges)". That sentence is the whole claim: if
the probe compares the *mean* to the ceiling while :func:`capstone.features.build` compares the
fastest repeat, the log is a verdict about a rule the shipping code does not use -- and the two
numbers can straddle the ceiling (a machine whose mean is 510 ms and whose best is 480 ms passes in
the step and fails in the log). Both directions of that disagreement are pinned here, offline,
because the measurement itself needs the student's database and the arithmetic does not.

Nothing in this file re-times anything: it feeds the probe the shapes a timing block produces and
checks which statistic the answer rests on, and that the string naming the basis is the one
``capstone.features`` exports rather than a second copy of its wording.
"""

from __future__ import annotations

import importlib.util
import unittest

import helpers

STAGE = helpers.STAGE_ROOT
PROBE = STAGE / "docs" / "evidence" / "frame-performance-run.py"


def load_probe():
    """Import the script as a module -- it is not on the package path, and it stays that way."""
    spec = importlib.util.spec_from_file_location("frame_performance_run", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CeilingVerdictTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.probe = load_probe()

    def test_the_basis_is_the_step_s_constant_not_a_copy_of_its_prose(self) -> None:
        from capstone.features import TIMING_BASIS

        verdict = self.probe.ceiling_verdict(
            {"min_ms": 410.0, "mean_ms": 420.0, "max_ms": 430.0}, {"mean_ms": 900.0}, 500.0
        )
        self.assertEqual(verdict["basis"], TIMING_BASIS)

    def test_a_mean_over_the_ceiling_does_not_move_the_verdict(self) -> None:
        """The case the old probe got wrong: the step passes, so the log must say it passes.

        It still reports the mean's overshoot, because "passes on the fastest repeat, fails on the
        average" is the sentence a reader needs before copying this to a slower machine -- but that
        is a note beside the verdict, not the verdict.
        """
        verdict = self.probe.ceiling_verdict(
            {"min_ms": 480.0, "mean_ms": 510.0, "max_ms": 560.0}, {"mean_ms": 1200.0}, 500.0
        )
        self.assertIsNone(verdict["over_ms"])
        self.assertEqual(verdict["mean_over_ms"], 10.0)
        self.assertEqual(verdict["judged_ms"], 480.0)
        self.assertEqual(verdict["read_mean_ms"], 1200.0)

    def test_a_slow_repeat_still_refuses_and_reports_the_overshoot(self) -> None:
        verdict = self.probe.ceiling_verdict(
            {"min_ms": 540.0, "mean_ms": 585.0, "max_ms": 640.0}, {"mean_ms": 1300.0}, 500.0
        )
        self.assertEqual(verdict["over_ms"], 40.0)
        self.assertEqual(verdict["mean_over_ms"], 85.0)

    def test_a_clean_run_reports_no_overshoot_on_either_statistic(self) -> None:
        verdict = self.probe.ceiling_verdict(
            {"min_ms": 422.8, "mean_ms": 433.3, "max_ms": 453.9}, {"mean_ms": 1127.1}, 500.0
        )
        self.assertIsNone(verdict["over_ms"])
        self.assertIsNone(verdict["mean_over_ms"])
        # the transfer is reported, never judged: the ceiling is about the query (decisions 16)
        self.assertEqual(verdict["read_mean_ms"], 1127.1)

    def test_the_advice_line_names_the_statistic_it_complains_about(self) -> None:
        """An advice block that says "the mean is X over" under a best-of-repeats rule is a lie."""
        lines = self.probe.advice([], 40.0)
        self.assertTrue(lines, "an overshoot must produce at least the header line")
        self.assertIn("best of the repeats", lines[0])
        self.assertNotIn("the mean is", lines[0])
        self.assertEqual(self.probe.advice([], None), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
