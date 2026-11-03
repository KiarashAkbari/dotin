"""Process exit codes (sysexits-style)."""
from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    UNEXPECTED = 1
    DATA = 65      # EX_DATAERR: input/data validation failed
    NETWORK = 69   # EX_UNAVAILABLE: API unreachable or bad response
    CONFIG = 78    # EX_CONFIG: invalid or missing configuration