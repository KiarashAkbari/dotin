"""Source-agnostic processing: classify, clean, join and write the final outputs."""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from .clean import CLEAN_COLUMNS, JoinStats, clean_stream
from .config import Config
from .errors import QuarantineThresholdError
from .outputs import (
    DATASET_FILENAME,
    WIDE_FILENAME,
    AreaMonthAggregator,
    DatasetWriter,
    months_between,
    write_wide,
)
from .population import ZipArea
from .profile import REPORT_FILENAME, QualityProfile, write_profile
from .quality import ClassifiedRecord, QualityReport, classify_records, enforce_threshold
from .quarantine import QuarantineWriter

OUTPUT_DATASET = "dataset"
OUTPUT_WIDE = "wide_table"
OUTPUT_QUARANTINE = "quarantine"
OUTPUT_QUALITY = "quality_report"


@dataclass(frozen=True)
class ProcessResult:
    report: QualityReport
    stats: JoinStats
    output_paths: dict[str, Path]


def _tee_quarantine(
    records: Iterable[ClassifiedRecord], writer: QuarantineWriter
) -> Iterator[ClassifiedRecord]:
    for record in records:
        if not record.is_valid:
            writer.write(record)
        yield record


def _borough_conflicts(stats: JoinStats) -> list[dict]:
    return [
        {"modzcta": modzcta, "boroughs": sorted(areas)}
        for modzcta, areas in stats.borough_by_modzcta.items()
        if len(areas) > 1
    ]


def process(
    rows: Iterable[Mapping[str, object]],
    cfg: Config,
    zip_index: Mapping[str, ZipArea],
) -> ProcessResult:
    """One streaming pass; a failed run keeps its quarantine as quarantine.failed.jsonl and leaves the last good outputs untouched."""
    zone = cfg.source.timezone
    report, stats = QualityReport(), JoinStats()
    aggregator = AreaMonthAggregator(
        months_between(cfg.source.start_date, cfg.source.end_date)
    )
    profile = QualityProfile(CLEAN_COLUMNS)
    dataset_path = cfg.paths.curated_dir / DATASET_FILENAME
    wide_path = cfg.paths.curated_dir / WIDE_FILENAME
    quality_path = cfg.paths.curated_dir / REPORT_FILENAME

    threshold_error: QuarantineThresholdError | None = None
    with QuarantineWriter(cfg.paths.quarantine_dir) as quarantine:
        try:
            with DatasetWriter(dataset_path) as dataset:
                classified = _tee_quarantine(
                    classify_records(rows, report, zone), quarantine
                )
                for cleaned in clean_stream(classified, zone, zip_index, stats):
                    dataset.write(cleaned)
                    aggregator.add(cleaned)
                    profile.add(cleaned)
                enforce_threshold(
                    report,
                    max_ratio=cfg.quality.max_quarantine_ratio,
                    min_rows=cfg.quality.min_rows_for_threshold,
                )
        except QuarantineThresholdError as exc:
            threshold_error = exc
            quarantine.mark_failed()
    if threshold_error is not None:
        raise threshold_error

    conflicts = _borough_conflicts(stats)
    extra = {
        "borough_modzcta_conflicts": conflicts,
        "borough_modzcta_conflict_count": len(conflicts),
    }
    write_wide(wide_path, aggregator)
    write_profile(quality_path, profile, extra)
    return ProcessResult(
        report,
        stats,
        {
            OUTPUT_DATASET: dataset_path,
            OUTPUT_WIDE: wide_path,
            OUTPUT_QUARANTINE: quarantine.path,
            OUTPUT_QUALITY: quality_path,
        },
    )
