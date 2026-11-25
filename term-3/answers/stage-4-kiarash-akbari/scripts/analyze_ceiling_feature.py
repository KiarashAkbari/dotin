#!/usr/bin/env python3
"""Answer the question the ceiling's error line asks, instead of leaving it as a task.

``capstone features`` judges the *fastest* of five timed repeats against the brief's 500 ms and, when
it fails, prints: "Store the EXPLAIN (ANALYZE) output with the number and fix the query". Storing that
output was a sentence in an error message and nothing else. This script is the missing half: it writes
the plan, twice, around the one intervention that costs nothing to try.

Why *around*: the bridge (`sql/006`) rebuilds ``capstone.mv_external_density`` on every apply, and a
refreshed materialised view starts with **no** statistics. The join could then pick its shape from a
table the planner believes to be empty -- a measurement artefact wearing a query's clothes. Measured on
a live database: 477 ms on a table holding two censuses, 663.4 ms after narrowing it to the registered
one, with no change to the frame's definition.

So the script measures the frame, runs ``ANALYZE`` on the three relations the bridge owns, measures
again, and prints the verdict twice -- once per state. If the failure survives the ANALYZE, it is a
query finding and `report.md` has to say so; if it does not, the number in the manifest was an artefact
and the fix is to keep statistics warm, not to raise the ceiling.

It can also measure a *tuning* hypothesis the same way, on a session only. A plan that launches one
parallel worker for 99,281 rows is a configurable cost, and that is worth knowing before a ceiling is
called a defect::

    python scripts/analyze_ceiling_feature.py --config config/capstone.default.toml
    python scripts/analyze_ceiling_feature.py --set "max_parallel_workers_per_gather=4"

`--set` runs `SET` on this connection and nothing else: no `ALTER SYSTEM`, no `postgresql.conf`, no
config key. A witness produced with it says so in its own header, and a witness measured *without* the
setting it already claims is refused -- so a pass nobody can reproduce cannot be committed by accident.

Crediting a condition is a separate claim from measuring one, so the plan is read in every state and a
delta is only attributed when the plan actually moved (`plans_moved` in the JSON, `plan moved across …`
in the witness). A state that meets the ceiling with an unchanged plan is reported as
``uncredited``: one fast repeat at the end of a warming session, not a cure -- and it does not exit 0.
Measured on a live database: `--set max_parallel_workers_per_gather=4` moved the judged statistic from
666.8 ms to 481.2 ms while `Workers Launched` stayed at 1 in both plans and 4 of that state's own 5
repeats stayed over the ceiling.

Writes ``docs/evidence/ceiling-feature-analysis.txt`` (+ ``.json``) and refuses to overwrite a witness
whose header names a different ceiling, frame row count or tuning, for the same reason
`scripts/cheating_model_gap.py` refuses a witness from another split.

A measurement that changes the *state* does not fight the stored one for the same filename: a `--set`
run writes ``ceiling-feature-analysis.tuned-<tag>.txt`` beside it, where `<tag>` is a digest of the
sorted settings. Both files are evidence -- one says the frame misses the ceiling, the other says what
it costs to move it -- and a refusal whose only remedy is `--force` would teach people to destroy the
first one to produce the second.
"""

from __future__ import annotations

import argparse
import hashlib
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

#: the witness a plain run writes; a tuned run derives its own name from this one so the two states
#: never fight over one file
DEFAULT_EVIDENCE = STAGE / "docs/evidence/ceiling-feature-analysis.txt"

#: a `SET` name is one identifier, and that is the whole allowlist: the value is quoted separately, so
#: a semicolon inside `--set` cannot smuggle a second statement into the session
SETTING_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

#: a name that already carries a state is left alone, so pointing `--evidence` at a tuned witness and
#: re-running the same settings rewrites that file instead of nesting `.tuned-xxxx.tuned-yyyy`
TUNED_NAME = re.compile(r"\.tuned-[0-9a-f]{6}$")


