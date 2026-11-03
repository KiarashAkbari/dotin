"""Quarantine output: rejected records with their reason codes, written atomically."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .quality import ClassifiedRecord

QUARANTINE_FILENAME = "quarantine.jsonl"
FAILED_FILENAME = "quarantine.failed.jsonl"
VALIDATE_FILENAME = "quarantine.validate.jsonl"
TMP_SUFFIX = ".tmp"


class QuarantineWriter:
    def __init__(self, directory: Path, filename: str = QUARANTINE_FILENAME) -> None:
        self._final = Path(directory) / filename
        self._tmp = self._final.with_name(self._final.name + TMP_SUFFIX)
        self._fh = None
        self._failed = False
        self.count = 0

    @property
    def path(self) -> Path:
        return self._final

    def mark_failed(self) -> None:
        # publish this run's quarantine under a separate name; the last good set stays intact
        self._final = self._final.with_name(FAILED_FILENAME)
        self._failed = True

    def __enter__(self) -> "QuarantineWriter":
        self._final.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._tmp.open("w", encoding="utf-8", newline="\n")
        return self

    def write(self, record: ClassifiedRecord) -> None:
        if self._fh is None:
            raise RuntimeError("QuarantineWriter must be used as a context manager")
        payload = {"reasons": [r.value for r in record.reasons], "row": record.row}
        self._fh.write(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        )
        self._fh.write("\n")
        self.count += 1

    def __exit__(self, exc_type, exc, tb) -> None:
        fh, self._fh = self._fh, None
        if exc_type is not None:
            fh.close()
            self._tmp.unlink(missing_ok=True)
            return
        fh.flush()
        os.fsync(fh.fileno())
        fh.close()
        os.replace(self._tmp, self._final)
        if not self._failed and self._final.name == QUARANTINE_FILENAME:
            self._final.with_name(FAILED_FILENAME).unlink(missing_ok=True)