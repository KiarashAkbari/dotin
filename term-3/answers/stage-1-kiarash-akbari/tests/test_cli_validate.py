import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from data_factory.cli import main
from data_factory.config import load_config
from data_factory.exit_codes import ExitCode
from data_factory.fetch import query_fingerprint
from data_factory.quarantine import VALIDATE_FILENAME
from data_factory.store import RawPageStore
from helpers import make_row

CONFIG = """
[source]
url = "https://example.test/x.json"
agency = "NYPD"
complaint_types = ["Noise - Residential"]
columns = ["unique_key", "created_date"]
start_date = 2025-01-01
end_date = 2025-06-30
page_size = 1000
app_token_env = "TOKEN"
timezone = "America/New_York"

[http]
timeout_seconds = 10
max_retries = 3
backoff_base_seconds = 1.5
backoff_factor = 2.0
retry_statuses = [429, 500, 503]

[paths]
raw_dir = "{raw}"
curated_dir = "{out}"
quarantine_dir = "{quarantine}"
manifest_path = "{manifest}"

[quality]
max_quarantine_ratio = 0.01
min_rows_for_threshold = 10
"""


def build_rows(total, bad=0):
    return [
        make_row(unique_key=str(i), incident_zip="N/A" if i < bad else "10466")
        for i in range(total)
    ]


class ValidateCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.quarantine_dir = root / "q"
        self.config_path = root / "c.toml"
        self.config_path.write_text(
            CONFIG.format(
                raw=(root / "raw").as_posix(),
                out=(root / "out").as_posix(),
                quarantine=self.quarantine_dir.as_posix(),
                manifest=(root / "m.json").as_posix(),
            ),
            encoding="utf-8",
        )
        cfg = load_config(self.config_path)
        self.store = RawPageStore(cfg.paths.raw_dir / query_fingerprint(cfg.source))

    def _run(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["validate", "--config", str(self.config_path)])
        return code, out.getvalue(), err.getvalue()

    def _quarantine_lines(self):
        path = self.quarantine_dir / VALIDATE_FILENAME
        return path.read_text(encoding="utf-8").splitlines()

    def test_clean_data_passes_with_empty_quarantine(self):
        self.store.write_page(0, build_rows(20))
        code, out, _ = self._run()
        self.assertEqual(code, int(ExitCode.OK))
        self.assertIn("quarantined=0", out)
        self.assertEqual(self._quarantine_lines(), [])

    def test_bad_rows_under_threshold_are_quarantined_and_run_passes(self):
        self.store.write_page(0, build_rows(200, bad=1))
        code, out, _ = self._run()
        self.assertEqual(code, int(ExitCode.OK))
        self.assertIn("reasons=BAD_ZIP_FORMAT=1", out)
        self.assertEqual(len(self._quarantine_lines()), 1)

    def test_over_threshold_fails_but_keeps_report_and_quarantine_file(self):
        self.store.write_page(0, build_rows(20, bad=5))
        code, out, _ = self._run()
        self.assertEqual(code, int(ExitCode.DATA))
        self.assertIn("quarantined=5", out)
        self.assertEqual(len(self._quarantine_lines()), 5)

    def test_missing_raw_pages_is_a_data_error(self):
        code, _, err = self._run()
        self.assertEqual(code, int(ExitCode.DATA))
        self.assertIn("run 'fetch' first", err)

    def test_quarantine_file_is_identical_across_runs(self):
        self.store.write_page(0, build_rows(200, bad=1))
        self._run()
        first = (self.quarantine_dir / VALIDATE_FILENAME).read_bytes()
        self._run()
        second = (self.quarantine_dir / VALIDATE_FILENAME).read_bytes()
        self.assertEqual(first, second)