def parse_settings(pairs: list[str] | None) -> dict[str, str]:
    """`--set key=value` arguments, validated -- and refused before a connection exists."""
    settings: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = str(pair).partition("=")
        key = key.strip().lower()
        if not sep or not SETTING_NAME.match(key):
            raise ValueError(
                f"--set wants key=value with a plain setting name, got {pair!r}; a session setting is "
                "a measurement condition, not a place to run SQL"
            )
        if not value.strip():
            raise ValueError(f"--set {key}= is empty; pass a value, or drop the flag")
        settings[key] = value.strip()
    return settings


def setting_lines(settings: dict[str, str]) -> list[str]:
    """The statements, one per setting, with the value quoted in exactly one place."""
    return [f"SET {key} = '{value.replace(chr(39), chr(39) * 2)}'" for key, value in settings.items()]


def tuning_tag(settings: dict[str, str]) -> str:
    """A short, order-independent name for a set of session settings; `""` when there are none.

    The digest is taken over the *sorted* pairs, so `--set a=1 --set b=2` and the reverse land on the
    same witness, and it is a digest of a fixed string rather than `hash()`, so the name a reader sees
    today is the name their re-run picks tomorrow.
    """
    if not settings:
        return ""
    joined = ";".join(f"{key}={settings[key]}" for key in sorted(settings))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:6]


def witness_path(evidence: Path, settings: dict[str, str]) -> Path:
    """Where a measurement belongs: a tuned run writes beside the untuned witness, not over it."""
    tag = tuning_tag(settings)
    if not tag or TUNED_NAME.search(evidence.stem):
        return evidence
    return evidence.with_name(f"{evidence.stem}.tuned-{tag}{evidence.suffix}")


def state_difference(stored: dict[str, str], current: dict[str, str]) -> list[str]:
    """Which of the three claims a witness makes have moved, as sentences a reader can act on.

    Empty means the run on disk and the run being asked for measure the same state, and overwriting is
    then just a refresh. Non-empty is what turns "refusing to overwrite" into something to do.
    """
    labels = (("ceiling_ms", "ceiling"), ("frame_rows", "frame rows"), ("tuning", "tuning"))
    moved: list[str] = []
    for key, label in labels:
        was = (stored.get(key) or "").strip()
        now = (current.get(key) or "").strip()
        if was != now:
            moved.append(f"{label}: {was or 'nothing'} there, {now or 'nothing'} here")
    return moved


def shown(path: Path) -> str:
    """The path as a reader standing in the stage directory would type it."""
    try:
        return str(path.relative_to(STAGE))
    except ValueError:
        return str(path)


def refusal(path: Path, stored: dict[str, str], claims: dict[str, str], belongs: Path) -> str:
    """The text a refused overwrite has to carry: what differs, and where the new run should go.

    Naming only `--force` turned a second measurement into a choice between destroying the first one and
    not taking it. A refusal is a tool working correctly only if the reader can act on it.
    """
    advice = (
        f"this measurement belongs at {shown(belongs)}, beside the state it compares against rather "
        "than on top of it"
        if belongs != path
        else "the file you pointed at already holds a different measurement, so --force only if this "
        "run supersedes it"
    )
    return (
        f"refusing to overwrite {path.name}: it records ceiling {stored['ceiling_ms']} ms on "
        f"{stored['frame_rows']} rows"
        + (f" tuned by {stored['tuning']}" if stored["tuning"] else "")
        + "\nwhat differs: "
        + "; ".join(state_difference(stored, claims))
        + "\n"
        + advice
    )



def read_plan(cursor, frame: str) -> str:
    """The plan, as text: what the ceiling's error line asks to be stored, read in one state."""
    cursor.execute(PLAN_SQL.format(frame=frame))
    return "\n".join(str(row[0]) for row in cursor.fetchall())


def plan_summary(plan: str) -> str:
    """One line per state -- the numbers a reader has to compare before crediting anything."""
    workers = ", ".join(str(count) for count in workers_launched(plan)) or "no Gather"
    spill = " + spill" if re.search(r"Batches: [2-9]\d*|Temp Read", plan) else ", no spill"
    execution = re.search(r"Execution Time: ([\d.]+) ms", plan)
    return f"Workers Launched: {workers}{spill}; execution {execution.group(1) if execution else '?'} ms"


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


