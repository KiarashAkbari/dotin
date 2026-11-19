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
from .pending import MARKER, is_pending


@dataclass(frozen=True)
class Step:
    """One link of the chain."""

    name: str
    #: the ROADMAP phase that writes it
    phase: int
    #: ``module:function`` inside this package, or a command line for another stage. The function
    #: is called by :func:`run_step` with exactly one positional argument, the loaded config, and
    #: anything else it needs it reads off that config -- the config is the only carrier the runner
    #: and the CLI agree on, and every field of it is validated. A step that needs more than the
    #: config (the model needs the frame, capacity-list needs the scores) says so by having no
    #: adapter in ``cli.STEP_ADAPTERS``, not by taking an argument the runner cannot supply.
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
            # the same lookup run_step uses, so "ready" and "callable" cannot mean two things
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
    # the audit mirror comes first, and it is a step rather than a note in the README: the guard in
    # ``features`` is fail-closed on an empty ``capstone.column_audit``, so a chain that starts from a
    # fresh database would die at its third command on a missing prerequisite instead of on a finding
    Step(
        name="load-audit",
        phase=2,
        entrypoint="capstone.audit:load_from_csv",
        checklist=("4.1", "4.2"),
    ),
    Step(name="features", phase=4, entrypoint="capstone.features:run", checklist=("5.1", "5.3")),
    Step(name="model", phase=5, entrypoint="capstone.model:train", checklist=("6.1", "6.2")),
    Step(name="capacity-list", phase=5, entrypoint="capstone.capacity:report", checklist=("6.3",)),
)


def blocking_phase(step: Step) -> int:
    """Which ROADMAP phase actually has to act before this step can run.

    Usually the step's own phase. Not always: ``capacity-list`` is a phase-5 row whose arithmetic
    is finished, and what is left of it is waiting on the phase-6 decision about where a scored
    frame lives. A refusal that names phase 5 would send the reader to work already done, so the
    placeholder's own phase is trusted over the table when the two disagree.
    """
    if ":" not in step.entrypoint:
        return step.phase
    module_name, _, attribute = step.entrypoint.partition(":")
    try:
        module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 - an unimportable module is its own problem
        return step.phase
    info = getattr(getattr(module, attribute, None), MARKER, None)
    if isinstance(info, dict) and isinstance(info.get("phase"), int):
        return int(info["phase"])
    return step.phase


def blocked_here(step: Step) -> str:
    """The sentence that explains a blocked capstone step, shared by every caller that prints one.

    ``statuses`` and ``require_ready`` used to compose this independently, and drifted at once
    -- one said "is still a placeholder", the other "is scaffolded but empty" -- and neither said
    anything about a step whose blocker belongs to a different phase than the step does.
    """
    phase = blocking_phase(step)
    items = ", ".join(step.checklist)
    # a step whose blocker is in another phase has to say both, in this order: where the work is,
    # then which row of the plan the reader is standing on
    where = f"ROADMAP phase {phase} writes its body"
    if phase != step.phase:
        where += f" (the step's own row is phase {step.phase}, and it is blocked from over there)"
    return f"{step.entrypoint} is still a placeholder: {where}, and the checklist items {items} are open"


def entrypoint_of(step: Step) -> object | None:
    """The function a step names, or ``None`` when the module or the attribute is missing.

    Importing is the whole reason the runner and the CLI get one answer instead of two:
    ``find_spec`` says a package exists, which is not the claim "this function is callable with the
    config".
    """
    if ":" not in step.entrypoint:
        return None
    module_name, _, attribute = step.entrypoint.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    return getattr(module, attribute, None)


def run_step(step: Step, config) -> object:
    """Run one capstone-owned step: ``entrypoint(config)``, and nothing else.

    The single place the call convention lives. It exists because the convention was a fiction --
    ``scripts/run_chain.py`` passed a second positional (an empty argv) that fit no entrypoint: the
    audit load refused with a ``TypeError``, and the frame accepted it as "the audit list is empty",
    which is a wrong answer rather than a crash. A step that needs more than the config is not
    runnable as a chain step, and says so by being absent from :data:`cli.STEP_ADAPTERS`.
    """
    from .errors import ConfigError

    function = entrypoint_of(step)
    if function is None:
        raise ConfigError(
            f"chain step {step.name!r} points at {step.entrypoint!r}, which does not resolve; the "
            "name in CHAIN is the plan, so a typo here is a plan that cannot run",
            context={"step": step.name, "entrypoint": step.entrypoint},
        )
    return function(config)


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
            row["reason"] = blocked_here(step)
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
                f"step {name!r} is scaffolded but empty: {blocked_here(step)}",
                context={"step": step.as_dict()},
            )
    raise NotReadyError(
        f"no chain step called {name!r}; the chain is " + ", ".join(step.name for step in CHAIN)
    )
