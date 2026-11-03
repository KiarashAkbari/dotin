"""Run manifest: what was fetched, what passed, what was quarantined, and file hashes."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from .config import MAX_SOCRATA_PAGE, Config
from .fetch_log import minimum_requests
from .outputs import sha256_file
from .pipeline import ProcessResult

PACKAGE_NAME = "data-factory"
UNKNOWN_VERSION = "unknown"
TMP_SUFFIX = ".tmp"


def _git_revision() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def code_version() -> str:
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        revision = _git_revision()
        return f"git:{revision}" if revision else UNKNOWN_VERSION


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_manifest(
    *,
    cfg: Config,
    result: ProcessResult,
    requests: int,
    started_at: str,
    finished_at: str,
    case_requests_total: int | None = None,
) -> dict:
    report, stats = result.report, result.stats
    reference_url = cfg.reference.url if cfg.reference else None
    return {
        "code_version": code_version(),
        "started_at": started_at,
        "finished_at": finished_at,
        "window": {
            "start_date": cfg.source.start_date.isoformat(),
            "end_date": cfg.source.end_date.isoformat(),
            "timezone": cfg.source.timezone.key,
        },
        "watermark": {
            "max_created_at": stats.max_created_at,
            "keys_at_max_created_at": sorted(stats.keys_at_max_created),
        },
        "restriction": {
            "agency": cfg.source.agency,
            "complaint_types": list(cfg.source.complaint_types),
        },
        "sources": {"cases": cfg.source.url, "population": reference_url},
        "records": {
            "raw": report.total,
            "passed": report.passed,
            "quarantined": report.quarantined,
            "quarantined_by_reason": {
                code.value: count
                for code, count in sorted(
                    report.by_reason.items(), key=lambda item: item[0].value
                )
            },
        },
        "join": {
            "rows_in": stats.rows_in,
            "rows_out": stats.rows_out,
            "matched": stats.matched,
            "missing_zip": stats.missing_zip,
            "unmatched_zip": stats.unmatched_zip,
            "unmatched_distinct_zips": len(stats.unmatched_by_zip),
            "ambiguous_timestamps": stats.ambiguous,
        },
        "requests_this_run": requests,
        "request_budget": {
            "page_size": cfg.source.page_size,
            "max_page_size_allowed": MAX_SOCRATA_PAGE,
            "raw_records": report.total,
            "minimum_case_requests": minimum_requests(report.total, cfg.source.page_size),
            "case_requests_total": case_requests_total,
        },
        "outputs": {
            name: {"path": path.as_posix(), "sha256": sha256_file(path)}
            for name, path in sorted(result.output_paths.items())
        },
    }


def write_manifest(path: Path, manifest: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, sort_keys=True, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)