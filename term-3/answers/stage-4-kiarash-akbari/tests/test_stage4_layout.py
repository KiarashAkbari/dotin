"""The skeleton itself: what must exist, and what must not have crept in.

Phase -1 asks for a real skeleton rather than zero-byte files, and the brief for this
stage repeats three structural rules: no raw data in Git, no absolute paths, and
English identifiers with Persian prose. Those are the cheapest promises to break by
accident in a four-week build, so they are checked here instead of in a checklist
somebody reads once.

This is deliberately not the anti-leak test (checklist 10.3) or the character-class
test ROADMAP phase 7 asks for; those belong to their phases and would pass vacuously
today.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest

import helpers

STAGE = helpers.STAGE_ROOT
SRC = STAGE / "src"
TESTS = STAGE / "tests"
#: Persian letters and the ZWNJ: prose belongs in Markdown, not in code
PERSIAN = re.compile("[\u0600-\u06ff\u0750-\u077f\u200c]")
REQUIRED_DIRS = (
    "src",
    "tests",
    "sql",
    "scripts",
    "data/raw",
    "data/curated",
    "data/quarantine",
    "docs/evidence",
)
#: the deliverables the brief names, which phase -1 does not have to invent content for
LATER_DOCS = ("report.md", "decisions.md", "manager_summary.md", "requirements.txt")
#: the one tabular file allowed in the tree, by path and by ceiling. The audit has to be a
#: CSV -- `04` asks for a table the test can read, and `time_guard.audit_file` points at
#: it -- so the no-data rule names this path instead of pretending it does not exist. The
#: size ceiling is what keeps the exemption from becoming a data drop: 53 audited columns
#: is a few tens of kilobytes, and a load of `orders.csv` in here is a defect.
TRACKED_TABLE_EXCEPTIONS = {"term-3/answers/stage-4-kiarash-akbari/docs/evidence/column_audit.csv": 65_536}
#: extensions that mean "the data itself", whatever the file is called
DATA_SUFFIXES = (".csv", ".ipynb", ".zip", ".parquet", ".joblib")


def ignored(relative: str) -> bool:
    """Does git's own ignore rule match this path? Checked from the repo root."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", relative],
        cwd=helpers.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


