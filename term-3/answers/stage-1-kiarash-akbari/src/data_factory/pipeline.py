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
from .publish import OutputStaging
from .quality import ClassifiedRecord, QualityReport, classify_records, enforce_threshold
from .quarantine import FAILED_FILENAME, QuarantineWriter

OUTPUT_DATASET = "dataset"
OUTPUT_WIDE = "wide_table"
OUTPUT_QUARANTINE = "quarantine"
OUTPUT_QUALITY = "quality_report"
# The dataset goes last among the data files: a reader that only looks for
# cases.csv never sees it before the files derived from the same run.
PUBLISH_ORDER = (OUTPUT_QUARANTINE, OUTPUT_QUALITY, OUTPUT_WIDE, OUTPUT_DATASET)


@dataclass(frozen=True)
class ProcessResult:
    report: QualityReport
    stats: JoinStats
    output_paths: dict[str, Path]
    # Set while the files still wait in staging (process(..., publish=False)).
    staging: OutputStaging | None = None

    def readable_path(self, name: str) -> Path:
        """Where the bytes of an output are right now: staged or published."""
        final = self.output_paths[name]
        return self.staging.path_for(final) if self.staging is not None else final

    def publish(self) -> "ProcessResult":
        """Rename the staged set onto the final names; a no-op once published."""
        if self.staging is None:
            return self
        quarantine = self.output_paths[OUTPUT_QUARANTINE]
        self.staging.commit(
            [self.output_paths[name] for name in PUBLISH_ORDER],
            remove=[quarantine.with_name(FAILED_FILENAME)],
        )
        return ProcessResult(self.report, self.stats, self.output_paths)


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
    *,
    allow_empty: bool = False,
    publish: bool = True,
) -> ProcessResult:
    """One streaming pass into staging, then the whole output set is published.

    A failed run keeps its quarantine as quarantine.failed.jsonl and leaves the last
    good outputs untouched. With publish=False the files stay staged so the caller
    can hash them into the manifest and call result.publish() itself.
    """
    zone = cfg.source.timezone
    report, stats = QualityReport(), JoinStats()
    aggregator = AreaMonthAggregator(
        months_between(cfg.source.start_date, cfg.source.end_date)
    )
    profile = QualityProfile(CLEAN_COLUMNS)
    curated_dir, quarantine_dir = cfg.paths.curated_dir, cfg.paths.quarantine_dir
    dataset_path = curated_dir / DATASET_FILENAME
    wide_path = curated_dir / WIDE_FILENAME
    quality_path = curated_dir / REPORT_FILENAME
    staging = OutputStaging(curated_dir, quarantine_dir)
    try:
        result = _process_into(
            rows, cfg, zip_index, staging, report, stats, aggregator, profile, zone,
            allow_empty,
        )
    except QuarantineThresholdError:
        failed = staging.folder(quarantine_dir) / FAILED_FILENAME
        if failed.exists():
            staging.move_out(failed, quarantine_dir / FAILED_FILENAME)
        staging.discard()
        raise
    except BaseException:
        staging.discard()
        raise
    pending = ProcessResult(
        report,
        stats,
        {
            OUTPUT_DATASET: dataset_path,
            OUTPUT_WIDE: wide_path,
            OUTPUT_QUARANTINE: quarantine_dir / result,
            OUTPUT_QUALITY: quality_path,
        },
        staging,
    )
    return pending.publish() if publish else pending


def _process_into(
    rows, cfg, zip_index, staging, report, stats, aggregator, profile, zone, allow_empty
) -> str:
    """Write every output into staging; returns the quarantine file's name."""
    curated = staging.folder(cfg.paths.curated_dir)
    threshold_error: QuarantineThresholdError | None = None
    with QuarantineWriter(staging.folder(cfg.paths.quarantine_dir)) as quarantine:
        try:
            with DatasetWriter(curated / DATASET_FILENAME) as dataset:
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
                    allow_empty=allow_empty,
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
    write_wide(curated / WIDE_FILENAME, aggregator)
    write_profile(curated / REPORT_FILENAME, profile, extra)
    return quarantine.path.name
