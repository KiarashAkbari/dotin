"""The anti-leak test checklist 10.3 asks for, driven by the audit table and nothing else.

A guard tested against a fixture written in the same file as the test proves only that
the fixture and the test agree. This module therefore reads the real
``docs/evidence/column_audit.csv`` -- the artefact ROADMAP phase 2 produced -- and asks
it what may enter the model. Every expectation below is derived from that file: the
forbidden set is whatever the audit forbids, so a column that starts being forbidden
tomorrow is refused here without this file changing.

Two directions matter and both are asserted:

* green -- the allowed set passes;
* red -- a feature list that adds one column from the audit's own forbidden set raises,
  naming the column, and a column absent from the audit entirely is refused as well.

The red direction is also recorded as a run, in ``docs/evidence/leak-{red,green}-run.txt``
(checklist 10.3 wants the failure witnessed, not promised); the harness that produces
those two files is ``docs/evidence/leak-run-red-green.py``.
"""

from __future__ import annotations

import unittest

import helpers
from capstone.audit import (
    allowed_columns,
    audit_problems,
    columns_from_ddl,
    coverage,
    load_audit,
    require_allowed,
    summarise,
)
from capstone.errors import AuditError

#: the committed audit is the input; nothing in this file re-states a verdict
AUDIT_PATH = helpers.STAGE_ROOT / "docs" / "evidence" / "column_audit.csv"


class CommittedAuditTest(unittest.TestCase):
    """What the tracked audit file is, and that it is whole."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.audit = load_audit(AUDIT_PATH)

    def test_it_covers_every_column_the_schema_defines(self) -> None:
        """ROADMAP phase 2 widened the audit to all 53 columns; this is what catches a
        column added to the DDL and quietly left out of the audit."""
        self.assertEqual(coverage(self.audit, columns_from_ddl(helpers.STAGE2_DDL)), {})
        self.assertEqual(summarise(self.audit)["rows"], 53)

    def test_it_is_self_consistent(self) -> None:
        self.assertEqual(audit_problems(self.audit), [])

    def test_the_label_columns_are_forbidden(self) -> None:
        """The three columns a leak would be most tempting to smuggle in, named here so
        that an edit flipping them to ``allowed`` has to pass this test first."""
        allowed = allowed_columns(self.audit)
        for key in (
            "order_reviews.review_score",
            "orders.order_delivered_customer_date",
            "orders.order_status",
        ):
            self.assertNotIn(key, allowed, f"{key} must not be usable")

    def test_the_decision_moment_itself_is_allowed(self) -> None:
        """``order_approved_at`` is the anchor, not a prediction; forbidding it would make
        every as-of condition unbuildable, so its verdict is pinned too."""
        self.assertIn("orders.order_approved_at", allowed_columns(self.audit))


class LeakGateTest(unittest.TestCase):
    """The behaviour 10.3 names: add an unauthorised column and the run goes red."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.audit = load_audit(AUDIT_PATH)
        cls.allowed = sorted(allowed_columns(cls.audit))
        cls.forbidden = sorted(row.key for row in cls.audit if row.verdict == "forbidden")

    def test_there_is_something_to_refuse(self) -> None:
        """A vacuous red is worse than no red: if the audit ever forbids nothing, this
        test says so instead of letting the gate pass on an empty set."""
        self.assertTrue(self.forbidden)
        self.assertLess(len(self.forbidden), len(self.allowed))

    def test_the_allowed_frame_passes(self) -> None:
        self.assertEqual(require_allowed(self.audit, self.allowed), None)

    def test_each_forbidden_column_is_refused_by_name(self) -> None:
        for key in self.forbidden:
            with self.subTest(column=key):
                with self.assertRaises(AuditError) as caught:
                    require_allowed(self.audit, [*self.allowed[:5], key])
                self.assertIn(key, str(caught.exception))
                self.assertIn("forbidden", str(caught.exception))

    def test_an_unaudited_column_is_refused_too(self) -> None:
        """No row means no column, which is the half of the rule that keeps a new schema
        column out of the model until somebody audits it."""
        with self.assertRaises(AuditError) as caught:
            require_allowed(self.audit, ["orders.order_id", "orders.created_at"])
        self.assertIn("not in the audit at all", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
