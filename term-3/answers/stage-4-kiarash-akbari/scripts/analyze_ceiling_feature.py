#!/usr/bin/env python3
"""Answer the question the ceiling's error line asks, instead of leaving it as a task.

``capstone features`` judges the *fastest* of five timed repeats against the brief's 500 ms and, when
it fails, prints: "Store the EXPLAIN (ANALYZE) output with the number and fix the query". Storing that
output was a sentence in an error message and nothing else. This script is the missing half: it writes
the plan, twice, around the one intervention that costs nothing to try.

Why *around*: the bridge (`sql/006`) rebuilds ``capstone.mv_external_density`` on every apply, and a
refreshed materialised view starts with **no** statistics. The join then picks its shape from a table
the planner believes to be empty -- which is a measurement artefact, not a property of the query, and
it looks exactly like a slow frame. Measured on a live database: 477 ms on a table holding two censuses,
663.4 ms after narrowing it to the registered one, with no change to the frame's definition.

So the script measures the frame, runs ``ANALYZE`` on the three relations the bridge owns, measures
again, and prints the verdict twice -- once per state. If the failure survives the ANALYZE, it is a
query finding and `report.md` has to say so; if it does not, the number in the manifest was an artefact
and the fix is to keep statistics warm, not to raise the ceiling.

Run from the stage folder, after the chain reached `features` once::

    python scripts/analyze_ceiling_feature.py --config config/capstone.default.toml

Writes ``docs/evidence/ceiling-feature-analysis.txt`` (+ ``.json``) and refuses to overwrite a witness
whose header names a different ceiling or a different frame row count, for the same reason
`scripts/cheating_model_gap.py` refuses a witness from another split.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))

#: the relations whose statistics the bridge changes; the frame last, so it is planned against columns
#: the planner has actually seen. The names are literals because this module must import without a
#: database driver -- `capstone.features` holds the truth, and a test pins these three to it.
FRAME_VIEW = "capstone.v_feature_frame"
ANALYZE_TARGETS = ("capstone.external_municipio", "capstone.mv_external_density", FRAME_VIEW)

PLAN_SQL = "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) SELECT * FROM {frame}"
TIMING_SQL = "EXPLAIN (ANALYZE, TIMING OFF) SELECT * FROM {frame}"


def measure(cursor, frame: str, *, repeat: int, clock) -> list[float]:
    """The same statistic the feature step judges: wall clock of the full frame query, per repeat.

    Deliberately a copy of the loop in `capstone.features.build` rather than a call into it --
    `build` refuses to return until the ceiling is met, and the whole point here is to look at a state
    that fails.
    """
    durations: list[float] = []
    for _ in range(repeat):
        started = clock()
        cursor.execute(TIMING_SQL.format(frame=frame))
        cursor.fetchall()
        durations.append((clock() - started) * 1000.0)
    return durations


def verdict(durations: list[float], ceiling_ms: float) -> dict:
    """One state's numbers, judged on the basis the project fixed in `features.TIMING_BASIS`."""
    best = min(durations)
    return {
        "runs_ms": [round(value, 3) for value in durations],
        "best_ms": round(best, 3),
        "mean_ms": round(sum(durations) / len(durations), 3),
        "worst_ms": round(max(durations), 3),
        "ceiling_ms": ceiling_ms,
        "passes": best <= ceiling_ms,
    }


def describe(before: dict, after: dict) -> str:
    """The sentence the report needs, in the shape of a decision rather than a number."""
    if before["passes"]:
        return (
            f"finding: the frame already meets the ceiling ({before['best_ms']:.1f} ms over "
            f"{len(before['runs_ms'])} repeats); this run is context, not a fix"
        )
    if after["passes"]:
        return (
            f"artefact: {before['best_ms']:.1f} ms becomes {after['best_ms']:.1f} ms after ANALYZE on "
            f"{len(ANALYZE_TARGETS)} relations, so the breach was a stale plan on the rebuilt "
            "snapshot, not a cost of the frame -- the remedy is to keep those statistics warm"
        )
    return (
        f"query finding: ANALYZE moves the fastest repeat from {before['best_ms']:.1f} ms to "
        f"{after['best_ms']:.1f} ms and the ceiling still fails, so `report.md` has to carry this as a "
        "measured shortfall and the plan below is the starting point"
    )


