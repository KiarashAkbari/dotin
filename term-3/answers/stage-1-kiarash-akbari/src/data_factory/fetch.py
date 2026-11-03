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
FINGERPRINT_LENGTH = 12


class PageSource(Protocol):
    page_size: int

    def fetch_page(self, offset: int) -> list[dict]: ...


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_where(cfg: SourceConfig) -> str:
    types = ", ".join(_quote(t) for t in cfg.complaint_types)
    start = f"{cfg.start_date.isoformat()}T00:00:00"
    end_exclusive = f"{(cfg.end_date + timedelta(days=1)).isoformat()}T00:00:00"
    return (
        f"agency = {_quote(cfg.agency)} AND complaint_type in({types}) "
        f"AND created_date >= {_quote(start)} AND created_date < {_quote(end_exclusive)}"
    )


def build_params(cfg: SourceConfig, offset: int) -> dict:
    return {
        "$select": ",".join(cfg.columns),
        "$where": build_where(cfg),
        "$order": ORDER_BY,
        "$limit": cfg.page_size,
        "$offset": offset,
    }


def query_fingerprint(cfg: SourceConfig) -> str:
    params = build_params(cfg, 0)
    params.pop("$offset")
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

    def fetch_page(self, offset: int) -> list[dict]:
        payload = self._client.get_json(
            self._cfg.url, params=build_params(self._cfg, offset), headers=self._headers
        )
        if not isinstance(payload, list) or not all(isinstance(r, dict) for r in payload):
            raise FetchError("Expected a JSON array of records",
                             context={"offset": offset})
        check_page_schema(payload, self._cfg.columns, offset=offset)
        return payload


@dataclass(frozen=True)
class FetchResult:
    pages_total: int
    pages_fetched: int
    rows_fetched: int
    resumed_from_page: int
    already_complete: bool


def fetch_all(source: PageSource, store: RawPageStore) -> FetchResult:
    page_size = source.page_size
    start = store.completed_pages()
    if start > 0 and store.row_count(start - 1) < page_size:
        return FetchResult(start, 0, 0, start, True)

    index = start
    rows_fetched = 0
    while True:
        rows = source.fetch_page(index * page_size)
        if len(rows) > page_size:
            raise FetchError("Page larger than requested size",
                             context={"offset": index * page_size, "rows": len(rows)})
        store.write_page(index, rows)
        rows_fetched += len(rows)
        index += 1
        if len(rows) < page_size:
            break
    return FetchResult(index, index - start, rows_fetched, start, False)