"""End to end through main(): a full run, then --since-last-run increments.

The source is an in-memory table that grows between runs; `_today` is patched so the
increments' window is still open. The sequence reproduces the three rolling defects:

* a second increment on the same day missed cases created after the first one
  (the raw cache of an open window was trusted as complete);
* an increment with nothing new exited 65 instead of succeeding with zero rows;
* every increment overwrote curated/rolling/ and its manifest, so earlier
  increments' rows were lost.
"""

import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from config_helpers import write_config_toml
from helpers import make_row

from data_factory import cli
from data_factory.exit_codes import ExitCode
from data_factory.fetch import cursor_of
from data_factory.rolling import rolling_manifest_path

FULL_RUN_DAY = date(2025, 4, 5)
TODAY = date(2025, 4, 5)


def case(key: str, created: str) -> dict:
    hour = int(created[11:13])
    closed = f"{created[:11]}{hour + 1:02d}{created[13:]}"
    return make_row(unique_key=key, created_date=created, closed_date=closed)


class TableSource:
    """Keyset source over a list the test mutates between runs."""

    def __init__(self, rows, cfg):
        self.rows, self.cfg = rows, cfg
        self.page_size = cfg.page_size

    def cursor_of(self, row):
        return cursor_of(row)

    def fetch_after(self, cursor):
        start = self.cfg.start_date.isoformat()
        end = (self.cfg.end_date + timedelta(days=1)).isoformat()
        rows = sorted(
            (r for r in self.rows if start <= r["created_date"][:10] < end), key=cursor_of
        )
        if cursor is not None:
            rows = [r for r in rows if cursor_of(r) > cursor]
        return [{k: r[k] for k in self.cfg.columns if k in r} for r in rows[: self.page_size]]


class RollingCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.config = write_config_toml(root / "c.toml")
        text = self.config.read_text(encoding="utf-8").replace(
            'columns = ["unique_key", "created_date"]',
            "columns = [\"unique_key\", \"created_date\", \"closed_date\", \"agency\", "
            "\"complaint_type\", \"descriptor\", \"status\", \"borough\", \"incident_zip\", "
            "\"community_board\"]",
        )
        self.config.write_text(text, encoding="utf-8")
        (root / "ref.json").write_text(
            json.dumps([{"modzcta": "10466", "label": "10466", "pop_est": "70000"}]),
            encoding="utf-8",
        )
        self.curated = root / "curated"
        self.manifest = root / "manifest.json"
        self.table = [case(str(i), f"2025-03-0{i}T10:00:00.000") for i in range(1, 6)]

    def _main(self, *extra, today=TODAY):
        source = lambda client, cfg: TableSource(self.table, cfg)  # noqa: E731
        err = io.StringIO()
        with (
            mock.patch.object(cli.SocrataSource, "from_env", side_effect=source),
            mock.patch.object(cli, "_today", return_value=today),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(err),
        ):
            code = cli.main(["run", "--config", str(self.config), *extra])
        return code, err.getvalue()

    def _increments(self):
        found = {}
        for path in sorted((self.curated / "rolling").glob("*/run_manifest.json")):
            manifest = json.loads(path.read_text(encoding="utf-8"))
            with (path.parent / "cases.csv").open(encoding="utf-8", newline="") as fh:
                keys = sorted(row["unique_key"] for row in csv.DictReader(fh))
            found[path.parent.name] = (manifest, keys)
        return found

    def test_increments_are_complete_kept_apart_and_may_be_empty(self):
        code, err = self._main(today=FULL_RUN_DAY)
        self.assertEqual(code, int(ExitCode.OK), err)

        self.table += [case("6", "2025-04-01T10:00:00.000"), case("7", "2025-04-02T10:00:00.000")]
        code, err = self._main("--since-last-run")
        self.assertEqual(code, int(ExitCode.OK), err)

        code, err = self._main("--since-last-run")
        self.assertEqual(code, int(ExitCode.OK), f"an empty increment must succeed: {err}")

        self.table.append(case("8", "2025-04-03T10:00:00.000"))
        code, err = self._main("--since-last-run")
        self.assertEqual(code, int(ExitCode.OK), err)

        increments = sorted(
            self._increments().values(), key=lambda item: item[0]["started_at"] + str(item[1])
        )
        self.assertEqual(len(increments), 3, "each increment keeps its own folder")
        keys = [k for _, k in increments]
        self.assertIn(["6", "7"], keys, "the first increment survived the later ones")
        self.assertIn([], keys, "the empty increment published zero rows")
        self.assertIn(["8"], keys, "a same-day rerun saw the case created after the first")
        for manifest, rows in increments:
            self.assertEqual(manifest["records"]["raw"], len(rows))

        latest = json.loads(rolling_manifest_path(self.manifest).read_text(encoding="utf-8"))
        self.assertEqual(latest["watermark"]["max_created_at"], "2025-04-03T10:00:00")
        full = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(full["records"]["raw"], 5, "increments never touch the full run")

    def test_rolling_without_a_previous_run_is_a_data_error(self):
        code, err = self._main("--since-last-run")
        self.assertEqual(code, int(ExitCode.DATA), err)


if __name__ == "__main__":
    unittest.main()
