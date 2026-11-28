"""The output set is published together, and a half-published set is detectable.

Before, each file was replaced on its own as its writer finished; a kill between two
replaces left a new cases.csv next to an old wide table and an old manifest.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config_helpers import make_config
from data_factory import publish
from data_factory.errors import DataError
from data_factory.manifest import build_manifest, write_manifest
from data_factory.outputs import sha256_file
from data_factory.pipeline import OUTPUT_DATASET, process
from data_factory.population import ZipArea
from helpers import make_row

INDEX = {"10466": ZipArea("10466", 72479)}


def good(key, day="01"):
    return make_row(unique_key=key, created_date=f"2025-{day}-03T00:57:07.000",
                    closed_date=f"2025-{day}-03T01:30:00.000")


def full_run(cfg, rows):
    """What cli._build does: stage, hash, publish, then write the manifest."""
    result = process(rows, cfg, INDEX, publish=False)
    manifest = build_manifest(cfg=cfg, result=result, requests=0, started_at="s", finished_at="f")
    result = result.publish()
    write_manifest(cfg.paths.manifest_path, manifest)
    return result, manifest


class PublishTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cfg = make_config(self.root)

    def _curated(self):
        return sorted(p.name for p in self.cfg.paths.curated_dir.iterdir())

    def test_nothing_is_visible_until_the_set_is_published(self):
        result = process([good("1")], self.cfg, INDEX, publish=False)
        self.assertFalse(result.output_paths[OUTPUT_DATASET].exists())
        self.assertTrue(result.readable_path(OUTPUT_DATASET).exists())
        result.publish()
        self.assertTrue(result.output_paths[OUTPUT_DATASET].exists())
        self.assertNotIn(publish.STAGING_DIR, self._curated())

    def test_manifest_hashes_match_the_published_files(self):
        result, manifest = full_run(self.cfg, [good("1"), good("2", "02")])
        for name, path in result.output_paths.items():
            self.assertEqual(manifest["outputs"][name]["sha256"], sha256_file(path))

    def test_a_kill_before_the_renames_keeps_the_previous_set(self):
        full_run(self.cfg, [good("1")])
        before = {p: p.read_bytes() for p in self.cfg.paths.curated_dir.glob("*.*")}
        process([good("1"), good("2", "02")], self.cfg, INDEX, publish=False)  # killed here
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        # the next run clears what the killed one staged
        full_run(self.cfg, [good("1")])
        self.assertNotIn(publish.STAGING_DIR, self._curated())

    def test_a_kill_during_the_renames_is_reported_by_verify_outputs(self):
        _, manifest = full_run(self.cfg, [good("1")])
        files = {name: self.root / entry["path"] for name, entry in manifest["outputs"].items()}
        publish.verify_outputs(self.cfg.paths.manifest_path, self.cfg.root, files)

        result = process([good("1"), good("2", "02")], self.cfg, INDEX, publish=False)
        real_replace = os.replace
        calls = {"n": 0}

        def die_after_two(src, dst):
            calls["n"] += 1
            if calls["n"] > 2:
                raise KeyboardInterrupt("killed mid-publish")
            real_replace(src, dst)

        with mock.patch.object(publish.os, "replace", side_effect=die_after_two):
            with self.assertRaises(KeyboardInterrupt):
                result.publish()
        with self.assertRaises(DataError) as caught:
            publish.verify_outputs(self.cfg.paths.manifest_path, self.cfg.root, files)
        self.assertIn("interrupted", str(caught.exception))

    def test_verify_outputs_ignores_a_missing_manifest_and_other_files(self):
        publish.verify_outputs(self.root / "absent.json", self.root, {"dataset": self.root / "x"})
        self.cfg.paths.manifest_path.write_text(
            json.dumps({"outputs": {"dataset": {"path": "elsewhere.csv", "sha256": "0"}}}), "utf-8"
        )
        other = self.root / "cases.csv"
        other.write_text("x", "utf-8")
        publish.verify_outputs(self.cfg.paths.manifest_path, self.root, {"dataset": other})

    def test_the_database_sink_refuses_a_half_published_set(self):
        import dataclasses

        from data_factory.config import DatabaseConfig
        from data_factory.database import load_stage1_sink

        _, manifest = full_run(self.cfg, [good("1")])
        (self.cfg.paths.curated_dir / "cases.csv").write_text("changed\n", "utf-8")
        cfg = dataclasses.replace(
            self.cfg,
            database=DatabaseConfig(
                url="postgresql+psycopg://nobody@127.0.0.1:1/nothing",
                password_env="ABSENT",
                olist_csv_dir=self.root,
                stage1_curated_dir=self.cfg.paths.curated_dir,
                batch_size=10,
            ),
        )
        with self.assertRaises(DataError) as caught:
            load_stage1_sink(cfg)
        self.assertIn("interrupted", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