class DirectoryTest(unittest.TestCase):
    def test_every_part_of_the_skeleton_exists(self) -> None:
        for relative in REQUIRED_DIRS:
            with self.subTest(path=relative):
                self.assertTrue((STAGE / relative).is_dir(), f"{relative} is missing")

    def test_the_source_package_is_importable_from_a_clean_interpreter(self) -> None:
        """A stranger's first command is an import; it must not need an install step."""
        code = (
            "import sys; sys.path.insert(0, " + repr(str(SRC)) + "); "
            "import capstone.audit, capstone.cli, capstone.doctor, capstone.predict; "
            "print('ok')"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_placeholders_are_the_only_empty_parts(self) -> None:
        """A .py file that is empty is a lie; one that raises NotReadyError is a plan."""
        offenders = [
            path.relative_to(STAGE).as_posix()
            for path in SRC.rglob("*.py")
            if len(path.read_text(encoding="utf-8").strip()) < 80
        ]
        self.assertEqual(offenders, [])

    def test_the_data_directories_keep_their_gitkeep(self) -> None:
        for relative in ("data/raw", "data/curated", "data/quarantine"):
            with self.subTest(path=relative):
                self.assertTrue((STAGE / relative / ".gitkeep").is_file())

    def test_the_docs_that_come_later_are_not_stubbed_out_as_zero_bytes(self) -> None:
        for name in LATER_DOCS:
            path = STAGE / name
            if path.exists():
                self.assertGreater(path.stat().st_size, 0, f"{name} exists but is empty")


class CodeLanguageTest(unittest.TestCase):
    def test_no_persian_in_the_python_sources(self) -> None:
        offenders = []
        for path in (
            list(SRC.rglob("*.py"))
            + list(TESTS.rglob("*.py"))
            + list((STAGE / "scripts").glob("*.py"))
        ):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if PERSIAN.search(line):
                    offenders.append(f"{path.relative_to(STAGE).as_posix()}:{number}")
        self.assertEqual(offenders, [])

    def test_every_module_starts_with_a_docstring(self) -> None:
        offenders = []
        for path in SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8").lstrip()
            if not text.startswith(('"""', 'r"""')):
                offenders.append(path.relative_to(STAGE).as_posix())
        self.assertEqual(offenders, [])

    def test_docstrings_are_english_sentences(self) -> None:
        """A docstring is code-facing prose, so it follows the code's language rule."""
        for path in SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            match = re.search(r'"""(.*?)"""', text, re.DOTALL)
            if not match:
                continue
            first = match.group(1).strip().splitlines()[0]
            with self.subTest(module=path.name):
                self.assertRegex(first, r"[A-Za-z]")


class NoRawDataTest(unittest.TestCase):
    def test_no_csv_or_notebook_is_tracked_under_stage_4(self) -> None:
        """No raw data in Git, with one named exception, and the exception is checked too.

        A blanket ban would be simpler, but the column audit is required to be a CSV (see
        ``TRACKED_TABLE_EXCEPTIONS``), and a rule a build has to route around is a rule
        nobody follows. So the exemption is explicit, pathed, and capped.
        """
        result = subprocess.run(
            ["git", "ls-files", "term-3/answers/stage-4-kiarash-akbari"],
            cwd=helpers.REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        tracked = [line for line in result.stdout.splitlines() if line.endswith(DATA_SUFFIXES)]
        offenders = [line for line in tracked if line not in TRACKED_TABLE_EXCEPTIONS]
        self.assertEqual(offenders, [])
        for path, ceiling in TRACKED_TABLE_EXCEPTIONS.items():
            with self.subTest(path=path):
                self.assertIn(path, tracked, f"{path} is whitelisted but not tracked")
                size = (helpers.REPO_ROOT / path).stat().st_size
                self.assertLessEqual(
                    size,
                    ceiling,
                    f"{path} is {size} bytes, over the {ceiling} an audit table needs; "
                    "this exemption covers the audit, not the dataset",
                )
                header = (helpers.REPO_ROOT / path).read_text(encoding="utf-8").splitlines()[0]
                self.assertEqual(
                    header,
                    "table,column,available_at,at_approval,source,verdict",
                    "the exempt CSV must be the audit table, header included",
                )

    def test_the_gitignore_carries_the_rules_the_test_relies_on(self) -> None:
        text = (STAGE / ".gitignore").read_text(encoding="utf-8")
        for pattern in (
            "data/raw/*",
            "data/quarantine/*",
            "data/curated/*",
            "*.joblib",
            ".ipynb_checkpoints/",
            "config/capstone.toml",
        ):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, text)

    def test_the_local_config_is_ignored_while_the_contract_is_not(self) -> None:
        """The one-command chain needs a tracked config to copy, so this is load-bearing."""
        self.assertTrue(ignored("term-3/answers/stage-4-kiarash-akbari/config/capstone.toml"))
        self.assertFalse(ignored("term-3/answers/stage-4-kiarash-akbari/config/capstone.example.toml"))
        self.assertFalse(ignored("term-3/answers/stage-4-kiarash-akbari/docs/REQUIREMENTS_CHECKLIST.md"))
        self.assertFalse(ignored("term-3/answers/stage-4-kiarash-akbari/docs/evidence/scaffold-run.txt"))
        self.assertTrue(ignored("term-3/answers/stage-4-kiarash-akbari/data/raw/olist_orders_demo.csv"))
        self.assertTrue(ignored("term-3/answers/stage-4-kiarash-akbari/docs/evidence/local-run.log"))


#: the config keys a student decision has to be written against, whatever value it takes
POLICY_SLOTS = (
    "model.no_approved_at",
    "model.no_review",
    "model.multi_review",
    "split.boundary",
    "external.name",
)
#: ``decide`` / ``choose`` / ``witness``, escaped because Persian belongs in Markdown only
_DECISION_MARK = "\u062a\u0635\u0645\u06cc\u0645"
_CHOICE_MARK = "\u0627\u0646\u062a\u062e\u0627\u0628"
_WITNESS_MARK = "\u0634\u0627\u0647\u062f"


class DecisionsDocumentTest(unittest.TestCase):
    """``decisions.md`` is the tracked owner of the decisions, so it has to be checked.

    The asymmetry is the reason this class exists: ``config/capstone.toml`` is ignored by
    design and never reaches the repository, so a decision "recorded only in the config" is a
    decision nobody else can see. The document is where the reasoning lives, and ``capstone
    doctor`` reads it to decide whether a value was actually written down.
    """

    @staticmethod
    def text() -> str:
        return (STAGE / "decisions.md").read_text(encoding="utf-8")

    def test_every_policy_slot_is_named_in_the_document(self) -> None:
        """Each config key has to appear, so "decided" cannot mean "typed a Persian sentence".

        A value can be legitimate and unfindable at once; naming the key next to it is what
        makes the pair readable by somebody who never opened the config.
        """
        missing = [slot for slot in POLICY_SLOTS if slot not in self.text()]
        self.assertEqual(missing, [])

    def test_every_decision_block_states_a_witness(self) -> None:
        """The rule item 0 states about this file, enforced: no witness, no decision.

        The cheapest way to finish a phase is ten headings with no evidence behind them, so
        "witness" is checked per block rather than once per file.
        """
        offenders = []
        for block in re.split(r"^## ", self.text(), flags=re.MULTILINE)[1:]:
            head, _, body = block.partition("\n")
            marks = any(f"**{mark}:" in body for mark in (_DECISION_MARK, _CHOICE_MARK))
            if marks and f"**{_WITNESS_MARK}:" not in body:
                offenders.append(head.strip())
        self.assertEqual(offenders, [])

    def test_a_recorded_value_appears_verbatim_in_the_document(self) -> None:
        """What ``capstone doctor`` checks, kept as a test so the pair cannot drift apart.

        This is not decoration: the tracked document once carried four decisions in prose while
        none of the five config values appeared in it, and ``doctor`` said so on one ``warn``
        line that nobody ran. Presence is a weak check -- it cannot judge the argument -- but
        its absence is exactly "the config says one thing, the record another".
        """
        local = STAGE / "config" / "capstone.toml"
        if not local.is_file():
            self.skipTest(
                "config/capstone.toml is ignored by design and absent on this machine, so "
                "nothing is decided here and the value-token check did not run"
            )
        from capstone.config import documented_decisions, load_config

        config = load_config(local)
        self.assertEqual(documented_decisions(STAGE / "decisions.md", config), [])


class ReadmeTest(unittest.TestCase):
    """The README is a command list a stranger runs, so the commands must be real."""

    def setUp(self) -> None:
        self.path = STAGE / "README.md"

    def test_the_readme_exists_and_is_long_enough_to_run(self) -> None:
        self.assertTrue(self.path.is_file(), "term-3/answers/stage-4-kiarash-akbari/README.md is missing")
        self.assertGreater(len(self.path.read_text(encoding="utf-8").splitlines()), 40)

    def test_it_documents_the_commands_the_cli_offers(self) -> None:
        text = self.path.read_text(encoding="utf-8")
        for command in ("doctor", "audit", "chain", "predict"):
            with self.subTest(command=command):
                self.assertIn(f"capstone {command}", text)

    def test_it_states_the_item_id_mapping_it_promises(self) -> None:
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("REQUIREMENTS_CHECKLIST.md", text)
        self.assertRegex(text, r"Persian digits|\u06f4\.\u06f1")

    def test_it_names_the_ceiling_without_claiming_a_measurement(self) -> None:
        text = self.path.read_text(encoding="utf-8")
        self.assertIn("45", text)
        self.assertIn("500", text)
        for line in text.splitlines():
            if "minute" in line or "\u062f\u0642\u06cc\u0642\u0647" in line:
                self.assertNotRegex(line, r"\b\d{2}:\d{2}\b", "a measured duration in a README")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
