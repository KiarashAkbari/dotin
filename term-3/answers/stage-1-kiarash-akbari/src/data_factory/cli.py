import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa

from .config import Config, ReferenceConfig, load_config
from .database import (
    apply_sql_migrations,
    build_database_url,
    load_olist,
    load_stage1_sink,
    reset_factory_schema,
    reset_olist_schema,
)
from .errors import ConfigError, DataError, PipelineError
from .exit_codes import ExitCode
from .fetch import SocrataSource, fetch_all, query_fingerprint
from .fetch_log import read_requests_total, record_requests
from .http import HttpClient
from .manifest import build_manifest, utc_now_iso, write_manifest
from .pipeline import process
from .population import build_zip_index, fetch_reference, read_reference, write_reference
from .quality import QualityReport, classify_records, enforce_threshold
from .quarantine import VALIDATE_FILENAME, QuarantineWriter
from .rolling import after_watermark, load_mark, load_watermark, rolling_config
from .store import RawPageStore

DEFAULT_CONFIG = "config/default.toml"
COMMANDS = ("run", "fetch", "fetch-reference", "validate", "clean", "load-database")
TOP_UNMATCHED_ZIPS = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="data-factory")
    parser.add_argument("command", nargs="?", default="run", choices=COMMANDS)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument(
        "--skip-sql-migrations",
        action="store_true",
        help="load CSV data without applying configured SQL migrations",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="drop the olist schema, and the factory sink schema unless "
             "--skip-factory-sink is given, before loading data",
    )
    parser.add_argument(
        "--skip-factory-sink",
        action="store_true",
        help="do not load the Stage 1 curated outputs into the factory schema",
    )
    parser.add_argument(
        "--since-last-run",
        action="store_true",
        help="only process records created since the previous run, up to today",
    )
    return parser


def _raw_store(cfg: Config) -> RawPageStore:
    return RawPageStore(cfg.paths.raw_dir / query_fingerprint(cfg.source))


def _reference_config(cfg: Config) -> ReferenceConfig:
    if cfg.reference is None:
        raise ConfigError("Missing config section: [reference]")
    return cfg.reference


def _require_raw(store: RawPageStore) -> None:
    if store.completed_pages() == 0:
        raise DataError(
            "No raw pages found; run 'fetch' first",
            context={"dir": str(store.directory)},
        )


def _fetch(cfg: Config, client: HttpClient) -> None:
    source = SocrataSource.from_env(client, cfg.source)
    store = _raw_store(cfg)
    before = client.request_count
    try:
        result = fetch_all(source, store)
    finally:
        record_requests(store.directory, client.request_count - before)
    print(
        f"pages_total={result.pages_total} pages_fetched={result.pages_fetched} "
        f"rows_fetched={result.rows_fetched} resumed_from_page={result.resumed_from_page} "
        f"requests={client.request_count} dir={store.directory}"
    )


def _fetch_reference(cfg: Config, client: HttpClient) -> None:
    ref = _reference_config(cfg)
    rows = fetch_reference(client, ref)
    write_reference(rows, ref.path)
    print(f"reference_rows={len(rows)} requests={client.request_count} path={ref.path}")


def _format_reasons(report: QualityReport) -> str:
    return ",".join(
        f"{code.value}={count}"
        for code, count in sorted(report.by_reason.items(), key=lambda item: item[0].value)
    ) or "none"


def _validate(cfg: Config) -> None:
    store = _raw_store(cfg)
    _require_raw(store)
    report = QualityReport()
    with QuarantineWriter(cfg.paths.quarantine_dir, VALIDATE_FILENAME) as writer:
        for record in classify_records(store.iter_rows(), report, cfg.source.timezone):
            if not record.is_valid:
                writer.write(record)
    print(
        f"total={report.total} passed={report.passed} quarantined={report.quarantined} "
        f"ratio={report.quarantine_ratio:.6f} reasons={_format_reasons(report)} "
        f"quarantine_file={writer.path}"
    )
    enforce_threshold(
        report,
        max_ratio=cfg.quality.max_quarantine_ratio,
        min_rows=cfg.quality.min_rows_for_threshold,
    )


