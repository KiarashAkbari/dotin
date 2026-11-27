"""Exceptions for the capstone chain; each maps to one exit code.

The hierarchy mirrors ``data_factory.errors`` on purpose. Two of these carry a
``context`` dict rather than a formatted string, because the CLI prints the
``--json`` form of the same payload for the run manifest: a message aimed at a
terminal and a record aimed at a file should not be written twice.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from .exit_codes import ExitCode

#: ``scheme://user:password@`` anywhere in a text; the password half is what gets replaced
_URI_PASSWORD = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^:/@\s]+:)([^@\s]+)@")
REDACTED = "«redacted»"


def _secrets(environ) -> list[str]:
    """Every password this process can see: the one inside each ``*_URI``/``*_URL`` value and each
    ``*PASSWORD*`` value. Shorter than 3 characters is skipped -- replacing ``a`` everywhere would
    shred the message without protecting anything."""
    found: set[str] = set()
    for key, value in environ.items():
        if not value:
            continue
        upper = key.upper()
        if "PASSWORD" in upper:
            found.add(value)
        elif upper.endswith(("_URI", "_URL")) and "://" in value:
            try:
                secret = urlsplit(value).password
            except ValueError:
                secret = None
            if secret:
                found.add(secret)
    return sorted((s for s in found if len(s) >= 3), key=len, reverse=True)


def redact(text: str, environ=None) -> str:
    """``text`` with every database password it could carry replaced by ``«redacted»``.

    Two passes: a URI written into the text loses its password whatever it is, and any password held
    in this process's environment is removed wherever it appears -- a driver or a child process may echo
    it without the ``user:...@`` around it. Error messages end up in manifests, logs and test output,
    and a failing assertion is printed in full, so the scrub sits where every message is born.
    """
    if not text:
        return text
    out = _URI_PASSWORD.sub(lambda m: m.group(1) + REDACTED + "@", str(text))
    for secret in _secrets(os.environ if environ is None else environ):
        out = out.replace(secret, REDACTED)
    return out


def _redact_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(item) for item in value)
    return value


class CapstoneError(Exception):
    """Base class. Anything raised deliberately is a subclass of this.

    Its message and context are passed through :func:`redact` on the way in, so no subclass can carry
    a database password to a terminal, a manifest or a test report.
    """

    exit_code: ExitCode = ExitCode.UNEXPECTED

    def __init__(self, message: str, *, context: dict | None = None) -> None:
        super().__init__(redact(message))
        self.context = _redact_value(context or {})


class ConfigError(CapstoneError):
    """A key is missing, undecided, or contradicts the brief."""

    exit_code = ExitCode.CONFIG


class InputError(CapstoneError):
    """An input file the chain was handed cannot be used: absent, empty, or the
    wrong shape. ``1`` is reserved for a crash, and a missing file is not one."""

    exit_code = ExitCode.DATA


class AuditError(CapstoneError):
    """The column time audit is unreadable, self-contradictory, or incomplete."""

    exit_code = ExitCode.DATA


class BudgetError(CapstoneError):
    """A measured time broke a ceiling the brief states as a limit."""

    exit_code = ExitCode.DATA


class NotReadyError(CapstoneError):
    """The scaffold is here; the phase that fills this step in is not."""

    exit_code = ExitCode.NOT_READY


class SourceError(CapstoneError):
    """The external API could not be reached or returned something unusable."""

    exit_code = ExitCode.NETWORK