#: the plan lines that decide whether a session setting did anything at all
PLAN_KEYS = ("Workers Planned", "Workers Launched", "Gather", "Execution Time", "Planning Time")


def plan_fingerprint(plan: str) -> list[str]:
    """The handful of plan lines a reader needs to see whether the shape moved.

    A whole `EXPLAIN` is ~100 lines, and a witness that carries two of them cannot be read; the lines
    below are the ones that change when a setting changes the plan, plus the spill markers -- a
    `Batches: 2` or a `Temp Read` appearing is a memory story, not a parallelism one.
    """
    kept = [line.strip() for line in plan.splitlines() if any(key in line for key in PLAN_KEYS)]
    kept += [
        line.strip()
        for line in plan.splitlines()
        if re.search(r"Batches: [2-9]\d*|Temp Read|Temp Written", line)
    ]
    return kept


def workers_launched(plan: str) -> list[int]:
    """Every `Workers Launched: N` in the plan -- normally one number, from the top Gather."""
    return [int(n) for n in re.findall(r"Workers Launched: (\d+)", plan)]


def plan_changed(before: str, after: str) -> bool:
    """Whether the two states ran *different* plans, which is what a setting has to do to be credited."""
    return plan_fingerprint(before) != plan_fingerprint(after)


def verdict(durations: list[float], ceiling_ms: float) -> dict:
    """One state's numbers, judged on the basis the project fixed in `features.TIMING_BASIS`.

    `best_repeat` and `below_ceiling` exist because the basis is `min`: a state whose fastest repeat is
    one outlier of five has not met the ceiling, it has met it once. Both numbers cost nothing to record
    and one of them is how a pass gets read honestly.
    """
    best = min(durations)
    return {
        "runs_ms": [round(value, 3) for value in durations],
        "best_ms": round(best, 3),
        # 1-based, so "repeat 5 of 5" reads the way a log does
        "best_repeat": durations.index(best) + 1,
        "below_ceiling": sum(1 for value in durations if value <= ceiling_ms),
        "mean_ms": round(sum(durations) / len(durations), 3),
        "worst_ms": round(max(durations), 3),
        "ceiling_ms": ceiling_ms,
        "passes": best <= ceiling_ms,
    }


def state_line(label: str, state: dict) -> str:
    """One measured state, printed with the two numbers that say how much of it is a fluke."""
    n = len(state["runs_ms"])
    return (
        f"{label}: best {state['best_ms']:.1f} ms, mean {state['mean_ms']:.1f} ms, "
        f"worst {state['worst_ms']:.1f} ms, runs {state['runs_ms']}; "
        f"{state['below_ceiling']}/{n} repeats under the ceiling, fastest was repeat "
        f"{state['best_repeat']} of {n}"
    )


