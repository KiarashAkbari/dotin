"""A local CSV that speaks stage 1's paging protocol, so the factory stays the only loader.

The challenge forbids a separate upload script: the new source must go through the
same validation, quarantine and manifest as the API. ``data_factory.fetch``
already isolates the transport behind a one-method protocol::

    class PageSource(Protocol):
        page_size: int
        def fetch_page(self, offset: int) -> list[dict]: ...

so a file source only has to satisfy that. Everything downstream
(``RawPageStore`` pages on disk, ``fetch_all`` resumability, the page-size bound
that keeps the whole file out of memory, ``check_page_schema``) is reused, which
is the point: the refactor is one adapter, not a new pipeline.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Self

from data_factory.errors import DataError, SchemaDriftError
from data_factory.schema import check_page_schema

from .columns import CSV_COLUMNS

DEFAULT_PAGE_SIZE = 10_000
#: matches data_factory.fetch.FINGERPRINT_LENGTH so both source kinds look alike
FINGERPRINT_LENGTH = 12
BOM = "\ufeff"


def read_header(path: Path) -> tuple[str, ...]:
    """Return the first line of the file as the names it declares."""
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        for raw in csv.reader(handle):
            if not raw:
                continue
            return tuple(name.lstrip(BOM).strip() for name in raw)
    raise DataError("CSV file has no header row", context={"path": str(path)})


def file_sha256(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Hash the file as stored, so provenance survives a re-download."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_fingerprint(path: Path, columns: tuple[str, ...] = CSV_COLUMNS) -> str:
    """Stable identity for a file source, in the spirit of ``query_fingerprint``.

    The Socrata fingerprint hashes url plus query parameters. The equivalent for
    a file is its name plus its header, *not* its bytes: re-downloading an
    unchanged competition file must not look like a new dataset, while the exact
    content still lives in ``credit.load_run.file_sha256`` where it belongs.
    """
    payload = {
        "kind": "file",
        "name": Path(path).name,
        "columns": list(columns),
    }
    canonical = io.StringIO()
    json.dump(payload, canonical, sort_keys=True)
    return hashlib.sha256(canonical.getvalue().encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def count_data_rows(path: Path, columns: tuple[str, ...] | None = None) -> int:
    """Rows after the header, counting only lines the loader will accept."""
    width = len(columns or read_header(path))
    total = 0
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        for index, raw in enumerate(reader):
            if index == 0:
                continue
            if not raw:
                continue
            if len(raw) != width:
                raise DataError(
                    "CSV row has the wrong number of fields",
                    context={"line": index + 1, "fields": len(raw), "expected": width},
                )
            total += 1
    return total


class CsvFileSource:
    """Paged reader over a CSV file, compatible with ``data_factory.fetch_all``.

    Pages are produced by streaming forward, so the cost is linear while pages are
    requested in order (which ``fetch_all`` always does) and constant in memory.
    A resume that starts at an earlier offset rewinds and skips: slower, but it
    keeps one code path and no byte-offset index that a quoted newline could break.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        columns: tuple[str, ...] = CSV_COLUMNS,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        if page_size <= 0:
            raise ValueError(f"page_size must be positive, got {page_size}")
        self._path = Path(path)
        self._columns = tuple(columns)
        self.page_size = int(page_size)
        self._handle: io.TextIOBase | None = None
        self._reader: Iterator[list[str]] | None = None
        self._header: tuple[str, ...] | None = None
        self._cursor = 0
        self._current: dict[str, str | None] | None = None
        self._eof = False

    @property
    def path(self) -> Path:
        return self._path

    def validate_header(self) -> tuple[str, ...]:
        """Read and check the header once; every later page depends on it."""
        if self._header is None:
            header = read_header(self._path)
            if header != self._columns:
                missing = sorted(set(self._columns) - set(header))
                extra = sorted(set(header) - set(self._columns))
                raise SchemaDriftError(
                    "CSV header does not match the credit contract",
                    context={
                        "path": str(self._path),
                        "expected": list(self._columns),
                        "actual": list(header),
                        "missing": missing,
                        "unexpected": extra,
                    },
                )
            self._header = header
        return self._header

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
        self._handle = None
        self._reader = None
        self._current = None
        self._cursor = 0
        self._eof = False

    @property
    def header(self) -> tuple[str, ...]:
        """Column names as the file declares them, validated against the contract."""
        return self.validate_header()

    def __enter__(self) -> Self:
        self.validate_header()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _rewind(self) -> None:
        self.close()
        self._handle = self._path.open("r", encoding="utf-8", newline="")
        self._reader = csv.reader(self._handle)
        next(self._reader)  # the header line
        self._cursor = 0

    def _next_row(self) -> dict[str, str | None] | None:
        assert self._reader is not None
        width = len(self.header)
        while True:
            try:
                raw = next(self._reader)
            except StopIteration:
                return None
            if not raw:
                continue
            if len(raw) != width:
                raise DataError(
                    "CSV row has the wrong number of fields",
                    context={
                        "path": str(self._path),
                        "line": self._cursor + 2,
                        "fields": len(raw),
                        "expected": width,
                    },
                )
            # blanks stay missing rather than becoming "": stage 1's contract
            # treats an omitted field as null, and the API source omits nulls
            return dict(zip(self.header, (value if value != "" else None for value in raw)))

    def _ensure_current(self) -> dict[str, str | None] | None:
        if self._current is None and not self._eof:
            row = self._next_row()
            if row is None:
                self._eof = True
            self._current = row
        return self._current

    def fetch_page(self, offset: int) -> list[dict]:
        """Rows starting at ``offset``, at most ``page_size`` of them."""
        if offset < 0:
            raise ValueError(f"offset must not be negative, got {offset}")
        if self._reader is None or offset < self._cursor:
            self._rewind()
        while self._cursor < offset:
            if self._ensure_current() is None:
                return []
            self._current = None
            self._cursor += 1
        page: list[dict] = []
        while len(page) < self.page_size and self._ensure_current() is not None:
            assert self._current is not None
            page.append(dict(self._current))
            self._current = None
            self._cursor += 1
        check_page_schema(page, self._columns, offset=offset)
        return page

    def iter_rows(self) -> Iterator[dict]:
        """Every data row, in file order, as string-or-None records."""
        offset = 0
        while True:
            page = self.fetch_page(offset)
            if not page:
                return
            yield from page
            if len(page) < self.page_size:
                return
            offset += len(page)
