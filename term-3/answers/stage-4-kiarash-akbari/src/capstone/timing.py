"""Per-layer timing for a chain that has one budget, not two.

``stage-3/scripts/run_notebook.py --budget-minutes`` measures a single notebook. The
capstone ceiling is different in kind: 45 minutes for the whole chain from an empty
environment to the final list, so the timer has to know which layer it is inside and
add the layers up. This module is that bookkeeping and nothing else -- it never
estimates a duration, so a missing measurement stays visibly missing.

Two clocks are used on purpose: ``monotonic`` for the elapsed seconds, and the wall
clock for the timestamps a manifest carries. Callers may inject either, which is how
the tests below stay fast and deterministic.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .errors import BudgetError

#: what a finished layer says about itself
STATUSES = ("ok", "failed", "blocked")


@dataclass(frozen=True)
class Layer:
    """One measured step of the chain."""

    name: str
    started_at: str
    finished_at: str
    seconds: float
    status: str
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class ChainTimer:
    """Record each layer once, in order, with a running total against the budget."""

    def __init__(
        self,
        *,
        budget_minutes: int,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if budget_minutes <= 0:
            raise ValueError("budget_minutes must be positive")
        self.budget_minutes = int(budget_minutes)
        self._clock = clock
        self._wall = wall
        self._layers: list[Layer] = []
        self._open: dict[str, tuple[str, float]] = {}

    @property
    def layers(self) -> tuple[Layer, ...]:
        return tuple(self._layers)

    @property
    def seconds_used(self) -> float:
        return round(sum(layer.seconds for layer in self._layers), 3)

    @property
    def budget_seconds(self) -> int:
        return self.budget_minutes * 60

    @property
    def over_budget(self) -> bool:
        return self.seconds_used > self.budget_seconds

    def start(self, name: str) -> None:
        if name in self._open:
            raise BudgetError(f"layer {name!r} is already running; stop it before starting again")
        if any(layer.name == name for layer in self._layers):
            raise BudgetError(f"layer {name!r} was already measured; reruns need a new timer")
        self._open[name] = (self._wall().isoformat(timespec="seconds"), self._clock())

    def stop(
        self,
        name: str,
        *,
        status: str = "ok",
        detail: str = "",
        started_at: str | None = None,
        seconds: float | None = None,
    ) -> Layer:
        """Close a running layer, or record an externally measured one.

        The second form exists because the notebook runner reports its own elapsed
        time: re-timing someone else's measurement would silently double-count the
        same minutes.
        """
        if status not in STATUSES:
            raise BudgetError(f"status must be one of {', '.join(STATUSES)}, got {status!r}")
        if name in self._open:
            opened_at, opened_clock = self._open.pop(name)
            layer = Layer(
                name=name,
                started_at=opened_at,
                finished_at=self._wall().isoformat(timespec="seconds"),
                seconds=round(self._clock() - opened_clock, 3),
                status=status,
                detail=detail,
            )
        elif seconds is not None and started_at:
            layer = Layer(
                name=name,
                started_at=started_at,
                finished_at=started_at,
                seconds=round(float(seconds), 3),
                status=status,
                detail=detail,
            )
        else:
            raise BudgetError(
                f"layer {name!r} was never started; pass seconds= and started_at= to record an "
                "externally measured duration"
            )
        self._layers.append(layer)
        return layer

    @contextmanager
    def layer(self, name: str) -> Iterator[None]:
        """Measure a block, and record ``failed`` with the exception text if it raises."""
        self.start(name)
        try:
            yield
        except Exception as exc:
            self.stop(name, status="failed", detail=f"{type(exc).__name__}: {exc}")
            raise
        self.stop(name, status="ok")

    def total_layer(self) -> Layer:
        """A ``total`` row derived from the layers, never typed by hand."""
        started = min((layer.started_at for layer in self._layers), default="")
        finished = max((layer.finished_at for layer in self._layers), default="")
        return Layer(
            name="total",
            started_at=started,
            finished_at=finished,
            seconds=self.seconds_used,
            # a failed or blocked layer fails the run even inside the budget (review B19)
            status="ok"
            if not self.over_budget and all(layer.status == "ok" for layer in self._layers)
            else "failed",
            detail=f"budget {self.budget_minutes} min",
        )

    def manifest(self) -> dict:
        """The structure ``ROADMAP`` phase 6 asks for: start, end and output per stage."""
        return {
            "budget_minutes": self.budget_minutes,
            "budget_seconds": self.budget_seconds,
            "seconds_used": self.seconds_used,
            "over_budget": self.over_budget,
            "layers": [layer.as_dict() for layer in self._layers],
            "total": self.total_layer().as_dict(),
        }

    def write_manifest(self, path: Path) -> Path:
        """Write the manifest deterministically apart from the timestamps themselves."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(self.manifest(), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        return path

    def over_budget_layers(self) -> list[Layer]:
        """Layers that broke the chain budget on their own -- usually none."""
        return [layer for layer in self._layers if layer.seconds > self.budget_seconds]
