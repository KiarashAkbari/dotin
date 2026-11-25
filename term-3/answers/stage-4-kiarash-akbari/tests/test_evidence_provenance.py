"""Provenance of the evidence scripts: say what you read, and refuse a witness from another split.

Four rules, each of them earned on a real run rather than guessed offline:

* ``sql/006_external_bridge.sql`` guards the stored period *before* it swaps ``v_external_feature``,
  because ``CREATE OR REPLACE VIEW`` cannot rename a column: on a database that holds the previous
  period, the swap raised Postgres's ``cannot change name of view column`` after the materialized view
  had already been refreshed -- half-applied, and with the file's own sentence never read. The order is
  a property of the text, so the text is pinned here (the live form is the re-apply test, which needs
  the database).
* ``scripts/cheating_model_gap.py`` refuses to overwrite an evidence file whose header names a
  different ``[split].boundary``, because ``default_config_path`` prefers a local
  ``config/capstone.toml`` and a gap computed on another split silently replaces a tracked witness.
* ``scripts/sealed_cut_rehearsal.py`` answers a checkout without the raw CSVs with a sentence naming
  both ``--olist`` and ``CAPSTONE_OLIST_DIR``, not a traceback out of ``pandas``.
* ``advice()`` in the performance probe does not recommend ``work_mem`` to a plan whose own counters
  say nothing spilled, and does not repeat one sentence once per hot node.
"""

from __future__ import annotations

import importlib.util
import io
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import helpers

STAGE = helpers.STAGE_ROOT
SQL_DIR = STAGE / "sql" / "006_external_bridge.sql"
SQL_README = STAGE / "sql" / "README.md"