def describe(before: dict, after: dict, *, state: str = "ANALYZE", plans_changed: bool = True) -> str:
    """The sentence the report needs, in the shape of a decision rather than a number.

    `state` names what changed between the two measurements, because the remedy differs entirely: warm
    statistics, a session setting, or nothing at all. `plans_changed` is the attribution gate: a delta
    can only be credited to a condition that changed the plan it ran under. Without it, a single fast
    repeat at the end of a warmed-up session reads as a cure -- which is exactly what one live run of
    `--set max_parallel_workers_per_gather=4` looked like before anyone compared the two plans.
    """
    n = len(after["runs_ms"])
    if before["passes"]:
        return (
            f"finding: the frame already meets the ceiling ({before['best_ms']:.1f} ms best of "
            f"{before['below_ceiling']}/{len(before['runs_ms'])} repeats under "
            f"{before['ceiling_ms']:.0f} ms); this run is context, not a fix"
        )
    if after["passes"] and not plans_changed:
        return (
            f"uncredited: {state} moves the fastest repeat from {before['best_ms']:.1f} ms to "
            f"{after['best_ms']:.1f} ms, but the two states ran the same plan shape -- same "
            f"`Workers Launched`, same spill markers -- so the delta is not {state}'s doing; and only "
            f"{after['below_ceiling']} of {n} repeats in that state were under the ceiling, with the "
            f"fastest as repeat {after['best_repeat']} of {n}. carry this as one fast outlier, not as "
            "a pass, and not as a condition the report can rely on"
        )
    if after["passes"]:
        remedy = (
            "so the breach was a stale plan on the rebuilt snapshot, not a cost of the frame -- the "
            "remedy is to keep those statistics warm"
            if state == "ANALYZE"
            else "so the breach is a session condition rather than the frame itself -- carry the "
                 "setting into the report as the condition the number was measured under"
        )
        return (
            f"artefact: {before['best_ms']:.1f} ms becomes {after['best_ms']:.1f} ms after {state}, "
            f"{remedy}"
        )
    if before["below_ceiling"] == 0 and after["below_ceiling"] == 0:
        spread = "; no repeat in either state met it"
    else:
        spread = (
            f" ({before['below_ceiling']}/{len(before['runs_ms'])} and {after['below_ceiling']}/{n} "
            "repeats under it, across the two states)"
        )
    return (
        f"query finding: {state} moves the fastest repeat from {before['best_ms']:.1f} ms to "
        f"{after['best_ms']:.1f} ms and the ceiling still fails{spread}, so `report.md` has to carry "
        "this as a measured shortfall and the plan below is the starting point"
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
        default=DEFAULT_EVIDENCE,
        help="base witness; a run with --set writes <base>.tuned-<tag>.txt instead of over it",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite a witness measured at a different ceiling, row count or tuning",
    )
    parser.add_argument(
        "--set",
        action="append",
        metavar="KEY=VALUE",
        help="measure a third time with this session SET applied (repeatable); nothing is persisted",
    )
    args = parser.parse_args(argv)
    if args.repeats < 2:
        print("error: --repeats needs at least 2; one number cannot show a worst case", file=sys.stderr)
        return 2
    try:
        settings = parse_settings(args.set)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    config = load_config(args.config)
    ceiling = float(config.features.feature_query_ms)
    frame = features.FRAME_VIEW
    tuning = ", ".join(f"{key}={value}" for key, value in settings.items())

    witness = witness_path(args.evidence, settings)
    previous = None
    if witness.exists():
        # the guard is about *what the file claims*; `--force` is consulted inside it, so it stays the
        # only way past
        head = witness.read_text(encoding="utf-8")[:2000]
        previous = re.search(r"ceiling (\d+) ms; frame rows (\d+)(?:; tuned by ([^\n]*))?", head)

    connection = connect(database_uri(config), purpose="measure the frame across an ANALYZE")
    try:
        cursor = connection.cursor()
        cursor.execute(f"SELECT count(*) FROM {frame}")
        rows = int(cursor.fetchone()[0])
        stored = {
            "ceiling_ms": previous.group(1) if previous else "",
            "frame_rows": previous.group(2) if previous else "",
            "tuning": (previous.group(3) or "").strip() if previous else "",
        }
        claims = {"ceiling_ms": str(int(ceiling)), "frame_rows": str(rows), "tuning": tuning}
        difference = state_difference(stored, claims) if previous else []
        if difference and not args.force:
            print(refusal(witness, stored, claims, witness_path(DEFAULT_EVIDENCE, settings)))
            return 2

        before_runs = measure(cursor, frame, repeat=args.repeats, clock=time.perf_counter)
        plan_before = read_plan(cursor, frame)
        connection.commit()
        for target in ANALYZE_TARGETS:
            if target == "capstone.mv_external_density":
                cursor.execute("SELECT to_regclass('capstone.mv_external_density') IS NOT NULL")
                if not cursor.fetchone()[0]:
                    continue  # no bridge yet: there is nothing here to have stale statistics about
            cursor.execute(f"ANALYZE {target}")
        connection.commit()
        after_runs = measure(cursor, frame, repeat=args.repeats, clock=time.perf_counter)
        plan_after = read_plan(cursor, frame)
        tuned_runs = None
        plan_tuned = ""
        if settings:
            for statement in setting_lines(settings):
                cursor.execute(statement)
            tuned_runs = measure(cursor, frame, repeat=args.repeats, clock=time.perf_counter)
            plan_tuned = read_plan(cursor, frame)
        # the plan the error line asked for is the one from the state the run ends in
        plan = plan_tuned or plan_after
    finally:
        connection.close()

    before = verdict(before_runs, ceiling)
    after = verdict(after_runs, ceiling)
    tuned = verdict(tuned_runs, ceiling) if tuned_runs else None
    # the two verdicts are computed once, so the .txt and the .json cannot disagree about what was found
    moved_analyze = plan_changed(plan_before, plan_after)
    moved_setting = plan_changed(plan_after, plan_tuned) if tuned is not None else False
    verdict_text = describe(before, after, plans_changed=moved_analyze)
    verdict_tuned_text = (
        describe(before, tuned, state="ANALYZE + SET " + tuning, plans_changed=moved_setting)
        if tuned is not None
        else None
    )
    lines = [
        "# ceiling on the feature frame, measured across one ANALYZE (scripts/analyze_ceiling_feature.py)",
        f"config {args.config.name}; frame {frame}; ceiling {int(ceiling)} ms; frame rows {rows}"
        + (f"; tuned by {tuning}" if tuning else ""),
        f"basis: {features.TIMING_BASIS}, the same statistic the feature step judges on",
        *(
            [f"companion: the same measurement without the setting is in {shown(args.evidence)}"]
            if witness != args.evidence
            else []
        ),
        "",
        state_line("before ANALYZE", before),
        state_line("after  ANALYZE", after),
        f"analyzed: {', '.join(ANALYZE_TARGETS)}",
        "",
        "plan shape per state -- a condition has to move these to be credited with the delta:",
        f"  before ANALYZE: {plan_summary(plan_before)}",
        f"  after  ANALYZE: {plan_summary(plan_after)}",
        *(
            [f"  tuned ({tuning}): {plan_summary(plan_tuned)}"]
            if tuned is not None
            else []
        ),
        f"  plan moved across the ANALYZE: {moved_analyze}",
        "",
        verdict_text,
    ]
    if tuned is not None:
        lines += [
            state_line(f"after  ANALYZE + SET {tuning}", tuned),
            f"  plan moved across the setting: {moved_setting}",
            f"with the setting applied: {verdict_tuned_text}",
        ]
    lines += [
        "",
        "## the plan the error line asked for, read after the last measurement in "
        + ("the tuned state " + f"({tuning})" if tuned is not None else "the untuned state"),
        plan,
    ]
    witness.write_text("\n".join(lines) + "\n", encoding="utf-8")
    witness.with_suffix(".json").write_text(
        json.dumps(
            {
                "config": str(args.config),
                "ceiling_ms": ceiling,
                "frame_rows": rows,
                "settings": settings,
                "before": before,
                "after": after,
                "tuned": tuned,
                "plan_shapes": {
                    "before_analyze": plan_summary(plan_before),
                    "after_analyze": plan_summary(plan_after),
                    "tuned": plan_summary(plan_tuned) if tuned is not None else None,
                },
                "plans_moved": {"analyze": moved_analyze, "setting": moved_setting},
                "verdict": verdict_text,
                "verdict_tuned": verdict_tuned_text,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\n".join(lines))
    print(f"\nwrote {shown(witness)} and {shown(witness.with_suffix('.json'))}")
    # a tuned state only exits green if the setting moved the plan: otherwise the "pass" is one fast
    # repeat in a warming session, and an exit code is read as a verdict, not as a number
    met = before["passes"] or after["passes"] or (
        tuned is not None and tuned["passes"] and moved_setting
    )
    return 0 if met else 1


if __name__ == "__main__":
    raise SystemExit(main())
