"""The ingest step: stage 1 loads Olist, with absolute paths and no password on disk (review B4)."""

from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts src on the path)

from capstone import ingest
from capstone.config import load_config
from capstone.errors import ConfigError, InputError


def write_olist(directory: Path, rows: int = 2) -> None:
    """Nine tiny CSVs with the headers stage 2's schema expects."""
    directory.mkdir(parents=True, exist_ok=True)
    for table, name in ingest.OLIST_FILES.items():
        header = ",".join(ingest.REQUIRED_HEADERS[table])
        body = "\n".join(",".join(f"v{r}" for _ in ingest.REQUIRED_HEADERS[table]) for r in range(rows))
        (directory / name).write_text(header + "\n" + body + "\n", encoding="utf-8")


class VerifyFilesTests(unittest.TestCase):
    def test_counts_records_not_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.csv"
            path.write_text('a,b\n1,"two\nlines"\n3,4\n', encoding="utf-8")
            self.assertEqual(ingest.count_records(path), (["a", "b"], 2))

    def test_a_complete_small_folder_is_accepted_and_not_called_public(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_olist(Path(tmp))
            counts = ingest.verify_files(Path(tmp))
        self.assertEqual(set(counts), set(ingest.OLIST_FILES))
        self.assertNotEqual(counts, ingest.PUBLIC_OLIST_ROWS)

    def test_a_missing_file_and_a_wrong_header_are_both_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_olist(Path(tmp))
            (Path(tmp) / "olist_sellers_dataset.csv").unlink()
            (Path(tmp) / "olist_orders_dataset.csv").write_text("order_id\nx\n", encoding="utf-8")
            with self.assertRaises(InputError) as caught:
                ingest.verify_files(Path(tmp))
        message = str(caught.exception)
        self.assertIn("olist_sellers_dataset.csv: missing", message)
        self.assertIn("order_approved_at", message)

    def test_a_missing_folder_names_the_override_variable(self) -> None:
        with self.assertRaises(InputError) as caught:
            ingest.verify_files(Path("/nonexistent/olist"))
        self.assertIn(ingest.OLIST_DIR_ENV, str(caught.exception))


class Stage1CommandTests(unittest.TestCase):
    URI = "postgresql://kiarash:secret@db.example:5433/dotin"

    def test_the_url_loses_its_password_and_keeps_the_rest(self) -> None:
        url, password = ingest.stage1_url(self.URI)
        self.assertEqual(url, "postgresql+psycopg://kiarash@db.example:5433/dotin")
        self.assertEqual(password, "secret")

    def test_a_non_postgres_uri_is_refused(self) -> None:
        with self.assertRaises(ConfigError):
            ingest.stage1_url("mysql://x@y/z")

    def test_the_plan_runs_load_database_with_reset_and_an_absolute_config(self) -> None:
        config = load_config(helpers.DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as tmp:
            olist = Path(tmp) / "olist"
            write_olist(olist)
            plan = ingest.plan(
                config,
                {ingest.DATABASE_URI_ENV: self.URI, ingest.OLIST_DIR_ENV: str(olist)},
            )
            written = plan.stage1_config.read_text(encoding="utf-8")
            parsed = tomllib.loads(written)
        self.assertEqual(plan.command[1:4], ("-m", "data_factory", "load-database"))
        self.assertIn("--reset", plan.command)
        self.assertIn("--skip-factory-sink", plan.command)
        self.assertNotIn("fetch", plan.command, "the NYC-311 fetch is not the capstone's ingest")
        self.assertTrue(Path(plan.command[plan.command.index("--config") + 1]).is_absolute())
        self.assertNotIn("secret", written, "the password must never be written to disk")
        self.assertEqual(Path(parsed["database"]["olist_csv_dir"]), olist.resolve())
        self.assertTrue(Path(parsed["database"]["sql_migrations_dir"]).is_absolute())
        self.assertTrue((plan.stage1_dir / "src" / "data_factory").is_dir())

    def test_the_child_gets_the_password_only_through_its_environment(self) -> None:
        config = load_config(helpers.DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as tmp:
            write_olist(Path(tmp))
            environ = {ingest.DATABASE_URI_ENV: self.URI, ingest.OLIST_DIR_ENV: tmp}
            plan = ingest.plan(config, environ)
            child = ingest.subprocess_env(plan, environ)
        self.assertEqual(child[ingest.STAGE1_PASSWORD_ENV], "secret")
        self.assertTrue(child["PYTHONPATH"].startswith(str(plan.stage1_dir / "src")))

    def test_no_database_uri_is_a_config_error(self) -> None:
        config = load_config(helpers.DEFAULT_CONFIG)
        with self.assertRaises(ConfigError):
            ingest.plan(config, {})


if __name__ == "__main__":
    unittest.main()
