"""The notebook is a generated artifact, so its shape is tested, not eyeballed.

The rules this checks are the ones the delivery guide states and a reviewer cannot
verify quickly by reading: one seed, no direct file reads, no logic in cells, Persian
prose, and - the part a generator makes possible - that every ``module.attribute`` a
cell names actually exists in the package. The last check parses the module sources
rather than importing them, so it runs on a machine with no database and no stage-1
install, which is every machine that only regenerates the notebook.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import ClassVar

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[0]
for candidate in (
    HERE,
    ROOT / "src",
    ROOT / "scripts",
    ROOT.parents[1] / "stage-1" / "src",
):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import build_solution_notebook as builder

NOTEBOOK = ROOT / "solution.ipynb"
PLACEHOLDER = re.compile(r"\{\{([a-z0-9_.\[\]]+)\}\}")

#: the only numbers the template may state in prose: constants the brief itself defines - the
#: cost matrix, the 1,000-applicant denominator, the sentinel codes, the ratio pair under
#: sensitivity, file and stage numbers - plus the same values in Persian digits, which \d
#: matches too. A result number does not belong here: it arrives through a placeholder.
DEFINITIONAL_NUMBERS = {
    "0",
    "1",
    "2",
    "3",
    "4",
    "5",
    "12",
    "15",
    "20",
    "25",
    "96",
    "98",
    "010",
    "011",
    "03",
    "1000",
    "1.0",
    "0.5",
    "\u06f0",
    "\u06f1",
    "\u06f2",
    "\u06f3",
    "\u06f4",
    "\u06f5",
    "\u06f6",
    "\u06f7",
    "\u06f8",
    "\u06f9",
    "\u06f0\u06f0\u06f0",
    "\u06f0\u06f1\u06f0",
    "\u06f0\u06f1\u06f1",
    "\u06f2\u06f5",
    "\u06f1\u06f2",
    "\u06f1\u06f5",
    "\u06f2\u06f0",
}

MACHINERY_TITLE = "## اجرای ماشین‌افزار"
READING_TITLE = "## چطور این عددها را بخوانیم"


def machinery_bounds(text: str) -> tuple[int, int]:
    """Where the archived machinery run sits - the report's one bounded exception.

    The section is exempt in both regimes because it quotes *a* run (the fixture execution this
    stage archived), not *the* run that fills the slots. Outside it, a number in prose is a
    number no run will ever correct.
    """
    start = text.index(MACHINERY_TITLE)
    end = text.index(READING_TITLE)
    if start >= end:
        raise AssertionError("the machinery section must sit where the map says")
    return start, end


def report_body(text: str) -> str:
    """The report as the number rules see it: slots and the machinery run removed, structure too.

    A key like ``sensitivity.12_1.threshold`` and the sixth item of a decision list are not
    measurements, and tokenising them is how a check starts shouting about nothing.
    """
    start, end = machinery_bounds(text)
    body = PLACEHOLDER.sub(" ", text[:start] + text[end:])
    return re.sub(r"(?m)^[ \t]*\d+\.[ \t]+", "", body)


#: a call, not a bare name, so a docstring explaining the mistake is not the mistake
OPTIONAL_DEPENDENCY_CALLS = {
    "to_markdown(": "tabulate",
    "to_excel(": "openpyxl",
    "to_parquet(": "pyarrow",
    "read_html(": "lxml",
}

#: the one pair of modules the delivery does not pin, and the one file allowed to use them
TOOLING_UNPINNED = {"nbclient", "nbformat"}
TOOLING_FILES = {"scripts/run_notebook.py"}


def _top_level_imports(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _all_imports(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _shipped_sources():
    for directory in ("src", "scripts"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path, path.read_text(encoding="utf-8")


class OptionalDependencyTests(unittest.TestCase):
    """No cell or module may reach for a pandas extra the lock does not carry.

    ``to_markdown`` is what brought this here: it reads like a pandas method and is an
    import of ``tabulate`` in disguise, so a notebook that passes every local test stops
    three cells into a run on a fresh venv - which is exactly what happened on the first
    real execution. The allowed stack is the delivery guide's, so the fix was to render
    the table here rather than widen what the environment has to install.
    """

    def test_no_optional_pandas_backend_is_called(self):
        sources = list(_shipped_sources())
        sources.append((NOTEBOOK, NOTEBOOK.read_text(encoding="utf-8")))
        for path, text in sources:
            for call, package in OPTIONAL_DEPENDENCY_CALLS.items():
                with self.subTest(file=path.name, call=call):
                    self.assertNotIn(
                        call,
                        text,
                        msg=f"{path.name} calls {call}, which imports {package}",
                    )

    def test_the_replacement_is_wired_in(self):
        tables = (ROOT / "src" / "credit" / "tables.py").read_text(encoding="utf-8")
        self.assertIn("def md_table", tables)
        generator = (ROOT / "scripts" / "build_solution_notebook.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("tables.md_table", generator)
        self.assertIn(
            "from .tables import md_table",
            (ROOT / "src" / "credit" / "measure.py").read_text(encoding="utf-8"),
        )


class ToolingImportTests(unittest.TestCase):
    """The unpinned pair stays a convenience, never a dependency of the delivery.

    An allowance without a boundary becomes the easy way out, so the exemption is bound to
    three facts: only one file may name those modules, it may name them only inside a
    function, and no other shipped source may name them at all. That is what lets a
    machine built from requirements.txt alone deliver every graded thing.
    """

    def test_the_allowance_is_used_and_stays_lazy(self):
        for name in sorted(TOOLING_FILES):
            source = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(file=name):
                self.assertTrue(_all_imports(source) & TOOLING_UNPINNED)
                self.assertTrue(_top_level_imports(source).isdisjoint(TOOLING_UNPINNED))

    def test_no_other_source_names_the_unpinned_pair(self):
        for path, source in _shipped_sources():
            relative = str(path.relative_to(ROOT))
            if relative in TOOLING_FILES:
                continue
            with self.subTest(file=relative):
                self.assertTrue(_all_imports(source).isdisjoint(TOOLING_UNPINNED))


class NotebookShapeTests(unittest.TestCase):
    def test_the_generator_reports_no_problems(self):
        notebook = builder.build_notebook()
        problems = builder.validate(notebook, src_root=ROOT / "src")
        self.assertEqual(problems, [], msg="\n".join(problems))

    def test_cell_ids_are_unique_and_ordered(self):
        notebook = builder.build_notebook()
        ids = [cell["id"] for cell in notebook["cells"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [f"cell-{i:03d}" for i in range(1, len(ids) + 1)])

    def test_markdown_and_code_alternate_in_a_fixed_order(self):
        notebook = builder.build_notebook()
        kinds = [cell["cell_type"] for cell in notebook["cells"]]
        self.assertEqual(
            kinds[0], "markdown", "the notebook must open with its roadmap"
        )
        self.assertEqual(
            kinds[-1], "markdown", "the notebook must close with its limits"
        )
        self.assertGreaterEqual(len(kinds), 30)
        # every section is introduced by markdown before the code that measures it
        for position in range(1, len(kinds) - 1):
            if kinds[position] == "code" and kinds[position - 1] == "code":
                continue  # a section may need more than one cell
            self.assertIn(kinds[position - 1], ("markdown", "code"))

    def test_the_file_on_disk_is_what_the_generator_produces(self):
        self.assertTrue(
            NOTEBOOK.is_file(), "run python scripts/build_solution_notebook.py"
        )
        text = NOTEBOOK.read_text(encoding="utf-8")
        generated = json.loads(builder.serialize(builder.build_notebook()))
        delivered = json.loads(text)
        self.assertEqual(
            builder._cell_sources(delivered),
            builder._cell_sources(generated),
            "a cell edited in the notebook is a cell the generator does not know about",
        )
        if builder._was_run(delivered):
            # an executed deliverable is allowed to differ from the generator only by
            # the things a run produces: outputs, counts, and the kernel's metadata
            self.assertNotEqual(text, builder.serialize(builder.build_notebook()))
            for cell in delivered["cells"]:
                if cell["cell_type"] == "code":
                    self.assertIsNotNone(cell.get("execution_count"))
        else:
            self.assertEqual(text, builder.serialize(builder.build_notebook()))

    def test_a_half_run_notebook_is_a_problem(self):
        """A run that stopped halfway leaves numbers from a source that no longer exists.

        The shape check has to be able to see that, because the notebook still looks
        full of outputs to anyone reading it.
        """
        notebook = json.loads(builder.serialize(builder.build_notebook()))
        code = [cell for cell in notebook["cells"] if cell["cell_type"] == "code"]
        self.assertGreater(len(code), 2)
        for position, cell in enumerate(code[: len(code) // 2], start=1):
            cell["execution_count"] = position
        problems = builder.validate(notebook)
        self.assertTrue(
            any("half-run" in problem for problem in problems),
            f"expected a half-run problem, got {problems}",
        )

    def test_regeneration_is_byte_identical(self):
        first = builder.serialize(builder.build_notebook())
        second = builder.serialize(builder.build_notebook())
        self.assertEqual(first, second)

    def test_no_cell_reads_a_csv_and_the_first_code_cell_seeds_everything(self):
        sources = builder.code_cells(builder.build_notebook())
        for position, source in enumerate(sources, start=1):
            self.assertNotIn("read_csv", source, f"cell {position} reads a file")
            self.assertNotIn("DataFrame.from_csv", source)
        self.assertIn("SEED = 20261103", sources[0])
        self.assertEqual(sum("SEED = " in source for source in sources), 1)

    def test_every_code_cell_parses_and_stays_under_the_line_budget(self):
        for position, source in enumerate(
            builder.code_cells(builder.build_notebook()), start=1
        ):
            tree = ast.parse(source)
            logic = [
                line
                for line in source.splitlines()
                if line.strip() and not line.lstrip().startswith(("import ", "from "))
            ]
            self.assertLessEqual(
                len(logic), builder.MAX_CODE_LINES, f"cell {position} is too long"
            )
            self.assertTrue(
                any(
                    isinstance(node, (ast.Expr, ast.Assign, ast.AnnAssign))
                    for node in tree.body
                ),
                f"cell {position} has no statements",
            )

    def test_markdown_is_persian(self):
        cells = builder.markdown_cells(builder.build_notebook())
        self.assertGreaterEqual(len(cells), 10)
        for position, source in enumerate(cells, start=1):
            self.assertTrue(
                builder.PERSIAN.search(source),
                f"markdown cell {position} is not Persian",
            )

    def test_the_notebook_opens_with_the_leakage_audit_as_its_first_analysis_section(
        self,
    ):
        """The challenge makes the decision-time audit the top of the report.

        The order belongs to the section headings and the table of contents, so those
        are the lines compared. Scanning the whole prose looked reasonable until an
        introduction sentence used one of the same words in passing and the check
        reported an ordering nobody had changed - a structure check must not read
        sentences as structure.
        """
        lines = [
            line.strip()
            for line in "\n".join(
                builder.markdown_cells(builder.build_notebook())
            ).splitlines()
            if line.lstrip().startswith(("## ", "| "))
        ]
        self.assertGreater(
            len(lines), 10, "no headings and no table of contents to order"
        )
        audit_at = next(
            position for position, line in enumerate(lines) if "ممیزی نشتی" in line
        )
        for section in ("ساختار", "گم‌شدگی", "آستانه"):
            later = next(
                position for position, line in enumerate(lines) if section in line
            )
            self.assertLess(audit_at, later, f"{section} precedes the leakage audit")

    def test_the_api_check_actually_resolves_names(self):
        """A check that silently skipped would be a claim about nothing."""
        broken = {
            "cells": [
                {"cell_type": "code", "source": ["print(read.nope())\n"]},
                {"cell_type": "code", "source": ["x = 1\nprint(x)\n"]},
            ]
        }
        problems = builder._check_apis(broken, ROOT / "src")
        self.assertIn("code cell 1: read.nope is not defined in read.py", problems)
        self.assertEqual(len(problems), 1, msg=str(problems))

    def test_dict_keys_in_printed_text_are_not_mistaken_for_api_calls(self):
        source = 'print(json.dumps({"prep": {"fitted_rows": 1}}))'
        self.assertEqual(
            builder.API_PATTERN.findall(builder._outside_strings(source)), []
        )

    def test_builder_cli_validate_only_passes(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "build_solution_notebook.py"),
                "--validate-only",
            ],
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(
            completed.returncode, 0, msg=completed.stdout + completed.stderr
        )
        self.assertIn("code cells", completed.stdout)


class ReportEvidenceTests(unittest.TestCase):
    """The report is authored with slots, and the run is what turns them into numbers."""

    def setUp(self):
        from credit import report

        self.report = report
        self.evidence = {
            "threshold": {
                "value": 0.37,
                "grid_size": 201,
                "ties": 1,
                "cost_per_1000": 3250.0,
            },
            "holdout": {
                "cost_per_1000": 3250.0,
                "reduction_vs_flagging_nobody_pct": 45.833333,
                "rows": 30000,
            },
            "splits": {"train": 90000, "validation": 30000, "holdout": 30000},
            "prep": {"fitted_rows": 120000, "features": 18},
            "load_run": {"rows_loaded": 150000, "rejected_count": 0},
            "queries": {
                "sentinel_spikes": {"rows": 3},
                "missingness_parity": {"rows": 10},
            },
            "cv": {
                "logistic": {"mean_cost_per_1000": 3300.0},
                "boosted_trees": {"mean_cost_per_1000": 3100.0},
            },
            "ablation": {"full_cost": 3300.0, "dropped": 1, "rows": 6},
        }

    def test_placeholders_become_the_measured_value(self):
        filled = self.report.fill_placeholders(
            "cost: {{holdout.cost_per_1000}}, rows: {{splits.train}}.", self.evidence
        )
        self.assertEqual(filled, "cost: 3250, rows: 90,000.")

    def test_an_unknown_slot_stays_visible(self):
        filled = self.report.fill_placeholders("{{does.not.exist}}", self.evidence)
        self.assertIn("missing(does.not.exist)", filled)
        self.assertNotIn("{{", filled)

    def test_a_thousands_separated_number_is_one_token(self):
        self.assertEqual(self.report.numeric_tokens("150,000 rows"), ["150000"])

    def test_invented_numbers_are_caught_and_measured_ones_are_not(self):
        honest = "The threshold is 0.37 and the holdout cost is 3250."
        self.assertEqual(self.report.check_traceability(honest, self.evidence), [])
        invented = "The threshold is 0.37 and the holdout cost is 2900."
        self.assertEqual(
            self.report.check_traceability(invented, self.evidence), ["2900"]
        )

    def test_the_decisions_table_resolves_every_evidence_key_it_names(self):
        """The report's decisions quote evidence keys; a typo there is a dead citation."""
        table = self.report.decisions_table(self.evidence)
        for key in re.findall(r"`([a-z0-9_.\[\]]+)`(?!=)", table):
            self.assertNotIn(key, {"", "-"}, msg=f"unresolved citation: {key}")
        unresolved = [part for part in re.findall(r"`([a-z0-9_.\[\]]+)`(?![=])", table)]
        self.assertEqual(unresolved, [], msg="every citation must resolve to a number")
        self.assertIn("3250", table)

    def test_numbers_flattens_lists_with_indexed_paths(self):
        flat = self.report.numbers(
            {"queries": {"x": {"preview": [{"a": 1}, {"a": 2}]}}}
        )
        self.assertEqual(flat["queries.x.preview[0].a"], 1)
        self.assertEqual(flat["queries.x.preview[1].a"], 2)

    def test_the_persian_prose_has_no_foreign_script_leaks(self):
        """Persian and Latin only, in every shipped text file of this stage.

        Model-written Persian picked up stray CJK tokens twice in one session (a
        filler word inside report.md, one inside this folder's README), and both
        survived review because they look like typos rather than defects. A
        character-class check is cheaper than that review and cannot get bored.
        """
        cjk = ((0x4E00, 0x9FFF), (0x3040, 0x30FF), (0xAC00, 0xD7AF))
        bad = []
        for path in sorted(ROOT.rglob("*")):
            if path.suffix not in {".md", ".py", ".ipynb"} or not path.is_file():
                continue
            if any(part in {"__pycache__", ".venv", "data"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            found = [
                c
                for start, end in cjk
                for c in sorted(set(text))
                if start <= ord(c) <= end
            ]
            if found:
                bad.append((str(path.relative_to(ROOT)), "".join(found)))
        self.assertEqual(bad, [], "foreign-script characters in shipped prose")

    def test_the_shipped_report_has_only_well_formed_slots(self):
        """Slots are dotted keys, and the report may not contain a bare number.

        Together with the notebook's last cell this is the delivery rule "every number
        traces to a notebook output" enforced twice: once before the run (nothing is
        written down) and once after (nothing survived that the evidence lacks). The
        second of those is the one that runs when the working copy has been filled in,
        because a filled report is a different artifact and the no-bare-numbers rule is
        a rule about templates.
        """
        text = (ROOT / "report.md").read_text(encoding="utf-8")
        for slot in PLACEHOLDER.findall(text):
            self.assertRegex(slot, r"^[a-z0-9_.\[\]-]+$")
        # the template may state only the brief's own constants (DEFINITIONAL_NUMBERS) and
        # the archived machinery run; every *result* number has to arrive through a
        # placeholder, and once a run has filled them the exemptions stay while the rule
        # changes shape - see the branch below.
        start, end = machinery_bounds(text)
        body = report_body(text)
        unexpected = sorted(
            set(self.report.numeric_tokens(body)) - DEFINITIONAL_NUMBERS
        )
        if PLACEHOLDER.findall(text):
            self.assertEqual(
                unexpected,
                [],
                "a result number is written in prose instead of a slot",
            )
        else:
            # a run has filled this report, so the promise that describes it is the one the
            # notebook's last cell makes: every number here exists in the evidence that run
            # wrote. the exemptions stay the same two - the brief's own constants, and the
            # archived machinery section - so filling the report cannot turn the suite red
            # over a policy constant, and cannot smuggle an invented number in either.
            # two places hold that payload, in this order: the run's own output, and the copy
            # archived beside the report it produced. the second one exists because committing a
            # filled report commits the numbers without data/ - a fresh clone has no curated file,
            # and a check that only reads the working tree of whoever ran the notebook would call
            # that a broken delivery instead of an archived one.
            evidence_path = ROOT / "data" / "curated" / "credit_evidence.json"
            if not evidence_path.is_file():
                archived = ROOT / "docs" / "evidence" / "real" / "credit_evidence.json"
                if archived.is_file():
                    evidence_path = archived
            if not evidence_path.is_file():
                self.fail(
                    "report.md carries no slots, and neither data/curated/credit_evidence.json "
                    "nor docs/evidence/real/credit_evidence.json is here to trace its numbers to"
                )
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            untraceable = sorted(
                set(self.report.check_traceability(body, evidence))
                - DEFINITIONAL_NUMBERS
            )
            self.assertEqual(
                untraceable,
                [],
                "a number in the filled report is in no evidence payload",
            )
        table = text[start:end]
        for word in ("آزمایشی", "نتیجهٔ چالش", "docs/evidence/fixture/"):
            with self.subTest(label=word):
                self.assertIn(
                    word,
                    table,
                    "the bare numbers are only allowed because the section says what they are",
                )
        self.assertGreater(
            len(text.split()), 400, "the report has a length floor in the guide"
        )


class RuntimeDependencyTests(unittest.TestCase):
    """Every third-party module the shipped runtime imports has to be pinned here.

    An unpinned transitive dependency is the quiet way a delivered environment stops
    existing: the run works on one machine, and a month later the install resolves
    something else. The list is derived rather than typed - imports are read out of the
    sources and out of the notebook cells with ast - so adding an import to a cell fails
    this test until the pin, and the reason for it, are written down.

    Tests are not scanned: they import their own fixtures, which nobody installs.
    """

    ALIASES: ClassVar[dict[str, str]] = {
        "sklearn": "scikit-learn",
        "psycopg_binary": "psycopg-binary",
    }
    LOCAL: ClassVar[set[str]] = {"credit", "data_factory"}
    #: imported inside a function in one tooling script, so the graded path needs neither
    TOOLING: ClassVar[dict[str, set[str]]] = {
        name: TOOLING_UNPINNED for name in TOOLING_FILES
    }

    def _imports(self, source: str) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
        return found

    def _runtime_imports(self) -> set[str]:
        names: set[str] = set()
        for directory in ("src", "scripts"):
            for path in sorted((ROOT / directory).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                relative = str(path.relative_to(ROOT))
                names |= self._imports(
                    path.read_text(encoding="utf-8")
                ) - self.TOOLING.get(relative, set())
        notebook = json.loads((ROOT / "solution.ipynb").read_text(encoding="utf-8"))
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                names |= self._imports("".join(cell["source"]))
        return names

    def _siblings(self) -> set[str]:
        """Names this stage's own tree can satisfy: a test helper is not a dependency.

        ``scripts/make_fixture.py`` imports ``fixtures`` out of ``tests/``, and pinning
        that in requirements.txt would be nonsense. The exemption is a listing of the
        tree rather than a list someone can grow, so it cannot quietly become a way to
        forget a real dependency.
        """
        names = {file.stem for file in (ROOT / "tests").glob("*.py")}
        names |= {child.name for child in (ROOT / "src").iterdir() if child.is_dir()}
        return names

    def test_the_sibling_exemption_only_covers_files_that_exist(self):
        """Otherwise "local module" is just a place to hide an unpinned import."""
        siblings = self._siblings()
        self.assertIn("fixtures", siblings)
        self.assertTrue((ROOT / "tests" / "fixtures.py").is_file())
        for name in ("tabulate", "numpy", "nbclient"):
            with self.subTest(name=name):
                self.assertNotIn(name, siblings)

    def _pinned(self) -> set[str]:
        pins = set()
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for line in text.splitlines():
            if "==" in line and not line.lstrip().startswith("#"):
                pins.add(line.split("==")[0].strip().lower())
        return pins

    def test_every_runtime_import_is_pinned_or_local(self):
        pins = self._pinned()
        missing = []
        for name in sorted(self._runtime_imports()):
            if (
                name in self.LOCAL
                or name in sys.stdlib_module_names
                or name in self._siblings()
            ):
                continue
            if self.ALIASES.get(name, name).lower() not in pins:
                missing.append(name)
        self.assertEqual(
            missing, [], "add an exact pin (and why it is needed) to requirements.txt"
        )

    def test_the_dependencies_scikit_learn_brings_are_written_down(self):
        """These are runtime requirements of the code, not decoration.

        scipy and threadpoolctl arrive through the estimators, narwhals through the
        dataframe interchange layer; an install that does not name them is an install
        that resolves them on its own.
        """
        pins = self._pinned()
        self.assertIn("scikit-learn", pins)
        for dist in ("scipy", "narwhals", "threadpoolctl", "joblib", "numpy"):
            self.assertIn(dist, pins, f"{dist} is required by scikit-learn")


def _installed(dist: str) -> bool:
    from importlib import metadata

    try:
        metadata.version(dist)
    except metadata.PackageNotFoundError:
        return False
    return True


class LockClosureTests(unittest.TestCase):
    """The installed environment has to prove the lock is closed.

    A pinned list is only reproducible if every requirement of every pin is also
    named: otherwise pip resolves the rest on the day someone installs it, and the
    environment the notebook ran in cannot be rebuilt. This reads the metadata of what
    is actually installed, so it is a check on the real thing rather than on my notes -
    and it found cloudpickle, which joblib requires and this file did not name.

    It compares names, not versions: an interpreter with the same packages at other
    versions still answers the question being asked here, which is "is anything missing
    from the lock".
    """

    LOCAL: ClassVar[set[str]] = {"data-factory"}

    def _pinned_names(self) -> set[str]:
        names = set()
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if "==" in line and not line.startswith("#"):
                dist = line.split("==")[0].strip().lower().replace("_", "-")
                names.add(dist)
        return names

    def test_the_lock_names_every_dependency_of_every_pin(self):
        from importlib import metadata

        declared = self._pinned_names()
        # the local editable is checked when it is importable as a distribution and not
        # otherwise: it is a path, so "pip install -e" from another folder is a normal
        # state, and treating it as a missing install would skip the check everywhere it
        # matters
        pins = declared | {dist for dist in self.LOCAL if _installed(dist)}
        absent = [dist for dist in sorted(pins) if not _installed(dist)]
        if absent:
            # The premise of this check is "the lock is installed here". A half-installed
            # interpreter cannot tell us whether the lock is closed - it can only tell us
            # what it is missing - so say which and stop, rather than report a gap in a
            # file that is fine.
            self.skipTest(f"install the lock first, missing: {', '.join(absent)}")
        gaps: list[str] = []
        for dist in sorted(pins):
            requires = metadata.requires(dist) or []
            for line in requires:
                if "extra ==" in line:
                    continue
                spec = line.split(";")[0].strip()
                if not spec:
                    continue
                name = (
                    spec.replace(" ", "")
                    .split(">=")[0]
                    .split("==")[0]
                    .split("<")[0]
                    .split("!")[0]
                    .split("[")[0]
                    .lower()
                    .replace("_", "-")
                )
                if name and name not in pins:
                    gaps.append(f"{dist} requires {name}")
        self.assertEqual(
            gaps,
            [],
            "add the missing names to requirements.txt with the version the env has",
        )

    def test_the_local_editable_dependency_is_written_down(self):
        """`-e ../stage-1` is a dependency too, so the lock has to carry it.

        Without it the notebook imports `data_factory` from wherever PYTHONPATH happens
        to point, and the delivery stops being self-describing.
        """
        text = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("-e ../stage-1", text)


class MeasureTests(unittest.TestCase):
    def setUp(self):
        from credit import measure

        self.measure = measure
        self.sql = ROOT / "sql"

    def test_the_measurement_files_have_the_shape_the_code_expects(self):
        """The statement counts are pinned: adding a query shifts an index silently."""
        counts = {
            name: self.measure.statement_count(self.sql, name)
            for name in self.measure.QUERIES
        }
        self.assertEqual(counts, {"sentinel_spikes": 2, "missingness_parity": 3})

    def test_a_statement_needs_no_semicolon_and_starts_with_sql(self):
        for name in self.measure.QUERIES:
            with self.subTest(name=name):
                sql = self.measure.read_query(self.sql, name)
                self.assertTrue(sql.strip())
                self.assertNotIn(";", sql)
                self.assertFalse(sql.lstrip().startswith("--"))
                self.assertTrue(sql.lstrip().upper().startswith(("WITH", "SELECT")))

    def test_an_out_of_range_statement_names_the_range(self):
        with self.assertRaises(ValueError) as caught:
            self.measure.read_query(self.sql, "sentinel_spikes", statement=9)
        self.assertIn("has 2 statements", str(caught.exception))

    def test_an_unknown_measurement_is_named_with_its_options(self):
        with self.assertRaises(ValueError) as caught:
            self.measure.read_query(self.sql, "made_up")
        self.assertIn("sentinel_spikes", str(caught.exception))

    def test_evidence_file_is_written_atomically_and_deterministically(self):
        import tempfile

        payload = {"b": 1, "a": {"z": 2, "y": [1, 2]}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credit_evidence.json"
            self.measure.write_json(path, payload)
            first = path.read_text(encoding="utf-8")
            self.measure.write_json(path, payload)
            second = path.read_text(encoding="utf-8")
            self.assertEqual(first, second)
            self.assertEqual(
                list(path.parent.iterdir()),
                [path],
                "the temp file must be renamed, not left behind",
            )
            self.assertEqual(self.measure.read_json(path), payload)
            self.assertLess(
                first.index('"a"'),
                first.index('"b"'),
                "keys are sorted so runs diff cleanly",
            )


class CellScopeTests(unittest.TestCase):
    """A notebook is one namespace advanced cell by cell, so order is a real rule.

    The per-cell syntax check cannot see a name that is defined two cells later, and
    that mistake costs a full run to find - which is how the ablation drop landed in a
    cell that ran after the model it was supposed to change was already trained.
    """

    def test_the_real_notebook_reads_only_what_an_earlier_cell_made(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        sources = builder.code_cells(notebook)
        self.assertTrue(sources)
        self.assertEqual(builder._check_scopes(sources), [])

    def test_a_forward_reference_is_reported(self):
        notebook = {
            "cells": [
                {
                    "cell_type": "code",
                    "source": [
                        f"SEED = {builder.SEED}\n",
                        "value = 1\n",
                        "print(value)\n",
                    ],
                },
                {"cell_type": "code", "source": ["print(kept_value)\n"]},
            ]
        }
        problems = builder.validate(notebook, src_root=ROOT / "src")
        self.assertEqual(
            problems,
            ["code cell 2 reads names no earlier cell made: kept_value"],
            "the check must be able to fail, or it proves nothing about ordering",
        )

    def test_a_loop_target_or_import_counts_as_defined(self):
        sources = [
            "import pandas as pd\n",
            "for name in sorted(pd.__all__):\n    print(name)\n",
            "import io\n",
            "with io.StringIO('a') as handle:\n    print(handle)\n",
            "rows = [n for n in range(3)]\nprint(rows)\n",
            "best = min(rows, key=lambda r: r.mean_cost)\nprint(best)\n",
        ]
        self.assertEqual(builder._check_scopes(sources), [])


class ArchivedEvidenceConsistencyTests(unittest.TestCase):
    """The archived files describe one run, and the report describes the same one.

    ``docs/evidence/fixture/`` is the record of an execution, so its two JSONs have to agree about
    the file that was read: the manifest counts the rows in it, the evidence counts what the
    database ended up holding, and the difference between them is the quarantine. A pair that
    contradicts itself is an archive someone edited, which is this stage's oldest failure mode
    seen from the other side - a number written down separately from a run.

    ``docs/evidence/real/`` is the same promise about *your* run. When both files are there the
    shipped report must no longer be a template, and every number in it has to be present in the
    evidence beside it. While the folder is empty this test says so out loud instead of pretending
    to have checked something.

    The rule is containment, not provenance, and it is worth knowing where that stops: the
    checker accepts any legal rounding of any number in the evidence, so a value copied from a
    neighbouring field stays green. On the fixture archive, rewriting the filled threshold as
    0.09 passes - three fields there hold 0.085, and 0.09 is its two-place spelling. Catching
    that would mean re-filling the template slot by slot, which needs the template's history
    rather than the two files in front of the test. A stated limit, not a claim of correctness.
    """

    FIXTURE = ROOT / "docs" / "evidence" / "fixture"
    REAL = ROOT / "docs" / "evidence" / "real"

    def _agree(self, manifest: dict, evidence: dict) -> None:
        """One run, two files: the manifest's claims about the input must be the run's."""
        source = manifest.get("source")
        run = evidence.get("load_run")
        self.assertIsNotNone(source, "the manifest does not say what it read")
        self.assertIsNotNone(run, "the evidence does not say what it read")
        self.assertEqual(
            source["sha256"],
            run["sha256"],
            "the archive's two files read different inputs",
        )
        self.assertEqual(
            source["fingerprint"],
            run["fingerprint"],
            "the page fingerprint and the run's disagree",
        )
        self.assertEqual(
            source["name"],
            run["file_name"],
            "the archived run read a different file name",
        )
        self.assertEqual(
            source["rows_in_file"],
            run["rows_loaded"] + run["rejected_count"],
            "rows in the file must equal rows loaded plus rows quarantined",
        )

    def test_the_fixture_archive_agrees_with_itself(self):
        manifest = json.loads(
            (self.FIXTURE / "credit_run_manifest.json").read_text(encoding="utf-8")
        )
        evidence = json.loads(
            (self.FIXTURE / "credit_evidence.json").read_text(encoding="utf-8")
        )
        self._agree(manifest, evidence)

    @staticmethod
    def _evidence() -> dict | None:
        """The payload the numbers came from: the run's own file, else the archived copy."""
        for name in (
            "data/curated/credit_evidence.json",
            "docs/evidence/real/credit_evidence.json",
        ):
            path = ROOT / name
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        return None

    def test_an_archived_real_run_matches_the_shipped_report(self):
        manifest_path = self.REAL / "credit_run_manifest.json"
        evidence_path = self.REAL / "credit_evidence.json"
        if not (manifest_path.is_file() and evidence_path.is_file()):
            self.skipTest(
                "docs/evidence/real/ holds no archived run yet: nothing to compare the report to"
            )
        self._agree(
            json.loads(manifest_path.read_text(encoding="utf-8")),
            json.loads(evidence_path.read_text(encoding="utf-8")),
        )
        from credit import report

        text = (ROOT / "report.md").read_text(encoding="utf-8")
        slots = PLACEHOLDER.findall(text)
        if slots:
            self.fail(
                "a real run is archived, but report.md still carries "
                f"{len(slots)} slots, e.g. {', '.join(slots[:3])}: fill it from this evidence"
            )
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        untraceable = sorted(
            set(report.check_traceability(report_body(text), evidence))
            - DEFINITIONAL_NUMBERS
        )
        self.assertEqual(
            untraceable,
            [],
            "the archived report states a number the archived evidence does not contain",
        )

    def test_no_report_number_depends_on_the_holdout_sensitivity_row(self):
        """``holdout.matrix_sensitivity`` re-picks its threshold on the holdout rows, so the cost
        in that row is what the best threshold *on those rows* would have cost - a fact about the
        matrix, not an estimate of the policy. The validation-side rows are where threshold
        movement may be quoted. This is the guard that keeps the optimistic number out: drop that
        block from the evidence and no number in the report may become untraceable.
        """
        from credit import report

        evidence = self._evidence()
        if evidence is None:
            self.skipTest("no evidence file: nothing to subtract from")
        text = (ROOT / "report.md").read_text(encoding="utf-8")
        body = report_body(text)
        with_block = report.check_traceability(body, evidence)
        stripped = {
            **evidence,
            "holdout": {
                k: v
                for k, v in evidence["holdout"].items()
                if k != "matrix_sensitivity"
            },
        }
        without_block = report.check_traceability(body, stripped)
        only_there = sorted(set(without_block) - set(with_block) - DEFINITIONAL_NUMBERS)
        self.assertEqual(
            only_there,
            [],
            "the report quotes a number that exists only in the holdout sensitivity row",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