def load_script(name: str):
    path = STAGE / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_probe():
    path = STAGE / "docs" / "evidence" / "frame-performance-run.py"
    spec = importlib.util.spec_from_file_location("frame_performance_run", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class BridgeFrozenTest(unittest.TestCase):
    """The stale-database failure is answered with a procedure, not by editing a shipped migration.

    The tempting fix was to move ``006``'s period guard above its ``CREATE OR REPLACE VIEW`` so the
    reader gets the project's sentence instead of Postgres's. It is not available: ``006`` is under
    ``docs/evidence/sql-checksums.txt``, and a migration that has already run on someone's database
    must not change underneath them -- that is the same lie ``006`` itself refuses about a column
    name. So the fix is the two resolutions written where a reader looks before applying, pinned here.
    """

    def test_the_order_of_the_frozen_migration_is_left_alone(self) -> None:
        text = SQL_DIR.read_text(encoding="utf-8")
        self.assertLess(
            text.index("CREATE OR REPLACE VIEW capstone.v_external_feature"),
            text.index("DO $bridge$"),
            "006 is checksummed; reordering it is editing a delivered migration, not a review fix",
        )

    def test_the_procedure_for_a_stale_database_is_written_down(self) -> None:
        note = SQL_README.read_text(encoding="utf-8")
        self.assertIn("sql-checksums.txt", note)
        self.assertIn("cannot change name of view column", note)
        self.assertIn("delete from capstone.external_municipio", note)
        self.assertIn("RENAME COLUMN", note, "the tempting wrong workaround has to be named to be refused")

    def test_the_checksum_manifest_covers_only_sql(self) -> None:
        """If that ever changes, the note above has to move -- and this test says so out loud."""
        source = (STAGE / "scripts" / "write_sql_checksums.py").read_text(encoding="utf-8")
        self.assertIn('rglob("*.sql")', source)


class GapWitnessProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gap = load_script("cheating_model_gap")

    def run_main(self, *args: str) -> tuple[object, str]:
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer):
                code = self.gap.main(list(args))
        except Exception as error:  # a refusal that got past the guard: not what exit 2 is for
            return None, f"{type(error).__name__}: {error}\n{buffer.getvalue()}"
        return code, buffer.getvalue()

    def test_a_witness_from_another_boundary_is_not_overwritten_in_silence(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            evidence = Path(tmp) / "cheating-model-gap.txt"
            evidence.write_text(
                "# cheating-model gap\nmodel family: hgb.d3; split boundary 2018-07-31; quota 10%\n",
                encoding="utf-8",
            )
            code, out = self.run_main(
                "--config",
                str(STAGE / "config" / "capstone.default.toml"),
                "--evidence",
                str(evidence),
            )
        self.assertEqual(code, 2, out)
        self.assertIn("refusing to overwrite", out)
        self.assertIn("2018-07-31", out)
        self.assertIn("capstone.default.toml", out, "the reader must see which config was in play")

    def test_the_refusal_comes_before_any_work_is_booked(self) -> None:
        """The order inside ``main`` is the point: a wrong invocation costs no queries.

        ``load_config`` is the only thing before the refusal, so the file is never opened, no metrics
        JSON is read and no connection is attempted -- on a machine whose database is the shared one,
        that is the difference between a typo and a wasted minute of somebody else's cluster.
        """
        source = (STAGE / "scripts" / "cheating_model_gap.py").read_text(encoding="utf-8")
        refusal = source.index("An evidence file is a claim about a split")
        self.assertLess(refusal, source.index("curated_dir / system.METRICS_NAME"))
        self.assertLess(refusal, source.index('connect(database_uri(config)'))

    def test_the_header_of_a_new_witness_names_the_config_it_read(self) -> None:
        source = (STAGE / "scripts" / "cheating_model_gap.py").read_text(encoding="utf-8")
        self.assertIn("(config {args.config.name})", source)

    def test_force_is_the_only_way_past_and_is_a_flag_not_a_default(self) -> None:
        source = (STAGE / "scripts" / "cheating_model_gap.py").read_text(encoding="utf-8")
        self.assertIn('"--force"', source)
        self.assertIn("and not args.force", source)


class SealedCutRefusalTest(unittest.TestCase):
    def test_a_missing_corpus_answers_with_a_sentence_not_a_traceback(self) -> None:
        import tempfile

        script = load_script("sealed_cut_rehearsal")
        with tempfile.TemporaryDirectory() as tmp:
            code, out = "", ""
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = script.main(["--olist", str(Path(tmp) / "nowhere"), "--month", "2018-08"])
            out = buffer.getvalue()
        self.assertEqual(code, 0, out)
        self.assertIn("not carved", out)
        self.assertIn("olist_orders_dataset.csv", out)
        self.assertIn("CAPSTONE_OLIST_DIR", out)

    def test_the_environment_variable_the_chain_honours_is_honoured_here_too(self) -> None:
        source = (STAGE / "scripts" / "sealed_cut_rehearsal.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("CAPSTONE_OLIST_DIR")', source)
        from capstone.ingest import OLIST_DIR_ENV

        self.assertIn(OLIST_DIR_ENV, source, "one name for one idea, or the two paths drift apart")


class AdviceContradictionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.probe = load_probe()

    def test_no_work_mem_lecture_when_the_plan_never_left_ram(self) -> None:
        hot = [{"node": "Hash Join", "relation": None}, {"node": "Hash Join", "relation": None}]
        dry = {"temp_read": 0, "temp_written": 0, "batches": 1, "spilling": []}
        lines = self.probe.advice(hot, 92.9, dry)
        self.assertEqual(len(lines), 2, lines)
        self.assertNotIn("work_mem", " ".join(lines[1]))
        self.assertIn("no spill anywhere in the plan", lines[1])

    def test_identical_advice_is_printed_once(self) -> None:
        wet = {"temp_read": 900, "temp_written": 1200, "batches": 5, "spilling": ["Hash Join"]}
        hot = [{"node": "Hash Join", "relation": None}] * 3
        lines = self.probe.advice(hot, 92.9, wet)
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("work_mem", lines[1], "a real spill still earns the memory advice")

    def test_a_passing_query_still_gets_no_advice_at_all(self) -> None:
        self.assertEqual(self.probe.advice([], None, {"temp_read": 0, "temp_written": 0}), [])


class AblationReadingTest(unittest.TestCase):
    """The summary line has to be read *from* the table, not written beside it.

    The witness this replaces explained what an interval covering 0 would mean, under a table where
    three families covered it and one did not -- so the file's own conclusion contradicted its own
    numbers, and a mixed result read as "no effect".
    """

    def setUp(self) -> None:
        path = STAGE / "docs" / "evidence" / "model-ablation-run.py"
        spec = importlib.util.spec_from_file_location("model_ablation_run", path)
        self.script = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(self.script)

    def test_a_mixed_table_names_the_family_that_excludes_zero(self) -> None:
        rows = [
            {"name": "logreg.c0_1", "delta": 0.0044, "ci": {"low": -0.0014, "high": 0.0121}},
            {"name": "hgb.d6", "delta": 0.0088, "ci": {"low": 0.0010, "high": 0.0186}},
        ]
        text = self.script.reading(rows)
        self.assertIn("1 of 2", text)
        self.assertIn("hgb.d6", text)
        self.assertIn("logreg.c0_1", text, "the family left out is named, not counted silently")
        self.assertEqual(text.count("logreg.c0_1"), 1)
        self.assertIn("raises", text)
        self.assertIn("inconclusive", text)

    def test_an_all_inconclusive_table_says_nothing_moved(self) -> None:
        rows = [{"name": n, "delta": d, "ci": {"low": -0.002, "high": 0.01}} for n, d in (("a", 0.004), ("b", -0.001))]
        text = self.script.reading(rows)
        self.assertIn("no family moved measurably", text)
        self.assertNotIn("1 of 2", text)

    def test_a_missing_interval_is_treated_as_inconclusive_not_as_zero(self) -> None:
        rows = [{"name": "hgb.d3", "delta": 0.01, "ci": None}]
        text = self.script.reading(rows)
        self.assertIn("no family moved measurably", text, "a bootstrap that did not run is not evidence")

    def test_the_script_uses_the_function_it_does_not_keep_a_second_sentence(self) -> None:
        """The function alone is not the fix; the call site is what the witness prints.

        A test that only exercises ``reading()`` stays green while the file prints last round's
        boilerplate, which is the failure mode this whole patch is about.
        """
        source = (STAGE / "docs" / "evidence" / "model-ablation-run.py").read_text(encoding="utf-8")
        self.assertIn("print(reading(measured))", source)
        self.assertIn("measured.append(", source)
        self.assertNotIn("an interval that contains 0 means the external signal", source)

    def test_coverage_is_not_allowed_to_sound_like_a_result(self) -> None:
        text = self.script.reading([{"name": "x", "delta": 0.01, "ci": {"low": 0.001, "high": 0.02}}])
        self.assertIn("only the right to ask the question", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
