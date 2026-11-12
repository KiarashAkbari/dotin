"""One place that turns a config file into an engine known to work.

Three entry points need a connection - the loader, the scorer, and the notebook's first cell -
and each of them used to write the same line: ``sa.create_engine(build_database_url(...))``.
That line is correct, and it fails in the worst available shape. The engine is lazy, so a
missing credential surfaces much later as a page of SQLAlchemy frames wrapped around one
psycopg sentence, on a machine where the honest answer is a single instruction: this process
has no password, and a notebook kernel inherits the environment of the shell that launched
it. That is exactly what a reviewer hit while running the notebook after a successful
``credit.load`` in another shell.

So the check happens once, here, and the failure names the fix. Nothing about the credential
is written down anywhere: the driver's text is redacted before it leaves this module, because
a driver can quote the conninfo it was handed, and the DSN shape puts a secret between
``://`` and ``@``.
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlalchemy as sa
from data_factory.config import load_config
from data_factory.database import build_database_url
from data_factory.errors import ConfigError

# the two shapes a secret takes in a DSN, whichever driver produced the message
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(password\s*[=:])[ \t]*\S+"), r"\1[redacted]"),
    (re.compile(r"://[^/@\s:]+:[^/@\s]+@"), "://[redacted]@"),
)


def redact(text: str) -> str:
    """Remove anything that reads like a credential from a driver's message."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def connection_refusal(exc: BaseException) -> str:
    """The first line of the server's own words, with the traceback noise dropped.

    ``.orig`` is what SQLAlchemy wraps: the driver's exception, which says the true thing
    ("no password supplied", "password authentication failed"). The lines below it are
    SQLAlchemy's boilerplate and a docs URL, and neither helps anyone fix a DSN.
    """
    cause = getattr(exc, "orig", None) or exc
    text = str(cause).strip()
    first = text.splitlines()[0].strip() if text else type(cause).__name__
    return redact(first)


def password_hint(env: str) -> str:
    """The one line that fixes the common case, without putting a secret in a file."""
    return (
        f"read -s {env} && export {env}   <- run it in the shell that starts this program. a "
        "notebook kernel inherits that shell's environment, so the same export has to happen "
        "before Run All too; a ~/.pgpass entry for the same user works as well, and no "
        "password goes into any file this repo tracks"
    )


CREDENTIAL_MARKERS = ("password", "auth")


def is_a_credential_refusal(detail: str) -> bool:
    """Whether the server's own words point at the credential rather than the server.

    A refusal to answer at all (``Connection refused``, a wrong port, a stopped cluster) gets
    the same exception type, and telling that operator to export a password they already have
    would send them to the one file that cannot help them.
    """
    lowered = detail.lower()
    return any(marker in lowered for marker in CREDENTIAL_MARKERS)


def server_hint(config_path: Path) -> str:
    """The instruction for a refusal that is not about a password.

    Deliberately not "the server is down": a role that does not exist and a port that nobody
    listens on arrive as the same exception type, and the only thing both have in common is
    that the answer came before this stage sent a statement.
    """
    return (
        f"the refusal is not about a password: re-read database.url in {config_path} (host, "
        "port, role) against the server's own words above, and note that nothing was written "
        "to the database - this stage never starts a server"
    )


def engine_for(config_path: Path, *, probe: bool = True) -> sa.Engine:
    """An engine over stage 1's config, or a :class:`ConfigError` that says what to run.

    ``probe`` costs one round trip at start-up and is what turns every later traceback into
    this error; ``probe=False`` is there for a caller that only wants an engine to describe.
    A refused connection is reported as a config problem on purpose: what is missing is a
    credential the configuration names, not a row, and ``ConfigError`` is what the CLIs
    already map to ``EX_CONFIG``.
    """
    stage1 = load_config(config_path)
    engine = sa.create_engine(build_database_url(stage1))
    if not probe:
        return engine
    try:
        with engine.connect() as connection:
            connection.execute(sa.text("SELECT 1"))
    except sa.exc.SQLAlchemyError as exc:
        engine.dispose()
        detail = connection_refusal(exc)
        env = stage1.database.password_env
        raise ConfigError(
            f"the database refused the connection: {detail}",
            context={
                "config": str(config_path),
                "password_env": env,
                "fix": (
                    password_hint(env)
                    if is_a_credential_refusal(detail)
                    else server_hint(config_path)
                ),
            },
        ) from exc
    return engine
