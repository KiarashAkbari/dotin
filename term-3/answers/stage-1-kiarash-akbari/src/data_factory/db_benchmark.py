"""Capture repeatable query timings and plans without inventing evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import sqlalchemy as sa
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ClauseElement, Executable

from .config import load_config
from .database import build_database_url

QUERIES = (
    "q1_monthly_category_sales.sql", "q2_delivery_by_state_month.sql",
    "q3_delay_and_satisfaction.sql", "q4_top_sellers.sql",
    "q5_payment_distribution.sql",
)


class Explain(Executable, ClauseElement):
    """SQLAlchemy compiler construct; parameters remain driver-bound."""
    inherit_cache = False

    def __init__(self, statement):
        self.statement = statement


@compiles(Explain, "postgresql")
def compile_explain(element, compiler, **kwargs):
    # Assemble SQL through the compiler, never interpolate parameter values.
    return "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + compiler.process(element.statement, **kwargs)


def consume(connection, statement):
    started = perf_counter()
    digest = hashlib.sha256()
    count = 0
    with connection.execution_options(stream_results=True).execute(statement) as result:
        for rows in result.partitions(1000):
            for row in rows:
                digest.update(json.dumps(list(row), default=str, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                digest.update(b"\n")
                count += 1
    return (perf_counter() - started) * 1000, count, digest.hexdigest()


def benchmark(engine, query_dir, repeats=7, warmups=2):
    if repeats < 3 or warmups < 0:
        raise ValueError("Use at least 3 measurements and nonnegative warmups")
    report = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "method": "Warm-session client elapsed time, execute through chunked fetch and result hashing; not cold-cache timing",
        "warmups": warmups, "repeats": repeats, "budget_ms": 200,
        "queries": {},
    }
    with engine.connect() as connection:
        with connection.begin():
            connection.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            report["postgresql_version"] = connection.scalar(sa.text("SELECT version()"))
            report["settings"] = [dict(row) for row in connection.execute(sa.text(
                "SELECT name, setting, unit FROM pg_settings WHERE name IN "
                "('work_mem', 'shared_buffers', 'jit', 'max_parallel_workers_per_gather', 'random_page_cost') ORDER BY name"
            )).mappings()]
            for filename in QUERIES:
                source = (Path(query_dir) / filename).read_text(encoding="utf-8")
                statement = sa.text(source.strip().removesuffix(";"))
                for _ in range(warmups):
                    consume(connection, statement)
                samples = [consume(connection, statement) for _ in range(repeats)]
                signatures = {(sample[1], sample[2]) for sample in samples}
                if len(signatures) != 1:
                    raise RuntimeError("Query output changed across repeated measurements: " + filename)
                timings = [sample[0] for sample in samples]
                # Streaming is disabled for EXPLAIN, which is a utility command.
                connection.execution_options(stream_results=False)
                plan = connection.execute(Explain(statement)).scalar_one()
                if isinstance(plan, str):
                    plan = json.loads(plan)
                report["queries"][filename] = {
                    "sql_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                    "elapsed_ms": timings,
                    "median_ms": statistics.median(timings),
                    "p95_ms": sorted(timings)[math.ceil(0.95 * len(timings)) - 1],
                    "max_ms": max(timings),
                    "all_measured_runs_under_budget": max(timings) < 200,
                    "rows": samples[0][1], "result_sha256": samples[0][2],
                    "explain_analyze": plan,
                }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--queries", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--warmups", type=int, default=2)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output exists; choose a new path to preserve previous evidence")
    cfg = load_config(args.config)
    engine = sa.create_engine(build_database_url(cfg))
    try:
        report = benchmark(engine, args.queries, args.repeats, args.warmups)
    finally:
        engine.dispose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(args.output)


if __name__ == "__main__":
    main()
