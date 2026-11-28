"""Paged, resumable download of the source into the raw store."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from .config import SourceConfig
from .errors import FetchError
from .http import HttpClient
from .store import RawPageStore
from .schema import check_page_schema

TOKEN_HEADER = "X-App-Token"
ORDER_BY = "created_date,unique_key"
CURSOR_COLUMNS = ("created_date", "unique_key")
FINGERPRINT_LENGTH = 12

# Position after which the next page starts: the (created_date, unique_key) of the
# last row already stored. Both are text in the source, compared the way $order sorts.
Cursor = tuple[str, str]


class PageSource(Protocol):
    """Offset paging: page N is the rows at offset N * page_size.

    Fine for a file that cannot change under the reader. A live source should
    implement KeysetPageSource instead.
    """

    page_size: int

    def fetch_page(self, offset: int) -> list[dict]: ...


class KeysetPageSource(Protocol):
    """Keyset paging: each page starts strictly after the last row stored.

    With offsets, a row inserted or deleted upstream between two requests shifts
    every later page by one, so a row is skipped or fetched twice. A cursor taken
    from the data itself does not move when other rows appear or disappear.
    """

    page_size: int

    def fetch_after(self, cursor: Cursor | None) -> list[dict]: ...

    def cursor_of(self, row: dict) -> Cursor: ...


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_where(cfg: SourceConfig, after: Cursor | None = None) -> str:
    types = ", ".join(_quote(t) for t in cfg.complaint_types)
    start = f"{cfg.start_date.isoformat()}T00:00:00"
    end_exclusive = f"{(cfg.end_date + timedelta(days=1)).isoformat()}T00:00:00"
    scope = (
        f"agency = {_quote(cfg.agency)} AND complaint_type in({types}) "
        f"AND created_date >= {_quote(start)} AND created_date < {_quote(end_exclusive)}"
    )
    if after is None:
        return scope
    created, key = (_quote(part) for part in after)
    return (
        f"{scope} AND (created_date > {created} "
        f"OR (created_date = {created} AND unique_key > {key}))"
    )


def build_params(cfg: SourceConfig, after: Cursor | None = None) -> dict:
    return {
        "$select": ",".join(cfg.columns),
        "$where": build_where(cfg, after),
        "$order": ORDER_BY,
        "$limit": cfg.page_size,
    }


def cursor_of(row: dict) -> Cursor:
    """The keyset position of a stored row; a row without one cannot be paged past."""
    values = tuple(row.get(column) for column in CURSOR_COLUMNS)
    if not all(isinstance(value, str) and value for value in values):
        raise FetchError(
            "Row has no usable paging cursor",
            context={"columns": list(CURSOR_COLUMNS), "row": {c: row.get(c) for c in CURSOR_COLUMNS}},
        )
    return values  # type: ignore[return-value]


def query_fingerprint(cfg: SourceConfig) -> str:
    """Identity of the scope (url, filter, columns, order, page size), not of a page."""
    params = build_params(cfg)
    canonical = json.dumps({"url": cfg.url, "params": params}, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


class SocrataSource:
    def __init__(self, client: HttpClient, cfg: SourceConfig, token: str | None = None) -> None:
        self._client = client
        self._cfg = cfg
        self._headers = {TOKEN_HEADER: token} if token else None
        self.page_size = cfg.page_size

    @classmethod
    def from_env(cls, client: HttpClient, cfg: SourceConfig) -> "SocrataSource":
        return cls(client, cfg, os.environ.get(cfg.app_token_env) or None)

    def cursor_of(self, row: dict) -> Cursor:
        return cursor_of(row)

    def fetch_after(self, cursor: Cursor | None) -> list[dict]:
        payload = self._client.get_json(
            self._cfg.url, params=build_params(self._cfg, cursor), headers=self._headers
        )
        where = {"after": list(cursor) if cursor else None}
        if not isinstance(payload, list) or not all(isinstance(r, dict) for r in payload):
            raise FetchError("Expected a JSON array of records", context=where)
        check_page_schema(payload, self._cfg.columns, offset=None)
        if payload and cursor is not None and cursor_of(payload[0]) <= cursor:
            raise FetchError(
                "Source returned a row at or before the paging cursor; "
                "the server is not honouring $order",
                context=where,
            )
        return payload


@dataclass(frozen=True)
class FetchResult:
    pages_total: int
    pages_fetched: int
    rows_fetched: int
    resumed_from_page: int
    already_complete: bool


def _last_cursor(source: KeysetPageSource, store: RawPageStore, index: int) -> Cursor | None:
    """Cursor after page `index - 1`, the last row stored before page `index`."""
    if index == 0:
        return None
    last = None
    for last in store.iter_page(index - 1):
        pass
    if last is None:
        raise FetchError("Cannot resume after an empty page", context={"page": index - 1})
    return source.cursor_of(last)


def fetch_all(
    source: PageSource | KeysetPageSource,
    store: RawPageStore,
    *,
    refresh_tail: bool = False,
) -> FetchResult:
    """Download pages into the store until a short page; resume at the first missing one.

    A store that ends in a short page is complete for a window that is over. For a
    window that is still open (it ends today or later) new cases keep arriving
    after that page was written, so refresh_tail fetches the short last page
    again and continues from there instead of trusting the cache.
    """
    page_size = source.page_size
    keyset = hasattr(source, "fetch_after")
    start = store.completed_pages()
    if start > 0 and store.row_count(start - 1) < page_size:
        if not refresh_tail:
            return FetchResult(start, 0, 0, start, True)
        start -= 1

    index = start
    rows_fetched = 0
    cursor = _last_cursor(source, store, index) if keyset else None
    while True:
        rows = source.fetch_after(cursor) if keyset else source.fetch_page(index * page_size)
        if len(rows) > page_size:
            raise FetchError("Page larger than requested size",
                             context={"page": index, "rows": len(rows)})
        store.write_page(index, rows)
        rows_fetched += len(rows)
        index += 1
        if len(rows) < page_size:
            break
        if keyset:
            cursor = source.cursor_of(rows[-1])
    return FetchResult(index, index - start, rows_fetched, start, False)