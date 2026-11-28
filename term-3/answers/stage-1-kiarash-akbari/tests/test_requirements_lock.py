"""The lock file must pin, with ==, every package the installed CLI imports.

pyproject.toml keeps loose ranges because it describes a library; the delivery guide
asks for exact versions, and requirements.txt is where they live. A runtime dependency
missing from the lock is resolved by pip at install time, which is how stage 1 used to
get whatever SQLAlchemy and psycopg were newest instead of the versions stage 2 tested.
"""

import re
import tomllib
import unittest
from pathlib import Path

STAGE_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT = re.compile(r"^([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(==|>=|<=|~=|>|<)?\s*([^\s;#]*)")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(path: Path) -> dict[str, tuple[str, str]]:
    pins: dict[str, tuple[str, str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = REQUIREMENT.match(line)
        if match:
            pins[_canonical(match.group(1))] = (match.group(3) or "", match.group(4))
    return pins


def runtime_dependencies() -> list[str]:
    data = tomllib.loads((STAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    names = []
    for spec in data["project"]["dependencies"]:
        match = REQUIREMENT.match(spec)
        names.append(_canonical(match.group(1)))
        if match.group(2) and "binary" in match.group(2):
            names.append(_canonical(f"{match.group(1)}-binary"))
    return names


class RequirementsLockTests(unittest.TestCase):
    def setUp(self):
        self.lock = read_lock(STAGE_ROOT / "requirements.txt")

    def test_every_runtime_dependency_is_pinned_exactly(self):
        for name in runtime_dependencies():
            with self.subTest(package=name):
                self.assertIn(name, self.lock, f"{name} is imported at runtime but not locked")
                operator, version = self.lock[name]
                self.assertEqual(operator, "==", f"{name} must be pinned with ==")
                self.assertTrue(version, f"{name} has no version")

    def test_database_drivers_are_in_the_lock(self):
        for name in ("sqlalchemy", "psycopg", "psycopg-binary"):
            with self.subTest(package=name):
                self.assertEqual(self.lock.get(name, ("", ""))[0], "==")

    def test_every_line_of_the_lock_is_an_exact_pin(self):
        for name, (operator, _) in self.lock.items():
            with self.subTest(package=name):
                self.assertEqual(operator, "==", f"{name} is not an exact pin")

    def test_database_drivers_match_the_stage2_lock_when_it_is_present(self):
        stage2 = sorted(STAGE_ROOT.parent.glob("stage-2*/requirements.txt"))
        if len(stage2) != 1:
            self.skipTest("stage-2 is not checked out next to stage-1; cross-stage pin check only")
        other = read_lock(stage2[0])
        for name in ("sqlalchemy", "psycopg", "psycopg-binary"):
            with self.subTest(package=name):
                self.assertEqual(self.lock[name], other[name])


if __name__ == "__main__":
    unittest.main()
