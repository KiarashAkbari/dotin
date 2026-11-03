"""Exception hierarchy; each error maps to one exit code."""
from .exit_codes import ExitCode


class PipelineError(Exception):
    exit_code: ExitCode = ExitCode.UNEXPECTED

    def __init__(self, message: str, *, context: dict | None = None) -> None:
        super().__init__(message)
        self.context = context or {}


class ConfigError(PipelineError):
    exit_code = ExitCode.CONFIG


class FetchError(PipelineError):
    exit_code = ExitCode.NETWORK


class DataError(PipelineError):
    exit_code = ExitCode.DATA

class QuarantineThresholdError(DataError):
    """Too large a share of records violated the dataset contract."""

class SchemaDriftError(DataError):
    """The source no longer matches the expected columns or value types."""