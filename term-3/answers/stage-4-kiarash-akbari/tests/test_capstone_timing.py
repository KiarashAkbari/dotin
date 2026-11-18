"""The chain timer: one budget, per-layer numbers, and a manifest that is not hand-typed.

``04`` asks for 45 minutes of wall clock for the whole chain and for each layer's own
time in the manifest. Both are bookkeeping, and bookkeeping is exactly what a test can
pin without a database: the order of layers, the sum, the flag when the sum breaks the
budget, and the fact that a placeholder step is recorded as ``blocked`` rather than as
a zero-length success.

A fake clock keeps these assertions exact -- sleeping to prove a stopwatch works would
only prove the scheduler.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from capstone.errors import BudgetError
from capstone.timing import ChainTimer


class FakeClock:
    """Advance by a fixed amount on every reading, so durations are exact."""

    def __init__(self, step: float = 1.0) -> None:
        self.step = step
        self.now = 0.0

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


class FakeWall:
    """A wall clock that ticks in whole minutes, for readable timestamps."""

    def __init__(self) -> None:
        self.at = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.at
        self.at += timedelta(minutes=1)
        return value


def timer(budget_minutes: int = 45, *, step: float = 1.0) -> ChainTimer:
    return ChainTimer(budget_minutes=budget_minutes, clock=FakeClock(step), wall=FakeWall())


class RecordingTest(unittest.TestCase):
    def test_a_layer_records_seconds_between_start_and_stop(self) -> None:
        chain = timer()
        chain.start("features")
        layer = chain.stop("features")
        self.assertEqual(layer.seconds, 1.0)
        self.assertEqual(layer.status, "ok")
        self.assertEqual(layer.started_at, "2026-11-16T12:00:00+00:00")
        self.assertEqual(chain.seconds_used, 1.0)

    def test_layers_stay_in_the_order_they_ran(self) -> None:
        chain = timer()
        for name in ("ingest", "load", "features", "model"):
            chain.start(name)
            chain.stop(name)
        self.assertEqual(
            [layer.name for layer in chain.layers], ["ingest", "load", "features", "model"]
        )

    def test_starting_a_running_layer_twice_is_refused(self) -> None:
        chain = timer()
        chain.start("features")
        with self.assertRaises(BudgetError):
            chain.start("features")

    def test_measuring_the_same_layer_twice_is_refused(self) -> None:
        chain = timer()
        chain.start("features")
        chain.stop("features")
        with self.assertRaises(BudgetError):
            chain.start("features")

    def test_stopping_something_never_started_needs_its_numbers_named(self) -> None:
        chain = timer()
        with self.assertRaises(BudgetError) as caught:
            chain.stop("features")
        self.assertIn("externally measured", str(caught.exception))
        layer = chain.stop("features", seconds=93.31, started_at="2026-11-16T11:59:00+00:00")
        self.assertEqual(layer.seconds, 93.31)

    def test_an_externally_measured_layer_is_recorded_not_double_timed(self) -> None:
        """The notebook runner reports its own elapsed time; the chain must not re-time it."""
        chain = timer()
        chain.stop("notebook", seconds=593.3, started_at="2026-11-16T10:00:00+00:00")
        self.assertEqual(chain.seconds_used, 593.3)
        self.assertEqual(len(chain.layers), 1)

    def test_an_unknown_status_is_refused(self) -> None:
        chain = timer()
        chain.start("features")
        with self.assertRaises(BudgetError):
            chain.stop("features", status="probably-fine")

    def test_the_context_manager_records_a_failure_with_its_reason(self) -> None:
        chain = timer()
        with self.assertRaises(ValueError), chain.layer("model"):
            raise ValueError("no estimator")
        layer = chain.layers[0]
        self.assertEqual(layer.status, "failed")
        self.assertEqual(layer.detail, "ValueError: no estimator")
        self.assertEqual(chain.seconds_used, layer.seconds)


class BudgetTest(unittest.TestCase):
    def test_the_budget_is_minutes_times_sixty(self) -> None:
        chain = timer(budget_minutes=45)
        self.assertEqual(chain.budget_seconds, 2700)
        self.assertFalse(chain.over_budget)

    def test_crossing_the_budget_is_flagged_not_truncated(self) -> None:
        # a fake clock that advances 40 s per reading, so three layers cost 120 s
        chain2 = timer(budget_minutes=1, step=40.0)
        for name in ("load", "features", "model"):
            chain2.start(name)
            chain2.stop(name)
        self.assertGreater(chain2.seconds_used, 60)
        self.assertTrue(chain2.over_budget)
        self.assertEqual(chain2.total_layer().status, "failed")
        self.assertIn("budget 1 min", chain2.total_layer().detail)

    def test_a_layer_longer_than_the_whole_budget_is_called_out(self) -> None:
        chain = timer(budget_minutes=1, step=0.0)
        chain.stop("load", seconds=500.0, started_at="2026-11-16T10:00:00+00:00")
        self.assertEqual([layer.name for layer in chain.over_budget_layers()], ["load"])

    def test_a_zero_budget_is_a_config_mistake(self) -> None:
        with self.assertRaises(ValueError):
            ChainTimer(budget_minutes=0)


class ManifestTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "run_manifest.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_manifest_carries_start_end_and_status_per_layer(self) -> None:
        chain = timer()
        chain.start("ingest")
        chain.stop("ingest")
        chain.start("features")
        chain.stop("features", status="blocked", detail="phase 4 is not written")
        chain.write_manifest(self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["budget_minutes"], 45)
        self.assertEqual([layer["status"] for layer in data["layers"]], ["ok", "blocked"])
        self.assertEqual(data["layers"][1]["detail"], "phase 4 is not written")
        self.assertEqual(data["total"]["seconds"], data["seconds_used"])
        self.assertFalse(data["over_budget"])

    def test_a_blocked_layer_is_still_timed(self) -> None:
        """A step that refuses takes real time, and the 45-minute figure must include it."""
        chain = timer()
        with self.assertRaises(RuntimeError), chain.layer("model"):
            raise RuntimeError("not ready")
        self.assertEqual(chain.seconds_used, 1.0)

    def test_two_runs_differ_only_in_their_timestamps(self) -> None:
        """Structure, status and durations are a function of the run, not of the writer."""
        first, second = timer(), timer()
        for chain in (first, second):
            chain.start("load")
            chain.stop("load")
            chain.start("model")
            chain.stop("model", status="blocked", detail="phase 5 is not written")

        def without_clock(chain: ChainTimer) -> dict:
            """Drop the wall-clock fields: two runs of one chain do not start together."""
            data = chain.manifest()
            rows = [data["layers"], [data["total"]]]
            for row in rows:
                for layer in row:
                    layer.pop("started_at")
                    layer.pop("finished_at")
            return data

        self.assertEqual(without_clock(first), without_clock(second))
        self.assertEqual(without_clock(first)["layers"][1]["status"], "blocked")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
