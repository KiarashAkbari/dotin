"""``credit.db``: the one engine factory, tested without a server.

The value of this module is the *error*, so the error is what is asserted: that a refusal
turns into a ConfigError naming the environment variable the config points at, that the hint
is a line a person can paste, that nothing credential-shaped survives, and that the probe
really runs before the engine is handed back. No driver and no server are needed for any of
it - a raised ``sqlalchemy.exc.SQLAlchemyError`` from a fake engine is the same object the
real path raises.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa

HERE = Path(__file__).resolve().parent
for candidate in (
    HERE,
    HERE.parent / "src",
    HERE.parents[1] / "stage-1" / "src",
):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from data_factory.errors import ConfigError

from credit import db

ENV = "TEST_DB_PASSWORD"


def fake_config() -> SimpleNamespace:
    return SimpleNamespace(database=SimpleNamespace(password_env=ENV))


class RefusingEngine:
    """An engine whose ``connect`` raises what the driver would raise."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.disposed = False
        self.connects = 0

    def connect(self) -> None:
        self.connects += 1
        raise self.error

    def dispose(self) -> None:
        self.disposed = True


class _Session:
    """What ``engine.connect()`` hands back inside the ``with``."""

    def __init__(self, owner) -> None:
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def execute(self, statement) -> None:
        self.owner.statements.append(str(statement))


class WillingEngine:
    def __init__(self) -> None:
        self.connects = 0
        self.disposed = False
        self.statements: list[str] = []

    def connect(self):
        self.connects += 1
        return _Session(self)

    def dispose(self) -> None:
        self.disposed = True


def patch_engine(factory):
    """Swap ``db.sa.create_engine`` for the duration of a test, then put it back."""
    original = db.sa.create_engine

    class _Restorer:
        def __enter__(self):
            return None

        def __exit__(self, *exc: object) -> None:
            db.sa.create_engine = original

    db.sa.create_engine = factory
    return _Restorer()


class EngineForTests(unittest.TestCase):
    def setUp(self):
        self.original_load = db.load_config
        self.original_url = db.build_database_url
        db.load_config = lambda path: fake_config()
        db.build_database_url = lambda cfg: (
            "postgresql+psycopg://user@127.0.0.1:5999/none"
        )

    def tearDown(self):
        db.load_config = self.original_load
        db.build_database_url = self.original_url

    def test_a_refusal_becomes_a_config_error_that_names_the_variable(self):
        refusal = sa.exc.SQLAlchemyError(
            "connection failed: fe_sendauth: no password supplied"
        )
        engine = RefusingEngine(refusal)
        with patch_engine(lambda url: engine), self.assertRaises(ConfigError) as caught:
            db.engine_for(Path("stage-1/config/default.toml"))
        message = str(caught.exception)
        self.assertIn("the database refused the connection", message)
        self.assertIn("no password supplied", message)
        self.assertEqual(caught.exception.context["password_env"], ENV)
        self.assertIn(f"read -s {ENV} && export {ENV}", caught.exception.context["fix"])
        self.assertTrue(
            engine.disposed, "the refused engine must not be left in the pool"
        )

    def test_a_refusal_that_is_not_about_credentials_does_not_blame_one(self):
        """``Connection refused`` must not be answered with an export line.

        The instruction is the whole value of this error, so an instruction that points at a
        credential the operator already has is worse than the traceback it replaced.
        """
        refusal = sa.exc.SQLAlchemyError(
            'connection failed: connection to server at "127.0.0.1", port 5499 failed: '
            "Connection refused"
        )
        with (
            patch_engine(lambda url: RefusingEngine(refusal)),
            self.assertRaises(ConfigError) as caught,
        ):
            db.engine_for(Path("x.toml"))
        fix = caught.exception.context["fix"]
        self.assertNotIn("read -s", fix)
        self.assertIn("database.url", fix)
        self.assertIn("Connection refused", str(caught.exception))
        # the variable is still named, because it is a fact about the config either way
        self.assertEqual(caught.exception.context["password_env"], ENV)

    def test_the_error_never_carries_a_credential(self):
        refusal = sa.exc.SQLAlchemyError(
            'connection failed: password authentication failed for user "kiarash" '
            "password=hunter2\n(Background on this error at: https://sqlalche.me/e/21/e3q8)"
        )
        with (
            patch_engine(lambda url: RefusingEngine(refusal)),
            self.assertRaises(ConfigError) as caught,
        ):
            db.engine_for(Path("x.toml"))
        text = str(caught.exception) + repr(caught.exception.context)
        self.assertNotIn("hunter2", text)
        # only the driver's first line is kept, so the boilerplate is gone too
        self.assertNotIn("sqlalche.me", text)

    def test_the_probe_runs_before_the_engine_is_returned(self):
        engine = WillingEngine()
        with patch_engine(lambda url: engine):
            returned = db.engine_for(Path("x.toml"))
        self.assertIs(returned, engine)
        self.assertEqual(engine.connects, 1)
        self.assertIn("SELECT 1", engine.statements[0])

    def test_probe_false_hands_back_the_engine_without_touching_the_server(self):
        engine = WillingEngine()
        with patch_engine(lambda url: engine):
            returned = db.engine_for(Path("x.toml"), probe=False)
        self.assertIs(returned, engine)
        self.assertEqual(engine.connects, 0)


class CauseTests(unittest.TestCase):
    def test_the_credential_words_are_recognised(self):
        self.assertTrue(db.is_a_credential_refusal("fe_sendauth: no password supplied"))
        self.assertTrue(
            db.is_a_credential_refusal(
                'password authentication failed for user "kiarash"'
            )
        )
        self.assertFalse(
            db.is_a_credential_refusal(
                "connection to server at 127.0.0.1 failed: refused"
            )
        )
        self.assertFalse(db.is_a_credential_refusal("server closed the connection"))


class RedactionTests(unittest.TestCase):
    def test_both_dsn_shapes_are_redacted(self):
        self.assertEqual(
            db.redact("postgresql+psycopg://kiarash:hunter2@127.0.0.1:5432/postgres"),
            "postgresql+psycopg://[redacted]@127.0.0.1:5432/postgres",
        )
        self.assertEqual(
            db.redact("conninfo password =hunter2 host=127.0.0.1"),
            "conninfo password =[redacted] host=127.0.0.1",
        )
        self.assertEqual(db.redact("Password: hunter2"), "Password:[redacted]")

    def test_a_message_without_a_secret_is_left_alone(self):
        text = 'connection to server at "127.0.0.1", port 5432 failed'
        self.assertEqual(db.redact(text), text)

    def test_refusal_reads_the_driver_error_and_only_its_first_line(self):
        class DriverError(Exception):
            def __str__(self) -> str:
                return "auth failed\n  detail line"

        outer = sa.exc.SQLAlchemyError("wrapper noise\nmore noise")
        outer.orig = DriverError()
        self.assertEqual(db.connection_refusal(outer), "auth failed")

    def test_an_exception_without_a_cause_still_reads(self):
        self.assertEqual(
            db.connection_refusal(
                RuntimeError("server closed the connection unexpectedly")
            ),
            "server closed the connection unexpectedly",
        )


class HintTests(unittest.TestCase):
    def test_the_hint_is_one_pasteable_line_and_says_what_it_does_not_do(self):
        hint = db.password_hint("OLIST_DB_PASSWORD")
        self.assertIn("read -s OLIST_DB_PASSWORD && export OLIST_DB_PASSWORD", hint)
        self.assertIn("notebook kernel", hint)
        self.assertIn(".pgpass", hint)
        self.assertNotIn("hunter", hint)


if __name__ == "__main__":
    unittest.main()