def _build(cfg: Config, requests: int, started_at: str, mark=None) -> None:
    ref = _reference_config(cfg)
    zip_index = build_zip_index(read_reference(ref.path))
    store = _raw_store(cfg)
    _require_raw(store)
    result = process(after_watermark(store.iter_rows(), mark), cfg, zip_index)
    manifest = build_manifest(
        cfg=cfg,
        result=result,
        requests=requests,
        started_at=started_at,
        finished_at=utc_now_iso(),
        case_requests_total=read_requests_total(store.directory),
    )
    write_manifest(cfg.paths.manifest_path, manifest)
    report, stats = result.report, result.stats
    print(
        f"window={cfg.source.start_date}..{cfg.source.end_date} "
        f"total={report.total} passed={report.passed} quarantined={report.quarantined} "
        f"matched={stats.matched} missing_zip={stats.missing_zip} "
        f"unmatched_zip={stats.unmatched_zip} "
        f"top_unmatched={stats.unmatched_by_zip.most_common(TOP_UNMATCHED_ZIPS)} "
        f"ambiguous={stats.ambiguous} requests={requests} "
        f"manifest={cfg.paths.manifest_path}"
    )


def _run(cfg: Config, mark=None) -> None:
    started_at = utc_now_iso()
    client = HttpClient(cfg.http)
    _fetch(cfg, client)
    ref = _reference_config(cfg)
    if not ref.path.is_file():
        _fetch_reference(cfg, client)
    _build(cfg, client.request_count, started_at, mark)


def _is_base_migration(path: Path) -> bool:
    return path.name[:3] in {"001", "002", "003"}


def _is_snapshot_migration(path: Path) -> bool:
    return path.name[:3] in {"005", "006", "007", "008"}


def _is_sink_migration(path: Path) -> bool:
    """The factory sink schema is its own group, applied right before its data.

    It is deliberately not folded into the base group: the sink is a separate
    destination, and a run that only rebuilds Olist must not need it.
    """
    return path.name[:3] in {"009"}


def _load_database(
    cfg: Config,
    apply_migrations: bool = True,
    reset: bool = False,
    with_sink: bool = True,
) -> None:
    if cfg.database is None:
        raise DataError("Database destination is not configured")

    engine = sa.create_engine(build_database_url(cfg))
    try:
        if reset:
            reset_olist_schema(engine)
            print("schema=reset")
            if with_sink:
                reset_factory_schema(engine)
                print("factory_schema=reset")

        if apply_migrations:
            base_files = apply_sql_migrations(
                engine,
                cfg.database.sql_migrations_dir,
                include=_is_base_migration,
            )
            print(
                f"sql_migrations={len(base_files)} "
                f"files={','.join(base_files)}"
            )
    finally:
        engine.dispose()

    results = load_olist(cfg)
    for table, result in results.items():
        print(f"table={table} loaded={result['loaded']}")

    if not apply_migrations:
        print("sql_migrations=skipped")
        if with_sink:
            print("factory_sink=skipped (the sink needs 009 to be applied)")
        return

    engine = sa.create_engine(build_database_url(cfg))
    try:
        # The sink schema comes first: it is a destination for data, while the
        # snapshot block derives objects from tables that are already loaded.
        sink_files = (
            apply_sql_migrations(
                engine,
                cfg.database.sql_migrations_dir,
                include=_is_sink_migration,
            )
            if with_sink
            else []
        )
        snapshot_files = apply_sql_migrations(
            engine,
            cfg.database.sql_migrations_dir,
            include=_is_snapshot_migration,
        )
    finally:
        engine.dispose()

    if with_sink:
        print(
            f"factory_migrations={len(sink_files)} "
            f"files={','.join(sink_files)}"
        )
    else:
        print("factory_sink=skipped")
    print(
        f"sql_snapshots={len(snapshot_files)} "
        f"files={','.join(snapshot_files)}"
    )
    if not with_sink:
        return
    if not sink_files:
        raise DataError(
            "Factory sink migration is missing",
            context={
                "dir": str(cfg.database.sql_migrations_dir),
                "expected": "009_factory_sink_schema.sql",
            },
        )

    sink = load_stage1_sink(cfg)
    for table, result in sink.items():
        print(f"factory_table={table} loaded={result['loaded']}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        mark = None
        if args.since_last_run:
            today = datetime.now(cfg.source.timezone).date()
            mark = load_mark(cfg.paths.manifest_path)
            cfg = rolling_config(cfg, load_watermark(cfg.paths.manifest_path), today)
        if args.command == "run":
            _run(cfg, mark)
        elif args.command == "fetch":
            _fetch(cfg, HttpClient(cfg.http))
        elif args.command == "fetch-reference":
            _fetch_reference(cfg, HttpClient(cfg.http))
        elif args.command == "validate":
            _validate(cfg)
        elif args.command == "load-database":
            _load_database(
                cfg,
                apply_migrations=not args.skip_sql_migrations,
                reset=args.reset,
                with_sink=not args.skip_factory_sink,
            )
        else:
            _build(cfg, 0, utc_now_iso(), mark)
        return int(ExitCode.OK)
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.context:
            print(
                json.dumps(exc.context, ensure_ascii=False, sort_keys=True),
                file=sys.stderr,
            )
        return int(exc.exit_code)