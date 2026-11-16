"""Marking the difference between "absent" and "scaffolded but empty".

A skeleton that raises ``NotImplementedError`` and a skeleton that quietly returns
empty results are the same size on disk and opposite in value. The decorator below
keeps the distinction machine-checkable: the function exists, has its real
signature, and refuses loudly -- and :func:`is_pending` lets the chain report
"this step is still a placeholder" without a human editing a status table.

``ROADMAP`` phase -1 asks for a real skeleton, not zero-byte files; this is what
makes that promise testable.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from .errors import NotReadyError

#: attribute set on every placeholder so callers can tell it from real code
MARKER = "capstone_pending"


def pending(*, phase: int, checklist: tuple[str, ...], step: str) -> Callable:
    """Wrap a placeholder body with the reason it refuses.

    ``phase`` is the ROADMAP phase that replaces the body, ``checklist`` names the
    items in ``docs/REQUIREMENTS_CHECKLIST.md`` that stay open because of it, and
    ``step`` names the chain step -- so one error message is enough to route a
    reader from a traceback to the plan.
    """

    def decorate(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            raise NotReadyError(
                f"{func.__module__}.{func.__name__} is a scaffold placeholder: ROADMAP phase "
                f"{phase} writes its body, and it closes checklist items {', '.join(checklist)} "
                f"(chain step {step!r})",
                context={
                    "phase": phase,
                    "checklist": list(checklist),
                    "step": step,
                    "function": f"{func.__module__}.{func.__name__}",
                },
            )

        setattr(wrapper, MARKER, {"phase": phase, "checklist": checklist, "step": step})
        wrapper.__doc__ = (func.__doc__ or "").strip()  # type: ignore[attr-defined]
        return wrapper

    return decorate


def is_pending(func: object) -> bool:
    """Is this callable a scaffold placeholder rather than working code?"""
    return hasattr(func, MARKER)
