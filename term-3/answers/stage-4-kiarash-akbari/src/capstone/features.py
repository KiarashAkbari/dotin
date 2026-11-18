"""The feature layer: a query in the database, guarded by the audit.

``04`` puts feature construction in the database (a view or a procedure) and caps the
generating query at 500 ms with a stored plan, so the two things this layer has to
prove are *where* the work happened and *what it cost*. Both are cheap to check and
impossible to check after the fact, which is why the checks live here rather than in
the report.

``build`` is a placeholder on purpose: writing the SQL needs the audit file
(checklist 4.1) and the schema of the source tables, and inventing either would put a
guessed column into the delivery. The guards, in contrast, are real and tested.
"""

from __future__ import annotations

from .audit import ColumnVerdict, require_allowed
from .config import CapstoneConfig
from .errors import BudgetError
from .pending import pending


def allowed_only(audit: list[ColumnVerdict], used: list[str]) -> list[str]:
    """Return the used columns, after refusing any the audit does not allow."""
    require_allowed(audit, used)
    return used


def check_query_cost(measured_ms: float, config: CapstoneConfig) -> float:
    """Fail loudly when the feature query broke the ceiling the brief states.

    ``02`` settled the house rule for a number like this: one run and a read-off value
    is not a measurement. The caller passes a mean over repeated runs -- warm and cold
    -- and this function only judges it, so no duration is invented here.
    """
    ceiling = config.features.feature_query_ms
    if measured_ms < 0:
        raise BudgetError(f"a duration cannot be negative: {measured_ms} ms")
    if measured_ms > ceiling:
        raise BudgetError(
            f"the feature query took {measured_ms:.1f} ms on average, over the {ceiling} ms the "
            "brief allows (checklist 5.3). Store the EXPLAIN (ANALYZE) output with the number "
            "and fix the query; raising the ceiling in config is a report finding, not a fix.",
            context={"measured_ms": measured_ms, "ceiling_ms": ceiling},
        )
    return float(measured_ms)


@pending(phase=4, checklist=("5.1", "5.2", "5.3"), step="features")
def build(config: CapstoneConfig, audit: list[ColumnVerdict]) -> dict:
    """Run ``stage-4/sql`` to materialise the feature frame and return its metadata.

    The body belongs to ROADMAP phase 4. What it must satisfy, in order: at least ten
    features from at least six tables, every aggregate carrying an as-of condition in
    its own SQL (5.2), every column present in the audit with a verdict of ``allowed``
    (4.2), and a mean query time under the ceiling in config (5.3).
    """
    raise AssertionError("placeholder")