def main(argv: list[str] | None = None) -> int:
    import time

    from capstone import features
    from capstone.cli import default_config_path
    from capstone.config import load_config
    from capstone.features import connect, database_uri

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=default_config_path(STAGE))
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--evidence",
        type=Path,
        default=STAGE / "docs/evidence/ceiling-feature-analysis.txt",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite a witness measured at a different ceiling or row count",
    )
    args = parser.parse_args(argv)
    if args.repeats < 2:
        print("error: --repeats needs at least 2; one number cannot show a worst case", file=sys.stderr)
        return 2

    config = load_config(args.config)
    ceiling = float(config.features.feature_query_ms)
    frame = features.FRAME_VIEW

    previous = None
    if args.evidence.exists():
        # the guard is about *what the file claims*; `--force` is consulted below it, so it stays the
        # only way past
        head = args.evidence.read_text(encoding="utf-8")[:2000]
        previous = re.search(r"ceiling (\d+) ms; frame rows (\d+)", head)
    connection = connect(database_uri(config), purpose="measure the frame across an ANALYZE")
    try:
        cursor = connection.cursor()
        cursor.execute(f"SELECT count(*) FROM {frame}")
        rows = int(cursor.fetchone()[0])
        if previous and not args.force and (
            int(previous.group(1)) != int(ceiling) or int(previous.group(2)) != rows
        ):
            print(
                f"refusing to overwrite {args.evidence.name}: it records ceiling {previous.group(1)} ms "
                f"on {previous.group(2)} rows, and this run uses {int(ceiling)} ms on {rows} rows.\n"
                "the two are different measurements of a different frame -- pass --force if the new "
                "state is the decision"
            )
            return 2

        before_runs = measure(cursor, frame, repeat=args.repeats, clock=time.perf_counter)
        connection.commit()
        for target in ANALYZE_TARGETS:
            if target == "capstone.mv_external_density":
                cursor.execute("SELECT to_regclass('capstone.mv_external_density') IS NOT NULL")
                if not cursor.fetchone()[0]:
                    continue  # no bridge yet: there is nothing here to have stale statistics about
            cursor.execute(f"ANALYZE {target}")
        connection.commit()
        after_runs = measure(cursor, frame, repeat=args.repeats, clock=time.perf_counter)
        cursor.execute(PLAN_SQL.format(frame=frame))
        plan = "\n".join(str(row[0]) for row in cursor.fetchall())
    finally:
        connection.close()

    before = verdict(before_runs, ceiling)
    after = verdict(after_runs, ceiling)
    lines = [
        "# ceiling on the feature frame, measured across one ANALYZE (scripts/analyze_ceiling_feature.py)",
        f"config {args.config.name}; frame {frame}; ceiling {int(ceiling)} ms; frame rows {rows}",
        f"basis: {features.TIMING_BASIS}, the same statistic the feature step judges on",
        "",
        f"before ANALYZE: best {before['best_ms']:.1f} ms, mean {before['mean_ms']:.1f} ms, "
        f"worst {before['worst_ms']:.1f} ms, runs {before['runs_ms']}",
        f"after  ANALYZE: best {after['best_ms']:.1f} ms, mean {after['mean_ms']:.1f} ms, "
        f"worst {after['worst_ms']:.1f} ms, runs {after['runs_ms']}",
        f"analyzed: {', '.join(ANALYZE_TARGETS)}",
        "",
        describe(before, after),
        "",
        "## the plan the error line asked for, read after the ANALYZE",
        plan,
    ]
    args.evidence.write_text("\n".join(lines) + "\n", encoding="utf-8")
    args.evidence.with_suffix(".json").write_text(
        json.dumps(
            {
                "config": str(args.config),
                "ceiling_ms": ceiling,
                "frame_rows": rows,
                "before": before,
                "after": after,
                "verdict": describe(before, after),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\n".join(lines))
    return 0 if after["passes"] or before["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
