"""Second source: population per ZIP code from the NYC MODZCTA reference table."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import ReferenceConfig
from .errors import DataError, FetchError

AREA_COLUMN = "modzcta"
ZIP_LIST_COLUMN = "label"
POPULATION_COLUMN = "pop_est"
ZIP_SEPARATOR = ","
TMP_SUFFIX = ".tmp"


class JsonClient(Protocol):
    def get_json(
        self, url: str, params: dict | None = None, headers: dict | None = None
    ) -> list | dict: ...


@dataclass(frozen=True)
class ZipArea:
    modzcta: str
    population: int | None


def fetch_reference(client: JsonClient, cfg: ReferenceConfig) -> list[dict]:
    payload = client.get_json(
        cfg.url,
        params={
            "$select": ",".join(cfg.columns),
            "$order": AREA_COLUMN,
            "$limit": cfg.limit,
        },
    )
    if not isinstance(payload, list) or not all(isinstance(r, dict) for r in payload):
        raise FetchError(
            "Expected a JSON array of reference records", context={"url": cfg.url}
        )
    if len(payload) >= cfg.limit:
        raise FetchError(
            "Reference table may be truncated; raise reference.limit",
            context={"url": cfg.url, "rows": len(payload), "limit": cfg.limit},
        )
    return payload


def write_reference(rows: list[dict], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(rows, fh, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_reference(path: Path) -> list[dict]:
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            rows = json.load(fh)
    except FileNotFoundError as exc:
        raise DataError(
            f"Reference file not found: {path}; run 'fetch-reference' first",
            context={"path": str(path)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise DataError(
            f"Reference file is not valid JSON: {path}", context={"path": str(path)}
        ) from exc
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise DataError(
            "Reference file must hold a JSON array of records",
            context={"path": str(path)},
        )
    return rows


def _parse_population(value: object, area: str) -> int | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError) as exc:
        raise DataError(
            f"Unparseable population for area {area}: {value!r}",
            context={"area": area},
        ) from exc
    return number if number > 0 else None


def build_zip_index(rows: list[dict]) -> dict[str, ZipArea]:
    """Map each ZIP code to its area; a ZIP belonging to two areas is a data error."""
    index: dict[str, ZipArea] = {}
    for row in rows:
        area = str(row.get(AREA_COLUMN) or "").strip()
        if not area:
            raise DataError("Reference row without an area code", context={"row": row})
        population = _parse_population(row.get(POPULATION_COLUMN), area)
        for raw_zip in str(row.get(ZIP_LIST_COLUMN) or "").split(ZIP_SEPARATOR):
            zip_code = raw_zip.strip()
            if not zip_code:
                continue
            known = index.get(zip_code)
            if known is not None and known.modzcta != area:
                raise DataError(
                    f"ZIP {zip_code} belongs to two areas",
                    context={"zip": zip_code, "areas": [known.modzcta, area]},
                )
            index[zip_code] = ZipArea(area, population)
    return index