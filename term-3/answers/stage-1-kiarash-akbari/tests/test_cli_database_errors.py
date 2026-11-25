"""Database failures leave the CLI as one readable line and a class-specific exit code.

Before this, any SQLAlchemy error escaped `main()` as a traceback with exit 1, and a
config without [database] exited 65 (a data error) instead of 78 (a config error).
"""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

import sqlalchemy as sa
from config_helpers import write_config_toml

from data_factory.cli import main
from data_factory.database import translate_database_error
from data_factory.errors import (
    DatabaseDataError,
    DatabaseSchemaError,
    DatabaseUnavailableError,
    PipelineError,
)
from data_factory.exit_codes import ExitCode

UNREACHABLE = "postgresql+psycopg://nobody@127.0.0.1:1/nothing"


def database_section(url: str, root: Path) -> str:
    return f"""
[database]
url = "{url}"
password_env = "ABSENT_PASSWORD_FOR_TEST"
olist_csv_dir = "{(root / "olist").as_posix()}"
stage1_curated_dir = "{(root / "curated").as_posix()}"
batch_size = 100
sql_migrations_dir = "{(root / "sql").as_posix()}"
"""


class FakeOrig(Exception):
    def __init__(self, message, sqlstate):
        super().__init__(message)
        self.sqlstate = sqlstate


class CliDatabaseErrorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # The sink migration is checked before connecting, so give the run one
        # and let it get as far as the connection under test.
        (self.root / "sql").mkdir()
        (self.root / "sql" / "009_factory_sink_schema.sql").write_text("SELECT 1;\n")

    def _main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_missing_database_section_is_a_config_error(self):
        config = write_config_toml(self.root / "c.toml")
        code, _, err = self._main("load-database", "--config", str(config))
        self.assertEqual(code, int(ExitCode.CONFIG))
        self.assertIn("[database]", err)

    def test_unreachable_database_exits_69_without_a_traceback(self):
        config = write_config_toml(
            self.root / "c.toml", extra=database_section(UNREACHABLE, self.root)
        )
        code, _, err = self._main("load-database", "--config", str(config))
        self.assertEqual(code, int(ExitCode.NETWORK))
        self.assertNotIn("Traceback", err)
        self.assertIn("Database is unavailable", err)

    def test_reset_with_skip_migrations_is_refused_before_connecting(self):
        config = write_config_toml(
            self.root / "c.toml", extra=database_section(UNREACHABLE, self.root)
        )
        code, _, err = self._main(
            "load-database", "--config", str(config), "--reset", "--skip-sql-migrations"
        )
        self.assertEqual(code, int(ExitCode.CONFIG), err)
        self.assertNotIn("unavailable", err)


class TranslateDatabaseErrorTests(unittest.TestCase):
    def _wrapped(self, cls, sqlstate):
        return cls("statement", {}, FakeOrig(f"boom {sqlstate}\nDETAIL: more", sqlstate))

    def test_connection_failure_is_unavailable(self):
        error = translate_database_error(self._wrapped(sa.exc.OperationalError, None))
        self.assertIsInstance(error, DatabaseUnavailableError)
        self.assertEqual(error.exit_code, ExitCode.NETWORK)

    def test_admin_shutdown_is_unavailable(self):
        error = translate_database_error(self._wrapped(sa.exc.OperationalError, "57P01"))
        self.assertIsInstance(error, DatabaseUnavailableError)

    def test_undefined_table_is_a_schema_error_with_config_exit(self):
        error = translate_database_error(self._wrapped(sa.exc.ProgrammingError, "42P01"))
        self.assertIsInstance(error, DatabaseSchemaError)
        self.assertEqual(error.exit_code, ExitCode.CONFIG)
        self.assertEqual(error.context["sqlstate"], "42P01")

    def test_reflection_of_a_missing_table_is_a_schema_error(self):
        error = translate_database_error(sa.exc.NoSuchTableError("olist.orders"))
        self.assertIsInstance(error, DatabaseSchemaError)

    def test_constraint_violation_is_a_data_error(self):
        error = translate_database_error(self._wrapped(sa.exc.IntegrityError, "23505"))
        self.assertIsInstance(error, DatabaseDataError)
        self.assertEqual(error.exit_code, ExitCode.DATA)

    def test_message_keeps_only_the_first_line(self):
        error = translate_database_error(self._wrapped(sa.exc.IntegrityError, "23505"))
        self.assertEqual(error.context["error"], "boom 23505")

    def test_anything_else_is_unexpected_but_still_a_pipeline_error(self):
        error = translate_database_error(self._wrapped(sa.exc.InternalError, "XX000"))
        self.assertIs(type(error), PipelineError)
        self.assertEqual(error.exit_code, ExitCode.UNEXPECTED)


@unittest.skipUnless(
    os.environ.get("TEST_DATABASE_URL"),
    "TEST_DATABASE_URL is not set: the missing-table exit code needs a real PostgreSQL",
)
class RealDatabaseErrorTests(unittest.TestCase):
    """Against a real server: loading into a schema nobody migrated."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.url = os.environ["TEST_DATABASE_URL"]
        engine = sa.create_engine(self.url)
        with engine.begin() as connection:
            connection.execute(sa.text("DROP SCHEMA IF EXISTS olist CASCADE"))
        engine.dispose()

    def test_load_without_migrations_into_an_empty_database_exits_78(self):
        config = write_config_toml(
            self.root / "c.toml",
            extra=database_section(
                sa.make_url(self.url).render_as_string(hide_password=False), self.root
            ),
        )
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = main(["load-database", "--config", str(config), "--skip-sql-migrations"])
        self.assertEqual(code, int(ExitCode.CONFIG), err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
