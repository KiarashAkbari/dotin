"""Relative paths in a config file are anchored to the file, not to the shell's cwd.

Before, `load_config` kept `data/raw` as a cwd-relative Path, so the same file pointed
at different folders depending on where the command was started; run from stage 4,
the chain found no config paths at all.
"""

import contextlib
import os
import tempfile
import unittest
from pathlib import Path

from config_helpers import write_config_toml

from data_factory.config import display_path, load_config, parse_config

STAGE_ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class ConfigPathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        (self.root / "project" / "config").mkdir(parents=True)
        (self.root / "elsewhere").mkdir()

    def _relative_config(self, extra_paths=""):
        path = self.root / "project" / "config" / "c.toml"
        write_config_toml(
            path,
            raw="data/raw",
            curated="data/curated",
            quarantine="data/quarantine",
            manifest="data/run_manifest.json",
            reference="data/raw/reference/ref.json",
        )
        if extra_paths:
            text = path.read_text(encoding="utf-8").replace("[paths]\n", f"[paths]\n{extra_paths}\n")
            path.write_text(text, encoding="utf-8")
        return path

    def test_relative_paths_default_to_the_config_folder(self):
        config = self._relative_config()
        with working_directory(self.root / "elsewhere"):
            cfg = load_config(config)
        self.assertEqual(cfg.root, self.root / "project" / "config")
        self.assertEqual(cfg.paths.raw_dir, self.root / "project" / "config" / "data" / "raw")

    def test_root_is_relative_to_the_config_file_and_anchors_every_section(self):
        config = self._relative_config('root = ".."')
        with working_directory(self.root / "elsewhere"):
            cfg = load_config(config)
        project = self.root / "project"
        self.assertEqual(cfg.root, project)
        self.assertEqual(cfg.paths.raw_dir, project / "data" / "raw")
        self.assertEqual(cfg.paths.manifest_path, project / "data" / "run_manifest.json")
        self.assertEqual(cfg.reference.path, project / "data" / "raw" / "reference" / "ref.json")

    def test_the_result_does_not_depend_on_the_current_directory(self):
        config = self._relative_config('root = ".."')
        with working_directory(self.root / "elsewhere"):
            first = load_config(config)
        with working_directory(self.root / "project"):
            second = load_config(Path("config") / "c.toml")
        self.assertEqual(first.paths, second.paths)
        self.assertEqual(first.reference, second.reference)

    def test_absolute_paths_are_kept(self):
        target = (self.root / "abs-raw").as_posix()
        config = self.root / "project" / "config" / "c.toml"
        write_config_toml(config, raw=target)
        self.assertEqual(load_config(config).paths.raw_dir, Path(target))

    def test_database_paths_are_anchored_too(self):
        config = self._relative_config('root = ".."')
        text = config.read_text(encoding="utf-8") + (
            '\n[database]\nurl = "postgresql+psycopg://u@localhost:5432/db"\n'
            'password_env = "P"\nolist_csv_dir = "../stage-2/data/raw/olist"\n'
            'stage1_curated_dir = "data/curated"\nbatch_size = 10\n'
            'sql_migrations_dir = "../stage-2/sql"\n'
        )
        config.write_text(text, encoding="utf-8")
        with working_directory(self.root / "elsewhere"):
            cfg = load_config(config)
        self.assertEqual(cfg.database.olist_csv_dir, self.root / "stage-2" / "data" / "raw" / "olist")
        self.assertEqual(cfg.database.sql_migrations_dir, self.root / "stage-2" / "sql")
        self.assertEqual(cfg.database.stage1_curated_dir, self.root / "project" / "data" / "curated")

    def test_a_config_built_in_memory_keeps_relative_paths(self):
        config = self._relative_config()
        import tomllib

        cfg = parse_config(tomllib.loads(config.read_text(encoding="utf-8")))
        self.assertIsNone(cfg.root)
        self.assertEqual(cfg.paths.raw_dir, Path("data/raw"))

    def test_display_path_is_relative_to_the_root(self):
        cfg = load_config(self._relative_config('root = ".."'))
        self.assertEqual(display_path(cfg, cfg.paths.curated_dir / "cases.csv"), "data/curated/cases.csv")
        outside = self.root / "elsewhere" / "x.csv"
        self.assertEqual(display_path(cfg, outside), outside.as_posix())

    def test_root_must_be_a_string(self):
        config = self._relative_config("root = 3")
        from data_factory.errors import ConfigError

        with self.assertRaises(ConfigError):
            load_config(config)

    def test_shipped_default_config_points_into_the_stage1_folder_from_any_cwd(self):
        with working_directory(self.root / "elsewhere"):
            cfg = load_config(STAGE_ROOT / "config" / "default.toml")
        self.assertEqual(cfg.root, STAGE_ROOT)
        self.assertEqual(cfg.paths.raw_dir, STAGE_ROOT / "data" / "raw")
        self.assertEqual(cfg.paths.manifest_path, STAGE_ROOT / "data" / "run_manifest.json")
        self.assertEqual(
            cfg.database.sql_migrations_dir.parent.parent, STAGE_ROOT.parent,
            "the stage-2 sql dir is a sibling of stage 1",
        )


if __name__ == "__main__":
    unittest.main()
