"""The chain as data: which command runs in which order, and what still blocks it.

``04`` asks for one command that walks the whole pipeline -- receive, load, features,
model, the 10 % list -- starting from an empty environment. Turning that into a list
of steps buys three things: the doctor can say which step is missing instead of
"not implemented", ``scripts/run_chain.py`` can time each layer against one shared
budget, and the step list itself becomes something a test can pin (``10.2``).

Readiness is *derived*, not declared: a step is ready when the module it points at
exists. Nothing here needs editing when a phase lands, and nothing here can claim a
step works because a checkbox said so.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from importlib.util import find_spec

from .errors import NotReadyError
from .pending import is_pending


@dataclass(frozen=True)
class Step:
    """One link of the chain."""

    name: str
    #: the ROADMAP phase that writes it
    phase: int
    #: module:function inside this package, or a command line for another stage
    entrypoint: str
    #: items in stage-4/docs/REQUIREMENTS_CHECKLIST.md that this step closes
    checklist: tuple[str, ...]
    #: steps owned by an earlier stage are run there, so they are never "not ready" here
    owner: str = "capstone"

    @property
    def module(self) -> str:
        return self.entrypoint.split(":")[0]

    def implemented(self) -> bool:
        """Does the code this step points at exist, and is it more than a placeholder?"""
        if self.owner != "capstone":
            return find_spec(self.module.split(".")[0]) is not None
        if ":" not in self.entrypoint:
            return find_spec(self.module) is not None
        module_name, _, attribute = self.entrypoint.partition(":")
        try:
            module = importlib.import_module(module_name)
        # broad on purpose: a step counts as ready only if importing it *and* reading
        # the attribute both succeed, and the failure that matters here is the one the
        # module raises in its own top-level code (a missing config, a bad path)
        except Exception:  # pragma: no cover - a broken module is not a ready step
            return False
        function = getattr(module, attribute, None)
        return callable(function) and not is_pending(function)

    def importable(self) -> bool:
        """For a foreign step: is the other stage's package on this interpreter's path?"""
        return find_spec(self.module.split(".")[0]) is not None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "phase": self.phase,
            "entrypoint": self.entrypoint,
            "owner": self.owner,
            "checklist": list(self.checklist),
            "ready": self.implemented(),
        }


#: the order the brief describes; the prediction entry point is deliberately absent
CHAIN: tuple[Step, ...] = (
    Step(
        name="ingest",
        phase=1,
        entrypoint="data_factory.cli",
        checklist=("11.1", "3.9"),
        owner="stage-1",
    ),
    Step(
        name="load-database",
        phase=1,
        entrypoint="data_factory.cli",
        checklist=("11.1", "11.4"),
        owner="stage-1",
    ),
    Step(name="features", phase=4, entrypoint="capstone.features:build", checklist=("5.1", "5.3")),
    Step(name="model", phase=5, entrypoint="capstone.model:train", checklist=("6.1", "6.2")),
    Step(name="capacity-list", phase=5, entrypoint="capstone.capacity:report", checklist=("6.3",)),
)


def statuses() -> list[dict]:
    """One row per step, with the reason a blocked step is blocked."""
    rows: list[dict] = []
    for step in CHAIN:
        row = step.as_dict()
        if row["ready"]:
            row["reason"] = ""
        elif step.owner != "capstone":
            row["reason"] = (
                f"{step.module} is not importable from this interpreter; stage 1 owns it, and "
                "README.md documents the path that makes it one"
            )
        else:
            row["reason"] = (
                f"{step.entrypoint} is still a placeholder: ROADMAP phase {step.phase} writes "
                f"its body, and the checklist items {', '.join(step.checklist)} are open"
            )
        rows.append(row)
    return rows


def first_blocked() -> Step | None:
    for step in CHAIN:
        if not step.implemented():
            return step
    return None


def require_ready(name: str) -> Step:
    """Return the step if it can run, and refuse with the phase that owns it if not."""
    for step in CHAIN:
        if step.name == name:
            if step.implemented():
                return step
            raise NotReadyError(
                f"step {name!r} is scaffolded but empty: it belongs to ROADMAP phase "
                f"{step.phase}, and closes checklist items {', '.join(step.checklist)}",
                context={"step": step.as_dict()},
            )
    raise NotReadyError(
        f"no chain step called {name!r}; the chain is " + ", ".join(step.name for step in CHAIN)
    )
