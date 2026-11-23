"""Rolling window: fetch only what was created since the last run."""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Config
from .errors import ConfigError, DataError

ROLLING_DIR = "rolling"
DATE_LENGTH = 10
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"
INCREMENT_MANIFEST = "run_manifest.json"


def rolling_manifest_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(
        f"{manifest_path.stem}.{ROLLING_DIR}{manifest_path.suffix}"
    )


def _read_watermark(path: Path) -> date | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise DataError(f"Manifest is not valid JSON: {path}", context={"path": str(path)}) from exc
    watermark = data.get("watermark") if isinstance(data, dict) else None
    value = watermark.get("max_created_at") if isinstance(watermark, dict) else None
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:DATE_LENGTH])
    except ValueError as exc:
        raise DataError(f"Bad watermark in {path}: {value!r}", context={"path": str(path)}) from exc


def load_watermark(manifest_path: Path) -> date:
    """Latest creation date seen by the previous full or rolling run."""
    found = [
        day
        for day in (
            _read_watermark(rolling_manifest_path(manifest_path)),
            _read_watermark(manifest_path),
        )
        if day is not None
    ]
    if not found:
        raise DataError(
            "No previous run with a watermark found; run the full pipeline first",
            context={"manifest": str(manifest_path)},
        )
    return max(found)


def new_run_id(cfg: Config, now: datetime | None = None) -> str:
    """A sortable UTC timestamp naming one increment; suffixed if that name is taken."""
    stamp = (now or datetime.now(timezone.utc)).strftime(RUN_ID_FORMAT)
    root = cfg.paths.curated_dir / ROLLING_DIR
    run_id, n = stamp, 1
    while (root / run_id).exists() or (cfg.paths.quarantine_dir / ROLLING_DIR / run_id).exists():
        n += 1
        run_id = f"{stamp}-{n}"
    return run_id


def rolling_config(cfg: Config, since: date, today: date, run_id: str | None = None) -> Config:
    """Same pipeline, narrowed to [since, today], writing into its own increment folder.

    Every increment gets curated/rolling/<run_id>/ and quarantine/rolling/<run_id>/,
    with its own manifest inside the curated folder. Earlier increments are never
    overwritten: a shared curated/rolling/ used to keep only the latest increment,
    so rows from the one before were in neither the full outputs nor the rolling
    ones. The latest increment's manifest is also copied to
    run_manifest.rolling.json, which is where the next run reads its watermark.
    """
    if since > today:
        raise ConfigError("Last-run watermark is after today")
    run_id = run_id or new_run_id(cfg)
    curated = cfg.paths.curated_dir / ROLLING_DIR / run_id
    paths = replace(
        cfg.paths,
        curated_dir=curated,
        quarantine_dir=cfg.paths.quarantine_dir / ROLLING_DIR / run_id,
        manifest_path=curated / INCREMENT_MANIFEST,
    )
    return replace(cfg, source=replace(cfg.source, start_date=since, end_date=today), paths=paths)

CREATED_LENGTH = 19


@dataclass(frozen=True)
class Watermark:
    created_at: str
    keys: frozenset[str]


def _read_mark(path: Path) -> Watermark | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    watermark = data.get("watermark") if isinstance(data, dict) else None
    if not isinstance(watermark, dict) or not watermark.get("max_created_at"):
        return None
    keys = watermark.get("keys_at_max_created_at") or []
    return Watermark(str(watermark["max_created_at"]), frozenset(str(k) for k in keys))


def load_mark(manifest_path: Path) -> Watermark | None:
    """Exact position reached by the previous full or rolling run."""
    marks = [
        mark
        for mark in (_read_mark(rolling_manifest_path(manifest_path)), _read_mark(manifest_path))
        if mark is not None
    ]
    if not marks:
        return None
    latest = max(mark.created_at for mark in marks)
    keys = frozenset().union(*(mark.keys for mark in marks if mark.created_at == latest))
    return Watermark(latest, keys)


def after_watermark(
    rows: Iterable[Mapping[str, object]], mark: Watermark | None
) -> Iterator[Mapping[str, object]]:
    """Drop rows the previous run already processed: older rows and ties already seen.

    Rows whose creation time is missing or unreadable are kept so the validator
    quarantines them instead of this filter hiding them.
    """
    for row in rows:
        if mark is None:
            yield row
            continue
        created = str(row.get("created_date") or "")[:CREATED_LENGTH]
        readable = len(created) == CREATED_LENGTH and created[4] == "-"
        if readable:
            if created < mark.created_at:
                continue
            if created == mark.created_at and str(row.get("unique_key", "")).strip() in mark.keys:
                continue
        yield row
