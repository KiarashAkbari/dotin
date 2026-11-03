"""Cumulative HTTP request count of the case download, kept next to the raw pages."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from .errors import DataError

FETCH_LOG_FILENAME = "fetch_log.json"
REQUESTS_KEY = "requests_total"
MIN_REQUESTS = 1
TMP_SUFFIX = ".tmp"


def minimum_requests(total_rows: int, page_size: int) -> int:
    """Fewest requests that can deliver total_rows when a page holds page_size rows."""
    return max(MIN_REQUESTS, math.ceil(total_rows / page_size))


def read_requests_total(directory: Path) -> int | None:
    path = Path(directory) / FETCH_LOG_FILENAME
    try:
        with path.open("r", encoding="utf-8") as fh:
            return int(json.load(fh)[REQUESTS_KEY])
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DataError(f"Fetch log is unreadable: {path}", context={"path": str(path)}) from exc


def record_requests(directory: Path, requests: int) -> int | None:
    """Add this run's requests to the running total; skip when there is nothing to add."""
    known = read_requests_total(directory)
    if known is None and requests == 0:
        return None
    total = (known or 0) + requests
    path = Path(directory) / FETCH_LOG_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump({REQUESTS_KEY: total}, fh)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return total