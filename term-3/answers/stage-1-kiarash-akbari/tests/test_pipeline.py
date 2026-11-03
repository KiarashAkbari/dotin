import csv
import tempfile
import unittest
from pathlib import Path

from config_helpers import make_config
from data_factory.errors import DataError, QuarantineThresholdError
from data_factory.manifest import build_manifest, write_manifest
from data_factory.outputs import sha256_file
from data_factory.pipeline import OUTPUT_DATASET, process
from data_factory.population import ZipArea
from helpers import make_row

INDEX = {"10466": ZipArea("10466", 72479)}


def good(key, month="01"):
    return make_row(
        unique_key=key,
        incident_zip="10466",
        created_date=f"2025-{month}-10T10:00:00.000",
        closed_date=f"2025-{month}-10T11:00:00.000",
    )


def bad(key):
    return make_row(unique_key=key, borough="ATLANTIS")


def read_csv(path):
    with Path(path).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


class ProcessTests(unittest.TestCase):
    def test_outputs_counts_and_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            result = process([good("1"), good("2", "02"), bad("3")], cfg, INDEX)
            self.assertEqual((result.report.passed, result.report.quarantined), (2, 1))
            self.assertEqual(len(read_csv(result.output_paths[OUTPUT_DATASET])), 2)
            wide = read_csv(result.output_paths["wide_table"])
            self.assertEqual(len(wide), 1)
            self.assertEqual(
                (wide[0]["count_2025-01"], wide[0]["count_2025-02"]), ("1", "1")
            )
            self.assertEqual(
                wide[0]["rate_per_10k_2025-01"], f"{1 / 72479 * 10_000:.4f}"
            )
            quarantine_text = result.output_paths["quarantine"].read_text("utf-8")
            self.assertIn("VALUE_OUT_OF_DOMAIN", quarantine_text)

    def test_over_threshold_fails_keeps_quarantine_and_publishes_no_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), max_ratio=0.1)
            with self.assertRaises(QuarantineThresholdError):
                process([good("1"), bad("2")], cfg, INDEX)
            self.assertTrue((Path(tmp) / "quarantine" / "quarantine.failed.jsonl").exists())
            self.assertFalse((Path(tmp) / "quarantine" / "quarantine.jsonl").exists())
            self.assertFalse((Path(tmp) / "curated" / "cases.csv").exists())
            self.assertFalse((Path(tmp) / "curated" / "complaints_by_area_month.csv").exists())

    def test_failed_run_leaves_the_last_good_output_set_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), max_ratio=0.1)
            process([good("1"), good("2", "02")], cfg, INDEX)
            paths = [
                Path(tmp) / "curated" / "cases.csv",
                Path(tmp) / "curated" / "complaints_by_area_month.csv",
                Path(tmp) / "quarantine" / "quarantine.jsonl",
            ]
            before = [p.read_bytes() for p in paths]
            with self.assertRaises(QuarantineThresholdError):
                process([good("3"), bad("4")], cfg, INDEX)
            self.assertEqual([p.read_bytes() for p in paths], before)
            failed = Path(tmp) / "quarantine" / "quarantine.failed.jsonl"
            self.assertIn("VALUE_OUT_OF_DOMAIN", failed.read_text("utf-8"))

    def test_successful_run_removes_a_stale_failed_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), max_ratio=0.1)
            failed = Path(tmp) / "quarantine" / "quarantine.failed.jsonl"
            with self.assertRaises(QuarantineThresholdError):
                process([good("1"), bad("2")], cfg, INDEX)
            self.assertTrue(failed.exists())
            process([good("1"), good("2", "02")], cfg, INDEX)
            self.assertFalse(failed.exists())

    def test_empty_input_fails_and_keeps_previous_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            process([good("1")], cfg, INDEX)
            cases = Path(tmp) / "curated" / "cases.csv"
            before = cases.read_bytes()
            with self.assertRaises(DataError):
                process([], cfg, INDEX)
            self.assertEqual(cases.read_bytes(), before)

    def test_two_independent_runs_give_identical_output_hashes(self):
        runs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as tmp:
                cfg = make_config(Path(tmp))
                result = process([good("1"), good("2", "02"), bad("3")], cfg, INDEX)
                runs.append(
                    {name: sha256_file(path) for name, path in result.output_paths.items()}
                )
        self.assertEqual(runs[0], runs[1])


class ManifestTests(unittest.TestCase):
    def test_manifest_holds_counts_window_restriction_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp))
            result = process([good("1"), good("2", "02"), bad("3")], cfg, INDEX)
            manifest = build_manifest(
                cfg=cfg, result=result, requests=3, started_at="s", finished_at="f"
            )
            self.assertEqual(
                manifest["records"],
                {
                    "raw": 3,
                    "passed": 2,
                    "quarantined": 1,
                    "quarantined_by_reason": {"VALUE_OUT_OF_DOMAIN": 1},
                },
            )
            self.assertEqual(manifest["window"]["start_date"], "2025-01-01")
            self.assertEqual(manifest["restriction"]["agency"], "NYPD")
            self.assertEqual(manifest["requests_this_run"], 3)
            self.assertEqual(
                manifest["outputs"]["dataset"]["sha256"],
                sha256_file(result.output_paths[OUTPUT_DATASET]),
            )

    def test_write_manifest_is_atomic_and_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "manifest.json"
            write_manifest(path, {"b": 1, "a": 2})
            self.assertEqual(path.read_text("utf-8"), '{\n  "a": 2,\n  "b": 1\n}\n')
            self.assertFalse(Path(str(path) + ".tmp").exists())