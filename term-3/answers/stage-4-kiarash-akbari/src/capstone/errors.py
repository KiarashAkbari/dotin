"""Exceptions for the capstone chain; each maps to one exit code.

The hierarchy mirrors ``data_factory.errors`` on purpose. Two of these carry a
``context`` dict rather than a formatted string, because the CLI prints the
``--json`` form of the same payload for the run manifest: a message aimed at a
terminal and a record aimed at a file should not be written twice.
"""

from __future__ import annotations

from .exit_codes import ExitCode


class CapstoneError(Exception):
    """Base class. Anything raised deliberately is a subclass of this."""

    exit_code: ExitCode = ExitCode.UNEXPECTED

    def __init__(self, message: str, *, context: dict | None = None) -> None:
        super().__init__(message)
        self.context = context or {}


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
