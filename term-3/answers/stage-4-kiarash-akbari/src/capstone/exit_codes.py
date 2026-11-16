"""Process exit codes, following the numbering stage 1 established.

``stage-1/src/data_factory/exit_codes.py`` uses sysexits-style numbers, and the
capstone chain calls into that package, so a shared meaning is worth more than a
fresh set. ``NOT_READY`` is the one addition: the scaffold can tell an evaluator
"this step exists but the phase behind it is unwritten" apart from a real failure,
which ``1`` (unexpected) would blur together with a crash.
"""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    UNEXPECTED = 1
    USAGE = 64  # EX_USAGE: bad command line, e.g. --require-complete without --ddl
    DATA = 65  # EX_DATAERR: audit coverage or feature frame violated a contract
    NOT_READY = 67  # the step is scaffolded; its phase is not written yet
    NETWORK = 69  # EX_UNAVAILABLE: the external source is unreachable
    CONFIG = 78  # EX_CONFIG: a student decision is still "unset"
