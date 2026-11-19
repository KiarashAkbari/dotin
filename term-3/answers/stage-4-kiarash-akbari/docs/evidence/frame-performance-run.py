#!/usr/bin/env python3
"""Time the feature frame on a real server, keep the plan, and name where the seconds go.

Checklist 5.3 asks for two things the offline suite cannot give: a repeatable duration for the
query that builds the frame, and its ``EXPLAIN (ANALYZE)`` plan. ``capstone features`` already owns
the first one -- :func:`capstone.features.build` runs the timed statement ``repeat`` times and
judges the mean against ``features.feature_query_ms`` -- but it prints a single number and then
forgets the
plan, so a reader told "412 ms" cannot see *why*, and a reader told "over the ceiling" cannot see
where to cut. This script writes both down.

What it reports, in order, and why each line is there:

* the tree it ran on, with the repository named -- a bare short hash is a dead link as soon as a log
  crosses trees, which is the mistake patch 0011 exists to stop repeating;
* the database, the server version, and the row counts of the ``olist`` tables the frame reads, so a
  number on two rows can never be read as a number on ninety-nine thousand;
* the frame's shape: rows, columns, feature columns against ``MINIMUM_FEATURES``;
* the timing, on the statement the code times (``FRAME_TIMING_SQL``) and on the same frame with the
  client transfer included (``FRAME_READ_SQL``). The pair is the honest version of "under 500 ms":
  the ceiling is written about the query, and the transfer is what a person actually waits for;
* ``EXPLAIN (ANALYZE, BUFFERS)`` for both, in text and JSON, with each plan's hottest nodes ranked by
  *self* time -- a nested loop whose child does the work is not the bottleneck the way its parent's
  total makes it look;
* a verdict and, when the plan says so, the shortest list of things that would actually change it.

It never writes. There is no ``CREATE``, no ``INSERT``, no ``ANALYZE`` statement here: a scratch
cluster that needs statistics is a fact about the machine, and the script says out loud when the
plan it captured was built on tables the planner has never seen.

Run from ``stage-4/`` with the driver the URI asks for::

    CAPSTONE_DATABASE_URI="postgresql://…" \
      python3 docs/evidence/frame-performance-run.py | tee docs/evidence/frame-performance-run.txt

With no ``CAPSTONE_DATABASE_URI`` it prints what is missing and exits 0: a laptop without the
project's database is not a failed measurement, and a log full of tracebacks teaches nobody anything.
``--runs N`` repeats the timing more often (the brief's protocol is warm and cold, so the first run
is labelled cold). Nothing is written beside the log: the log is the artifact, and a second file of
the same numbers is a second thing to keep true.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

STAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(STAGE / "src"))

from capstone.features import (  # noqa: E402  (the point of the import is the shared constants)
    FRAME_COLUMNS_SQL,
    FRAME_COUNT_SQL,
    FRAME_READ_SQL,
    FRAME_TIMING_SQL,
    FRAME_VIEW,
    MINIMUM_FEATURES,
    TECHNICAL_COLUMNS,
    connect,
    database_uri,
)
# `load_contract`, not `load_config`: the contract loader refuses an undecided student decision,
# and a performance run on a laptop where `[external]` is still empty is a measurement worth having
# -- the frame does not depend on that section, and refusing here would hide the ceiling verdict
# behind an unrelated open question
from capstone.config import load_contract  # noqa: E402

#: a plan node whose self time dominates, and the shortest honest answer for it. The keys are node
#: types PostgreSQL prints; the text is what a reader of the log gets, not a guess about their
#  schema.
ADVICE = {
    "Hash Join": "a hash join dominating the plan usually means one side is bigger than work_mem allows",
    "Merge Join": "a merge join dominating means both sides arrive sorted; an index on the join key of"
    " the smaller side can remove the sort, not the join",
    "Sort": "a sort dominates; work_mem, or an index that provides the order the window already wants",
    "WindowAgg": "the window is the cost; a materialized frame refresh moves it out of the request path",
    "Seq Scan": "a sequential scan of a table the filter should narrow; the index to add is the filter's",
    "Gather": "parallelism is already engaged, so the remaining cost is real work, not scheduling",
}


def repo_name() -> str:
    """Which repository the captured commit belongs to, read from ``origin``."""
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=STAGE, capture_output=True, text=True
    ).stdout.strip().rstrip("/")
    if url:
        tail = url.rsplit("/", 2)[-2:]
        tail[-1] = tail[-1][: -len(".git")] if tail[-1].endswith(".git") else tail[-1]
        if any(part in ("github.com", "gitlab.com", "localhost", "~") for part in tail):
            tail = tail[-1:]
        return "/".join(part for part in tail if part)
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=STAGE, capture_output=True, text=True
    ).stdout.strip()
    return Path(toplevel).name if toplevel else "unknown repository"


def tree_state() -> str:
    """The commit and whether the tree differs from it -- the same honesty rule as the smoke log."""
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=STAGE, capture_output=True, text=True
    ).stdout.strip()
    state = subprocess.run(
        ["git", "status", "--porcelain", "-uno"], cwd=STAGE, capture_output=True, text=True
    ).stdout.splitlines()
    own = f"stage-4/{Path(__file__).relative_to(STAGE)}"
    tracked = [line for line in state if own not in line]
    label = "no tracked file differs from HEAD" if not tracked else f"{len(tracked)} tracked file(s) differ"
    return f"tree: {repo_name()} @ HEAD {commit}, {label} -- the plan below is that tree's plan"


def rows(cursor: Any, sql: str, params: tuple = ()) -> list[tuple]:
    """Run one read and hand back the rows; the whole script is this plus formatting."""
    cursor.execute(sql, params)
    return list(cursor.fetchall())


def source_counts(cursor: Any) -> list[tuple[str, int]]:
    """Row counts of the tables the frame reads, skipping the ones this server does not have."""
    found = rows(
        cursor,
        "SELECT table_name FROM information_schema.columns"
        " WHERE table_schema = 'olist' GROUP BY table_name",
    )
    names = sorted(str(name) for (name,) in found)
    out: list[tuple[str, int]] = []
    for table in names:
        out.append((table, int(rows(cursor, f'SELECT count(*) FROM olist."{table}"')[0][0])))
    return out


def frame_shape(cursor: Any) -> dict:
    """Rows, columns and feature columns of the frame, against the constants the code uses."""
    schema, _, view = FRAME_VIEW.partition(".")
    columns = [str(r[0]) for r in rows(cursor, FRAME_COLUMNS_SQL, (schema, view))]
    if not columns:
        return {"rows": 0, "columns": 0, "features": 0, "technical": 0, "missing": True}
    count = int(rows(cursor, FRAME_COUNT_SQL.format(frame=FRAME_VIEW))[0][0])
    features = [name for name in columns if name not in TECHNICAL_COLUMNS]
    return {
        "rows": count,
        "columns": len(columns),
        "features": len(features),
        "technical": len(columns) - len(features),
        "missing": False,
        "names": columns,
    }


def stats_note(cursor: Any) -> str:
    """Whether the planner has ever seen these tables, in one line the log can be read with.

    A plan is a decision made from statistics. On a scratch cluster that nobody analysed, PostgreSQL
    guesses, and a 400 ms guess is not a fact about the query -- so the log states which case it is in
    instead of leaving the reader to know how the cluster was built.
    """
    read, analysed, stamp = rows(
        cursor,
        "SELECT count(*),"
        " count(*) FILTER (WHERE last_analyze IS NOT NULL OR last_autoanalyze IS NOT NULL),"
        " GREATEST(max(last_analyze), max(last_autoanalyze))::text"
        " FROM pg_stat_user_tables WHERE schemaname = 'olist'",
    )[0]
    if not read:
        return "unknown -- this server has no olist tables in pg_stat_user_tables to ask"
    if int(analysed) == int(read):
        return f"current: all {int(read)} olist tables have been ANALYZEd (last: {stamp})"
    return (
        f"{int(read) - int(analysed)} of {int(read)} olist tables have never been ANALYZEd, so their"
        " row estimates are guesses -- the timings are still real, the plan beside them is partly"
        " not"
    )


def timed(cursor: Any, sql: str, runs: int) -> dict:
    """One statement, ``runs`` times, milliseconds per run -- cold first, then warm.

    No cache clearing is attempted. A script that cannot ``echo 3 > drop_caches`` reporting a
    "cold" number would be inventing the interesting part of the protocol, so the runs are labelled
    by order instead: the first is the one that pays whatever the rest get for free.
    """
    samples: list[float] = []
    for _ in range(max(1, runs)):
        started = time.perf_counter()
        cursor.execute(sql)
        for _row in cursor:
            pass
        samples.append(round((time.perf_counter() - started) * 1000.0, 1))
    return {
        "sql": sql,
        "runs": len(samples),
        "cold_ms": samples[0],
        "warm_ms": round(sum(samples[1:]) / len(samples[1:]), 1) if len(samples) > 1 else None,
        "mean_ms": round(sum(samples) / len(samples), 1),
        "min_ms": min(samples),
        "max_ms": max(samples),
        "samples_ms": samples,
    }


def explain(cursor: Any, sql: str, *, fmt: str) -> Any:
    """``EXPLAIN (ANALYZE, BUFFERS)`` in the requested format, unwrapped from the driver's shape.

    The two formats come back in opposite shapes and the first draft got that wrong: JSON is one row
    holding one value, text is *one row per plan line*. Reading the text plan with ``fetchone`` printed
    the root node and nothing else -- a log that shows ``Aggregate 483 ms`` with no children is worse
    than no plan, because it looks like evidence.
    """
    cursor.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT {fmt}) {sql}")
    if fmt == "json":
        payload = cursor.fetchone()[0]
        return json.loads(payload) if isinstance(payload, str) else payload
    return "\n".join(str(row[0]) for row in cursor.fetchall())


def self_time(node: dict) -> float:
    """What this node cost, subtracting the child that actually did the work.

    PostgreSQL reports ``Actual Total Time`` per node, which is inclusive: the root's total is the
    query's total. Ranking by it would always name the root, so the ranking here is over the
    difference -- and a parent slower than its slowest child is the operator worth reading about.
    """
    total = float(node.get("Actual Total Time", 0.0) or 0.0)
    children = node.get("Plans") or []
    child = max((float(c.get("Actual Total Time", 0.0) or 0.0) for c in children), default=0.0)
    return round(total - child, 3)


def hotspots(plan: dict, *, limit: int = 3) -> list[dict]:
    """The plan's costliest nodes by self time, flattened, with their share of the total.

    Written as a pure function over the JSON so it is testable without a server: the log is only
    worth keeping if the sentence under it -- "the sort is the cost" -- can be checked against the
    plan it came from by somebody who has no database open.
    """
    top = plan[0] if isinstance(plan, list) else plan
    root = top.get("Plan") or {}
    total = float(root.get("Actual Total Time", 0.0) or 0.0)
    found: list[dict] = []

    def walk(node: dict) -> None:
        found.append(
            {
                "node": str(node.get("Node Type", "?")),
                "relation": str(node.get("Relation Name") or ""),
                "self_ms": self_time(node),
                "rows": int(node.get("Actual Rows", 0) or 0),
                "loops": int(node.get("Actual Loops", 1) or 1),
                "shared_hit": int(node.get("Shared Hit Blocks", 0) or 0),
                "shared_read": int(node.get("Shared Read Blocks", 0) or 0),
            }
        )
        for child in node.get("Plans") or []:
            walk(child)

    walk(root)
    found.sort(key=lambda item: item["self_ms"], reverse=True)
    for item in found[:limit]:
        item["share"] = round(item["self_ms"] / total, 3) if total > 0 else None
    return found[:limit]


def spill(plan: dict) -> dict:
    """What the plan paid outside RAM: temp blocks, and which operator actually spilled.

    This is the line that makes a sub-ceiling measurement actionable. A query at 390 ms of a 500 ms
    ceiling on a cluster with a small ``work_mem`` is a different machine than the same query at 390 ms
    on one that never touches temp, and the difference is the first thing a reader needs before deciding
    whether *their* server is under the ceiling too. Reported as a fact in both cases: only the verdict
    section proposes a change.

    The totals are read off the root, not summed over the nodes. PostgreSQL's per-node ``Buffers`` and
    temp counters are inclusive of the children, exactly like ``Actual Total Time``, and the same
    mistake was made here once already (see ``self_time``): summing a Gather, its child and the hash
    under it reports the same blocks three times, and a plan that spilled 21 MB reads as 63 MB.
    """
    top = plan[0] if isinstance(plan, list) else plan
    root = top.get("Plan") or top
    read = int(top.get("Temp Read Blocks", root.get("Temp Read Blocks", 0)) or 0)
    written = int(top.get("Temp Written Blocks", root.get("Temp Written Blocks", 0)) or 0)
    batches: int | None = None
    spilling: list[str] = []

    def walk(node: dict) -> None:
        nonlocal batches
        # PostgreSQL's JSON key is "Hash Batches" and the text format prints "Batches"; both spellings
        # were checked against a live plan before this line was written, because guessing a key name
        # prints a confident "nothing spilled" over a query that went to disk
        local = node.get("Hash Batches", node.get("Batches"))
        if local is not None:
            batches = int(local) if batches is None else max(batches, int(local))
        # only the operator that spilled is named: a parent inherits its child's counters, so listing
        # every node with a non-zero block count would call ``Aggregate`` a spiller and hide the ``Hash``
        spilled = (int(local or 1) > 1) or str(node.get("Sort Space Type", "")) == "Disk"
        if spilled:
            relation = node.get("Relation Name")
            spilling.append(f"{node.get('Node Type', '?')}" + (f" on {relation}" if relation else ""))
        for child in node.get("Plans") or []:
            walk(child)

    walk(root)
    return {
        "temp_read": read,
        "temp_written": written,
        "batches": batches,
        "spilling": list(dict.fromkeys(spilling))[:3],
    }


def show_spill(plan: dict) -> None:
    """Print the temp-space fact under its plan, in one line, or say that there was nothing to say."""
    info = spill(plan)
    if not info["temp_read"] and not info["temp_written"]:
        print("   spill: none -- no temp blocks were read or written")
        return
    where = f"; spilling at: {', '.join(info['spilling'])}" if info["spilling"] else ""
    known = info["batches"]
    batches = f"worst batch count {known} (> 1 means work_mem sent a hash to disk)" if known else "batches n/a"
    print(
        f"   spill: temp read {info['temp_read']} blocks / written {info['temp_written']}, {batches}"
        f"{where}"
    )


def advice(hot: list[dict], over_ms: float | None) -> list[str]:
    """What the plan says to do, or the one line that says the plan is fine.

    A closed table keyed on node types, and only consulted when the measurement is over the ceiling:
    an optimization proposal for a query that already passes is how a delivery grows an index
    nobody needed.
    """
    if over_ms is None:
        return []
    lines = [f"the mean is {over_ms:.1f} ms over the ceiling; the plan names where that goes:"]
    for item in hot:
        key = item["node"]
        text = ADVICE.get(key)
        if text is None:
            continue
        where = f" on {item['relation']}" if item["relation"] else ""
        lines.append(f"  {key}{where}: {text}")
    if len(lines) == 1:
        lines.append("  no node type in the advice table dominates; read the text plan below")
    return lines


def main(argv: list[str]) -> int:
    runs = 3
    for index, item in enumerate(argv):
        if item == "--runs" and index + 1 < len(argv):
            runs = int(argv[index + 1])
    config = load_contract(STAGE / "config" / "capstone.example.toml")
    print("=" * 72)
    print("feature frame performance -- timing, plan, and what the plan says about the ceiling")
    print("=" * 72)
    print(tree_state())
    try:
        uri = database_uri(config, purpose="time the feature frame")
    except Exception as error:  # a refusal the project already words; no traceback needed
        print(f"\nnot measured: {error}")
        print("exit 0 -- the numbers belong to a machine that has the database")
        return 0
    connection = None
    try:
        connection = connect(uri, purpose="time the feature frame")
        cursor = connection.cursor()
        server = rows(cursor, "SELECT current_database(), version()")[0]
        print(f"\ndatabase: {server[0]}")
        print(f"server:   {str(server[1]).splitlines()[0]}")
        counts = source_counts(cursor)
        if counts:
            print("source tables (the frame's inputs, so a small number here means a small measurement):")
            for table, count in counts:
                print(f"      olist.{table:<28} {count:>9} rows")
        else:
            print("no olist schema on this server: the frame cannot have real inputs here")
        shape = frame_shape(cursor)
        if shape["missing"]:
            print(f"\n{FRAME_VIEW} does not exist on this server; nothing is measured below")
            print("verdict: not measurable -- apply stage-4/sql first, then re-run this script")
            return 0
        print(
            f"\nframe: {shape['rows']} rows, {shape['columns']} columns, "
            f"{shape['features']} feature columns (the brief asks for at least {MINIMUM_FEATURES})"
        )
        if shape["features"] < MINIMUM_FEATURES:
            print(f"      SHORT of the minimum by {MINIMUM_FEATURES - shape['features']} column(s)")
        timing = timed(cursor, FRAME_TIMING_SQL.format(frame=FRAME_VIEW), runs)
        read = timed(cursor, FRAME_READ_SQL.format(frame=FRAME_VIEW), runs)
        ceiling = config.features.feature_query_ms
        print(f"\ntiming, {timing['runs']} runs, cold first (the ceiling in config is {ceiling} ms)")
        for label, item in (("timed (what capstone features judges)", timing), ("read (with transfer)", read)):
            warm = f"{item['warm_ms']} ms" if item["warm_ms"] is not None else "n/a"
            print(
                f"      {label:<40} mean {item['mean_ms']} ms | cold {item['cold_ms']} ms"
                f" | warm {warm} | min {item['min_ms']} | max {item['max_ms']}"
            )
        plans = {}
        print("\n------------------------------------------------------------------------")
        print("1. EXPLAIN (ANALYZE, BUFFERS) -- the statement the ceiling is measured on")
        print("------------------------------------------------------------------------")
        text = explain(cursor, FRAME_TIMING_SQL.format(frame=FRAME_VIEW), fmt="text")
        print("\n".join(f"  {line}" for line in text.splitlines()))
        plans["timing"] = explain(cursor, FRAME_TIMING_SQL.format(frame=FRAME_VIEW), fmt="json")
        show_spill(plans["timing"])
        print("\n   hottest nodes by self time (a parent is not the work when its child is):")
        for item in hotspots(plans["timing"]):
            relation = f" on {item['relation']}" if item["relation"] else ""
            print(
                f"      {item['node']}{relation}: {item['self_ms']} ms"
                f" ({int((item['share'] or 0) * 100)}% of the query), {item['rows']} rows x {item['loops']}"
                f" loops, buffers {item['shared_hit']} hit / {item['shared_read']} read"
            )
        print("\n------------------------------------------------------------------------")
        print("2. the same frame read whole, which is what a person waits for")
        print("------------------------------------------------------------------------")
        plans["read"] = explain(cursor, FRAME_READ_SQL.format(frame=FRAME_VIEW), fmt="json")
        show_spill(plans["read"])
        for item in hotspots(plans["read"]):
            relation = f" on {item['relation']}" if item["relation"] else ""
            print(f"      {item['node']}{relation}: {item['self_ms']} ms self time, {item['rows']} rows")
        over = timing["mean_ms"] - ceiling if timing["mean_ms"] > ceiling else None
        print("\n------------------------------------------------------------------------")
        print("verdict")
        print("------------------------------------------------------------------------")
        if over is None:
            print(
                f"within the ceiling: the timed mean is {timing['mean_ms']} ms against {ceiling} ms."
                f" The whole-frame read is {read['mean_ms']} ms, which the ceiling does not cover and"
                " the brief does not ask about -- recorded so nobody has to guess it later."
            )
        else:
            for line in advice(hotspots(plans["timing"]), over):
                print(f"      {line}" if line.startswith("  ") else line)
        print(
            "\nnumbers, machine-readable: "
            + json.dumps(
                {
                    "database": str(server[0]),
                    "frame_rows": shape["rows"],
                    "frame_columns": shape["columns"],
                    "frame_features": shape["features"],
                    "ceiling_ms": ceiling,
                    "timed_mean_ms": timing["mean_ms"],
                    "read_mean_ms": read["mean_ms"],
                    "over_ceiling_ms": over,
                },
                sort_keys=True,
            )
        )
        print(f"\nstatistics: {stats_note(cursor)}")
        print(
            "protocol note: no cache clearing was attempted -- a script that cannot write"
            "\n/proc/sys/vm/drop_caches reports warm timings, and says so, rather than implying a cold"
            "\nrun it did not perform."
        )
        return 0
    except Exception as error:
        name = type(error).__name__
        reason = re.sub(r"\s+", " ", str(error)).strip()
        print(f"\ncould not measure: {name}: {reason}")
        print("exit 0 -- a server that refuses the statement is reported here, not traced")
        return 0
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
