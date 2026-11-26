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
    python scripts/analyze_ceiling_feature.py --set "max_parallel_workers_per_gather=2" --control

`--control` then puts those settings back off with `RESET` and measures the previous state one more time,
last; see the ordering paragraph below for what that buys.

`--set` runs `SET` on this connection and nothing else: no `ALTER SYSTEM`, no `postgresql.conf`, no
config key. A witness produced with it says so in its own header, and a witness measured *without* the
setting it already claims is refused -- so a pass nobody can reproduce cannot be committed by accident.

Crediting a condition is a separate claim from measuring one, so the plan is read in every state and
compared on its **shape** -- estimates, `Workers Planned/Launched`, spill markers, with the `(actual …)`
noise and the two timing lines removed, because a timing line differs between any two runs of the same
plan. The verdict is then one of six words (`attribution`): ``refuted`` (no repeat in that state met the
ceiling), ``noise`` (the gap between the states is smaller than the spread inside one of them),
``ordering`` (a control arm says most of the drop was the session warming up anyway), ``credited`` (the plan
moved), ``conditional`` (the plan did not move but a majority of that state's own repeats met the ceiling)
or ``uncredited`` (a pass resting on one fast repeat). Only ``credited`` and ``conditional`` exit 0, and the
first two of these are the cheap ones: ``noise`` needs no second run, only the two the script already took.

Measured on a live database three times with ``--set max_parallel_workers_per_gather=4``, ``=4`` again and
``=2``: bests of 443.1 ms (4 of 5 repeats under 500), 443.2 (5 of 5) and 452.2 (5 of 5), with both states'
plans staying `Workers Launched: 1` over the identical `cost=22585.11..39899.80 rows=99252`. Three values
of one setting and one plan shape, so the number was never the setting's. Then a fourth run, with *no*
setting at all and nine repeats, put the **untuned** frame at 467.3 ms best with 6 of 9 repeats under the
ceiling -- and the same run's `after ANALYZE` state at 472.9 ms best with 4 of 9, its own nine samples
running 472.9 to 657.7. ``conditional`` is what the tuned pair deserved; ``noise`` is what this one does,
and `report.md` has to carry both: 500 ms on this machine is a line the same session crosses twice, so
neither a pass nor a breach here is a finding about the query.

The frame is also read once per state with the transfer included (`features.FRAME_READ_SQL`), because
`EXPLAIN (ANALYZE) SELECT *` discards the rows on the server: whatever the judged number says, it has
never included the client, and 99,281 rows × 261 bytes is a cost a reader is entitled to see priced.

The order the states are measured in is itself a condition, and the one that lies most easily, because
every state is an interval and the ceiling is a point inside or outside it. Each state's line therefore
ends with `spread X ms, drift ±Y ms, the ceiling is inside this state's own interval|outside it` -- the
drift being the second half of the run minus the first, since a live session turned out to move *upward*
in its second half while its endpoints implied a 48 ms improvement, which is what an endpoint difference
is worth. `--control` prices the order explicitly instead of bounding it: it puts the settings back off and
measures the previous state again, last. What the control recovers by itself is what the run refuses to
credit to the condition. The comparison only works one way, because only one way exists: a session setting
comes off with `RESET` in the same connection, while `ANALYZE` has no inverse -- nothing un-writes the row
estimates it stored -- so the before -> after gap has no arm and stays a claim about estimates, not about
speed.

A third question is answered from the catalog instead of from the plan text: `pg_stat_user_tables` says
*who* last refreshed the statistics each source carries, which is the difference between a stale frame and
a warm one. Autovacuum analyzing a quarter of an hour before a run makes that run's `before` state a duplicate of
its `after`, and it is the reason two rounds of this command disagreed about the untuned state. The read
happens before the `ANALYZE` this script issues, because that statement is itself one of the possible
answers and it overwrites the evidence.

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
from datetime import datetime, timezone
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
#: the frame *with* the transfer included -- `features.FRAME_READ_SQL`, copied because this module must
#: import without a driver, and pinned to it by a test. `EXPLAIN` discards the rows on the server, so
#: the judged number never says how much of it is network and client; this statement is what his own
#: comment reserves for that question, and the witness now answers it per state.
READ_SQL = "SELECT * FROM {frame} AS frame"

#: what makes a plan a *different* plan: the estimates, the worker counts and the spill. Node lines keep
#: their `(cost=… rows=…)` because those are what `ANALYZE` moves, and lose their `(actual …)` because
#: those move on their own every run -- and the two timing lines are pure wall clock, so leaving them in
#: would make every comparison of two identical plans report a change (which is exactly the bug one live
#: pair of runs exposed: `plan moved across the setting: True`, with a byte-identical plan above it).
PLAN_ACTUAL_NOISE = re.compile(r"\s*\(actual[^)]*\)")
PLAN_TIMING_LINES = ("Execution Time", "Planning Time")

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


def reset_lines(settings: dict[str, str]) -> list[str]:
    """How a session condition comes back off: `RESET`, in the reverse order it went on.

    A control arm has to measure the *earlier state again*, which means the setting has to leave. `RESET`
    is the session-scoped mirror of `SET`, so the same rule that keeps `--set` inside one connection keeps
    the control inside one run: no `ALTER SYSTEM`, no config file, nothing the next session inherits.
    """
    return [f"RESET {key}" for key in reversed(list(settings))]


def tag_settings(settings: dict[str, str], *, control: bool) -> dict[str, str]:
    """What the witness filename is hashed from: the SET pairs, plus the control arm when it was taken.

    `0038`'s rule, applied to a new dimension -- a run that re-measured a state is not the same measurement
    as one that did not, so the two must not fight over one file. With `control` off this is the same dict,
    which is why the tag of a plain `--set max_parallel_workers_per_gather=4` run is still `411fdc`.
    """
    return {**settings, "control": "same-state-last"} if control else dict(settings)


def trend_ms(state: dict) -> float:
    """Second half of the run minus its first half: the direction the state was still moving in.

    Endpoints were the first attempt here and his live run killed them: a state reading
    ``[599.8, 506.7, 479.0, 478.8, 467.3, 473.4, 469.5, 500.9, 483.3]`` starts with one cold sample and
    then settles, while a state reading ``[472.9, 474.4, 473.6, 476.1, 641.1, 657.7, 584.1, 515.6,
    521.0]`` was *rising* -- and first-minus-last calls the second one a 48 ms improvement. Averaging each
    half means a single outlier at either end cannot decide the sign, and the sign is the whole point: a
    state that is moving by more than the ceiling margin has not been measured, it has been travelled
    through, in whichever direction that happens to be.
    """
    runs = state["runs_ms"]
    if len(runs) < 2:
        return 0.0
    half = len(runs) // 2
    head, tail = runs[:half], runs[half:]
    return round(sum(tail) / len(tail) - sum(head) / len(head), 3)


def interval_ms(state: dict) -> float:
    """How wide one state's own repeats are, read back out of `runs_ms`.

    `verdict()` already stores this as `spread_ms`; the helper exists so a sentence can be built from a
    state dict that a test or a reader handed over without the derived fields, and so the two numbers can
    never disagree about which runs they were computed from.
    """
    runs = state["runs_ms"]
    return round(max(runs) - min(runs), 3) if runs else 0.0


def gap_pair(earlier: dict, later: dict, *, first_name: str, second_name: str) -> dict:
    """What a condition moved the judged statistic, against how wide the two states already are.

    The gap is the number a reader would credit to `ANALYZE` or to a `SET`. The spread is the reason not
    to, straight away and without a control arm: if the wider of the two states' own intervals is bigger
    than the gap, the two runs were never going to settle anything -- his warm session put the untuned
    frame at 467.3 ms best and 599.8 ms worst, and that interval alone is wider than every difference this
    probe has ever been asked to explain.
    """
    first_ms, second_ms = interval_ms(earlier), interval_ms(later)
    return {
        "observed_ms": round(earlier["best_ms"] - later["best_ms"], 3),
        "spread_ms": max(first_ms, second_ms),
        "spread_of": first_name if first_ms >= second_ms else second_name,
        "first_spread_ms": first_ms,
        "second_spread_ms": second_ms,
        "inside_noise": abs(round(earlier["best_ms"] - later["best_ms"], 3)) < max(first_ms, second_ms),
    }


def straddles_ceiling(state: dict) -> bool:
    """Whether the ceiling falls inside the interval this state's own repeats produced.

    This is the question a pass or a fail has to answer before either is believed, and it needs no
    threshold and no model: ``best <= ceiling <= worst``. His warm session put the *untuned* frame at best
    467.3 ms and worst 599.8 ms, so "the frame meets the ceiling" and "the frame breaches it" are both
    true of that one run, and neither is a finding about the query. A state that does not straddle --
    452.2 to 454.6 against a 500 ms ceiling -- is a measurement.
    """
    ceiling = state["ceiling_ms"]
    return state["best_ms"] <= ceiling <= state["worst_ms"]


def gain_split(*, naive_ms: float, drift_ms: float) -> dict:
    """Split an observed drop between the intervention and the moment it was measured at.

    `naive_ms` is the gap the run would otherwise credit to `ANALYZE` or to a `SET`; `drift_ms` is how
    much the *same state* improved again when it was re-measured at the end with nothing changed (so the
    control arm is what makes this number exist). A negative drift means the control was slower, which is
    not a credit to the condition, so it is floored at zero; the ordering share is capped at the whole gap.
    """
    naive = float(naive_ms)
    ordering = min(max(float(drift_ms), 0.0), naive) if naive > 0 else 0.0
    return {
        "naive_ms": round(naive, 3),
        "ordering_ms": round(ordering, 3),
        "attributed_ms": round(naive - ordering, 3),
        "ordering_share": round(ordering / naive, 3) if naive > 0 else 0.0,
    }


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


def measure_read(cursor, frame: str, *, clock) -> tuple[float, int]:
    """One read of the frame *with* the transfer included, to price what `EXPLAIN` throws away.

    `EXPLAIN (ANALYZE) SELECT *` computes every row and column and discards them on the server, so the
    judged number can never say how much of it is the client. `features.FRAME_READ_SQL` exists for that
    question and nothing in `src/` runs it; this takes it once per state -- once, because a single sample
    of the read is a cost estimate, not a second verdict.
    """
    started = clock()
    cursor.execute(READ_SQL.format(frame=frame))
    rows = sum(1 for _ in cursor)
    return (clock() - started) * 1000.0, rows


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
PLAN_KEYS = ("Workers Planned", "Workers Launched", "Gather", "Join", "Scan")
SPILL_MARK = re.compile(r"Batches: [2-9]\d*|Temp Read|Temp Written")


def plan_shape(plan: str) -> list[str]:
    """The comparison key: estimates, worker counts and spill -- and nothing the run itself decides.

    Two filters, and the second one is the whole rule: keep a line if it names a node, a worker count or
    a spill marker. `Execution Time` and `Planning Time` fall out of it on their own -- they name no node
    -- which is why the shape cannot move just because the clock moved. `PLAN_TIMING_LINES` is what puts
    them back for `plan_fingerprint`, the human-facing list.
    """
    lines: list[str] = []
    for raw in plan.splitlines():
        line = PLAN_ACTUAL_NOISE.sub("", raw.strip())
        if line and (any(key in line for key in PLAN_KEYS) or SPILL_MARK.search(line)):
            lines.append(line)
    return lines


def plan_fingerprint(plan: str) -> list[str]:
    """The handful of plan lines a reader needs to see whether the shape moved, timings included.

    A whole `EXPLAIN` is ~100 lines, and a witness that carries three of them cannot be read. These are
    the ones that change when a setting changes the plan, plus the spill markers -- a `Batches: 2` or a
    `Temp Read` appearing is a memory story, not a parallelism one. This is what the witness prints per
    state and what `plan_shape` is for the machine: `plan_shape` is the same list without the two timing
    lines, and only that one is compared. A reader may diff these by hand; feeding them to `plan_changed`
    is what made the first version of this gate answer True to everything.
    """
    return plan_shape(plan) + [
        line.strip()
        for line in plan.splitlines()
        if any(key in line for key in PLAN_TIMING_LINES)
    ]


def workers_launched(plan: str) -> list[int]:
    """Every `Workers Launched: N` in the plan -- normally one number, from the top Gather."""
    return [int(n) for n in re.findall(r"Workers Launched: (\d+)", plan)]


#: the only setting whose entire job is to raise a ceiling: `Workers Planned` is the planner's decision,
#: `Workers Launched` the resource manager's, and a plan that *planned* fewer workers than the cap allows
#: was never limited by the cap at all
PARALLEL_CAP = re.compile(r"max_parallel_workers_per_gather")
PLAN_GATHER_ROWS = re.compile(r"^Gather\s+\(cost=[\d.]+\.\.[\d.]+ rows=(\d+)", re.M)
PLAN_GATHER_ACTUAL = re.compile(r"^Gather\s+\(.*?\(actual rows=(\d+)", re.M)


def workers_planned(plan: str) -> list[int]:
    """Every `Workers Planned: N` in the plan -- normally one, from the top Gather.

    The sibling of `workers_launched`, and the two answer different questions: what the planner wanted,
    versus what the cluster gave. `max_parallel_workers_per_gather` can only act on the first.
    """
    return [int(n) for n in re.findall(r"Workers Planned: (\d+)", plan)]


def cap_binding(settings: dict[str, str], plans: dict[str, str]) -> dict | None:
    """Whether the parallel cap in `settings` is what limited these plans, or was never reached.

    `attribution()` answers the statistical question -- is this gap smaller than the states' own spread --
    and a reader who is told `noise` reasonably asks how many repeats would settle it. None, if the
    condition cannot move the plan at all: with `Workers Planned: 1` under a cap of `2`, the planner was
    limited by cost, not by the setting, so the gap is not merely small, it is unpriceable. Returns None
    when the setting is not the parallel cap, when its value is not a number (a word like `DISABLED` is a
    different claim), or when no plan carries a Gather -- no plan, no mechanism, and no invented one.
    """
    cap = next((value for key, value in settings.items() if PARALLEL_CAP.search(key)), None)
    if cap is None:
        return None
    try:
        limit = int(cap)
    except ValueError:
        return None
    planned = {name: max(values) for name, values in ((k, workers_planned(p)) for k, p in plans.items()) if values}
    if not planned:
        return None
    top = max(planned.values())
    return {"cap": limit, "planned": top, "per_state": planned, "binding": top >= limit}


def estimate_drift(plan: str) -> dict | None:
    """How far the top node's own row estimate is from what the same run measured, in percent.

    A stale-plan story needs a stale estimate, and the plan carries both numbers on one line: `ANALYZE` is
    credited with a pass only if the estimate was wrong before it ran. His 15-repeat run read
    `Gather (cost=22585.18..39901.76 rows=99265 width=261) (actual rows=99281 loops=1)` in the *untuned*
    state -- 0.02% off, which is what "there is no stale-plan effect left to measure on this database"
    means, and is a different claim from "we measured no effect": only the second tells a reader to stop
    repeating the command.
    """
    estimated, actual = PLAN_GATHER_ROWS.search(plan), PLAN_GATHER_ACTUAL.search(plan)
    if estimated is None or actual is None:
        return None
    est, act = int(estimated.group(1)), int(actual.group(1))
    if act == 0:
        return None
    return {"estimated_rows": est, "actual_rows": act, "pct": abs(est - act) / act * 100.0}


def name_list(names) -> str:
    """``a``, ``a and b``, ``a, b and c`` -- a witness line is prose, not a list dump.

    The states are joined in the order they were measured, because "after and before and tuned" reads like
    an alphabetised dict, and a reader should not have to unsort the one sentence that names them.
    """
    spelled = [f"`{name}`" for name in names]
    if len(spelled) == 1:
        return spelled[0]
    return " and ".join([", ".join(spelled[:-1]), spelled[-1]])


def inert_line(inert: dict | None) -> str | None:
    """The witness line that prices a setting by mechanism instead of by repeats, or None for no claim."""
    if inert is None:
        return None
    where = name_list(inert["per_state"])
    if not inert["binding"]:
        return (
            f"  setting inert: `Workers Planned: {inert['planned']}` in {where} against a cap of "
            f"{inert['cap']} -- the cap is not what limited the plan, so no number of repeats makes this "
            "condition measurable on this query; `Workers Planned` is the planner's own choice and it "
            "stopped below the ceiling you raised"
        )
    return (
        f"  setting binding: `Workers Planned: {inert['planned']}` reached the cap of {inert['cap']} in "
        f"{where} -- the plan was limited by the setting, so a change in it is a mechanism and not noise"
    )


def stats_line(drift: dict | None, moved_analyze: bool, ceiling_ms: float) -> str | None:
    """The witness line that says whether `ANALYZE` can still be priced here, or None for no claim."""
    if drift is None or moved_analyze or drift["pct"] > 1.0:
        return None
    return (
        f"  statistics already current: the *untuned* plan estimates {drift['estimated_rows']:,} rows "
        f"against {drift['actual_rows']:,} it actually returned ({drift['pct']:.2f}% off), so there was no "
        "stale estimate for this `ANALYZE` to correct -- `ANALYZE` earns credit only on a frame rebuilt "
        "without it, and repeating this command on this database cannot change that. Read the "
        f"{ceiling_ms:.0f} ms line as met-but-thin, and buy headroom in the query, not in the statistics"
    )


#: who refreshed the statistics this run is about to call "untuned". `pg_stat_user_tables` keeps two
#: stamps per relation and they have two different authors: `last_analyze` is a manual pass -- including
#: *this script's own*, from the previous round -- and `last_autoanalyze` is the autovacuum launcher
#: deciding a table had changed enough. The run's own `ANALYZE` overwrites `last_analyze`, so the
#: question can only be asked before the statement that destroys the answer
STATISTICS_SQL = (
    "SELECT relname, last_analyze, last_autoanalyze FROM pg_stat_user_tables "
    "WHERE schemaname = '{schema}' AND relname IN ({quoted}) ORDER BY relname"
)


def statistics_query(targets: list[str]) -> str:
    """The catalog read for `targets`, built from them rather than written out beside them.

    The five names are the five the run is about to `ANALYZE`, so the question "whose statistics did the
    `before` state run against" is asked about exactly the relations whose statistics it is about to
    overwrite -- and a sixth table added to `ANALYZE_TARGETS` is queried here without anyone remembering
    to edit a second literal list.
    """
    schema = targets[0].split(".", 1)[0]
    quoted = ", ".join(f"'{name.split('.', 1)[1]}'" for name in targets)
    return STATISTICS_SQL.format(schema=schema, quoted=quoted)


def minutes_since(stamp: datetime, now: datetime) -> float:
    """Minutes between a catalog stamp and the clock, with or without a timezone on either side.

    `pg_stat_user_tables` holds `timestamptz`, so the driver gives back an aware datetime and the age is
    absolute. Asked for as `to_char(..., 'HH24:MI:SS')` -- which is how this got checked by hand once -- the
    same number is a *wall clock in the server's zone*, and his reads +03:30: an age computed against a UTC
    `now` is then off by three and a half hours, which is a bigger error than the finding. A naive stamp is
    therefore taken at face value rather than assumed to be UTC, and what the witness leans on is the
    difference between two stamps, which no zone can move.
    """
    left, right = (now, stamp) if stamp.tzinfo else (now.replace(tzinfo=None), stamp)
    return max(0.0, (left - right).total_seconds() / 60.0)


def statistics_provenance(rows, *, now: datetime) -> dict | None:
    """Which author wrote the newest statistics on each source, and how long ago.

    `before` -> `after` prices one thing only: a manual `ANALYZE` run on statistics nobody had touched
    since the build. Nothing in the run says whether that was the situation -- his fourth round read 6/15
    and 2/15 for two states the third round put at 6/15 and 13/15, and the catalog said why: autoanalyze
    at 01:43:51, his manual `ANALYZE` at 01:58:09. The `before` state of that run was warm already, so
    the pair measured an autovacuum pass nobody in the run had paid for. None when there are no catalog
    rows at all: a view's statistics are its base tables' business, and silence beats an invented author.
    """
    if not rows:
        return None
    per_source: dict[str, dict] = {}
    for name, manual, auto in rows:
        stamps = {who: value for who, value in (("manual", manual), ("auto", auto)) if value is not None}
        if not stamps:
            per_source[name] = {"author": "never", "minutes_ago": None, "separation_minutes": None}
            continue
        author = max(stamps, key=lambda who: stamps[who])
        gap = (
            None
            if manual is None or auto is None
            else round(abs(minutes_since(manual, auto)), 1)
        )
        per_source[name] = {
            "author": author,
            "minutes_ago": round(minutes_since(stamps[author], now), 1),
            "separation_minutes": gap,
        }
    ages = [value["minutes_ago"] for value in per_source.values() if value["minutes_ago"] is not None]
    separations = [
        value["separation_minutes"] for value in per_source.values() if value["separation_minutes"] is not None
    ]
    return {
        "sources": len(per_source),
        "per_source": per_source,
        "auto": sorted(name for name, value in per_source.items() if value["author"] == "auto"),
        "never_statted": sorted(name for name, value in per_source.items() if value["author"] == "never"),
        "newest_minutes": min(ages) if ages else None,
        # how far apart the two authors were, over the sources where both wrote: the finding is "minutes",
        # and only a difference survives a server that renders its stamps in +03:30
        "separation_minutes": min(separations) if separations else None,
    }


def provenance_line(prov: dict | None) -> str | None:
    """The witness line naming the author of the statistics the `before` state ran against, or None.

    The third mechanism read, after `inert_line` (what a setting could do) and `stats_line` (what an
    estimate still owed): this one says whether the pair measured `ANALYZE` at all. It exists because a
    missing line now means the opposite of what it meant in run three -- `statistics already current`
    stopped being a rendering glitch and became a finding -- so a reader who has to run their own catalog
    query to know what `before` was is one step away from quoting it as a stale frame.
    """
    if prov is None:
        return None
    if prov["auto"]:
        return (
            f"  before was not a stale frame: autovacuum holds the newest statistics on "
            f"{name_list(prov['auto'])} ({len(prov['auto'])} of {prov['sources']}; newest "
            f"{prov['newest_minutes']:.0f} min ago), so the `ANALYZE` step ran on numbers somebody else "
            "had already refreshed. Read the before -> after pair as the same statistics twice -- and "
            "reconcile a `before` that looks slow with that: the only state that measures the build's own "
            "statistics is a rebuild without an `ANALYZE`"
            + (
                f" (the two stamps sit {prov['separation_minutes']:.0f} min apart, and a difference between "
                "them is the one number here no timezone can move)"
                if prov["separation_minutes"] is not None
                else ""
            )
        )
    if prov["never_statted"] and len(prov["never_statted"]) == prov["sources"]:
        return (
            f"  no statistics at all: none of the {prov['sources']} sources carries an `analyze` or an "
            "`autoanalyze` stamp, so this is the one situation a manual `ANALYZE` can price -- the "
            "estimates the `before` state ran against are the planner's defaults, and a plan that moves "
            "here is a real move"
        )
    return (
        f"  the newest statistics on all {prov['sources']} sources are from a manual ANALYZE (newest "
        f"{prov['newest_minutes']:.0f} min ago) -- which this script's own previous round writes, so "
        "`before` is the frame as the last `ANALYZE` left it and not as the build did. autovacuum has had "
        "no turn since, so nothing here separates the two and the pair cannot price the step either way"
    )


def plan_changed(before: str, after: str) -> bool:
    """Whether the two states ran *different* plans, which is what a setting has to do to be credited.

    `plan_shape`, not `plan_fingerprint`: a wall clock is not a plan. Comparing the fingerprints made this
    answer True for two byte-identical plans that merely ran at different times, which is how a live run
    reported "plan moved across the setting" above a plan with the same costs and the same one worker.
    """
    return plan_shape(before) != plan_shape(after)


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
        "spread_ms": round(max(durations) - best, 3),
        "ceiling_ms": ceiling_ms,
        "ceiling_margin_ms": round(ceiling_ms - best, 3),
        "passes": best <= ceiling_ms,
    }


def state_line(label: str, state: dict) -> str:
    """One measured state, printed with the two numbers that say how much of it is a fluke."""
    n = len(state["runs_ms"])
    text = (
        f"{label}: best {state['best_ms']:.1f} ms, mean {state['mean_ms']:.1f} ms, "
        f"worst {state['worst_ms']:.1f} ms, runs {state['runs_ms']}; "
        f"{state['below_ceiling']}/{n} repeats under the ceiling, fastest was repeat "
        f"{state['best_repeat']} of {n}"
    )
    if state.get("read_ms") is not None:
        text += f" | read with transfer, one sample: {state['read_ms']:.1f} ms"
    if state.get("trend_ms") is not None:
        verdict_word = (
            "the ceiling is inside this state's own interval"
            if state["straddles_ceiling"]
            else "the ceiling is outside it"
        )
        text += f" | spread {state['spread_ms']:.1f} ms, drift {state['trend_ms']:+.1f} ms, {verdict_word}"
    return text


def noise_line(which: str, gap: dict | None) -> str:
    """The bound a comparison has to clear before its number means anything.

    Printed under both `plan moved` lines, because the answer is usually the interesting one: the pair
    spread more than the gap between them, and no amount of confidence in the difference changes that.
    """
    if gap is None:
        return f"  noise bound ({which}): no second state to compare against"
    return (
        f"  noise bound ({which}): {abs(gap['observed_ms']):.1f} ms observed against "
        f"{gap['spread_ms']:.1f} ms of spread inside {gap['spread_of']}"
        + (" -- the gap is inside the noise" if gap["inside_noise"] else " -- the gap clears it")
    )


def bounds_lines(gap_analyze: dict | None, gap_setting: dict | None) -> list[str]:
    """The two bound lines, in the order the witness carries them.

    Composed here rather than inline in `main()`, because `main()` needs a database to reach its own
    witness: a claim as load-bearing as "every comparison in this file states the noise it has to clear"
    should not rest on a line appended inside a 150-line function that no test can call.
    """
    lines = [noise_line("the ANALYZE", gap_analyze)]
    if gap_setting is not None:
        lines.append(noise_line("the setting", gap_setting))
    return lines


def attribution(
    *,
    plans_changed: bool,
    below_ceiling: int,
    repeats: int,
    ordering_share: float | None = None,
    gap_ms: float | None = None,
    spread_ms: float | None = None,
) -> str:
    """What a state that met the ceiling may be called. Six answers, and only two of them are a pass.

    `noise` is the newest and the cheapest: it fires when the gap between two states is smaller than the
    spread *inside* one of them, which needs no extra run and no control arm -- it only refuses to call a
    number an effect. `ordering` is the version of the same doubt that a control arm priced.

    `credited` needs the plan to have moved; `conditional` is the honest label for the case a live pair of
    runs produced -- the same plan in both states, yet a majority of the state's own repeats under the
    ceiling, which is reproducible and unexplained at once; `uncredited` is a pass resting on one fast
    repeat; `refuted` needs no sentence. `ordering` outranks the two that met the ceiling when a control
    arm was taken and most of the gap was paid for by measuring later: a number that only appears when the
    session is warm is not an effect of the condition, whatever the plan did.
    """
    if below_ceiling == 0:
        return "refuted"
    if gap_ms is not None and spread_ms is not None and abs(float(gap_ms)) < float(spread_ms):
        return "noise"
    if ordering_share is not None and ordering_share >= 0.5:
        return "ordering"
    if plans_changed:
        return "credited"
    if below_ceiling * 2 > repeats:
        return "conditional"
    return "uncredited"


def exit_note(
    before: dict,
    after: dict,
    kind_setting: str,
    control: dict | None,
    tuned: dict | None = None,
) -> tuple[int, str]:
    """Whether the run may exit green, and the line that says why not.

    `0041` refused the *prose* of a pass that only the minimum of a straddling state supports, and left the
    exit code on the old rule -- so his `--control` run printed "the line is unresolved on this machine"
    and returned 0, which is the exact combination a harness reads as success. The judged statistic and the
    exit code have to be refused by the same test: a state counts here only if it met the ceiling outside
    its own interval, which is the same sentence `describe()` already refuses to print without it.

    `0042` applied that rule to `before` and `after` only -- the before -> ANALYZE pair -- and so refused
    his 15-repeat run for the wrong reason: the states that *did* meet the line outside their own interval
    were the tuned one (`15/15`, best 451.6, spread 11.5 ms) and the control arm (`15/15`, spread 25.7 ms),
    and the rule never looked at either. Two separate questions live in one exit code, and only one of them
    is attribution: *did the frame meet the ceiling under these conditions* (any state's own repeats answer
    that), versus *which condition moved it* (the plan and the control arm answer that, in the sentences
    above). So every measured state is asked the first question here, and the second is named in the note
    when it disagrees -- which is also why a green exit now carries a `note:` line instead of silence: the
    pass is real, and "by the setting" is not.
    """
    states = [state for state in (before, after, tuned, control) if state is not None]
    met = [
        name
        for name, state in zip(("the untuned state", "the ANALYZE state", "the tuned state", "the control"),
                               (before, after, tuned, control))
        if state is not None and state["passes"] and not straddles_ceiling(state)
    ]
    if met or kind_setting in ("credited", "conditional"):
        # read as a sentence, not as a list dump: "the tuned state, the control met" is a comma splice in
        # the one line of this file a reader is most likely to copy into `report.md`
        who = (
            "" if not met else met[0] if len(met) == 1 else " and ".join([", ".join(met[:-1]), met[-1]])
        )
        note = (
            ""
            if not met  # granted on attribution alone: there is no interval to name, so nothing to say
            else f"note: {who} met {before['ceiling_ms']:.0f} ms outside its own interval, so "
            "the ceiling is met on this machine at this repeat count -- but the attribution above is what "
            "`report.md` copies, and `noise` or `ordering` there means this is not a fix *by the setting*"
        )
        return 0, note
    arm = (
        ""
        if control is None
        else ", and the control arm is what priced the gap rather than the condition"
    )
    return (
        1,
        f"red: no state met {before['ceiling_ms']:.0f} ms outside its own interval{arm} -- the ceiling is "
        "unresolved on this machine at this repeat count, so this is not a pass to report and not a "
        "breach to fix yet: take more repeats on the same command, or carry the shortfall into "
        "section 8 of `report.md` as the unresolved line the witness already spells out",
    )


def ordering_sentence(split: dict, state: str, judged: dict, repeats: int) -> str:
    """The one sentence that speaks about *this* pair, in both of its numbers.

    Deliberately no `best_ms` of either state in here: the control prices the gap between the state
    *before the condition* and the state *inside* it, which is not the before -> after pair `describe()`
    is otherwise asked about, and quoting both would stitch two gaps into one story. What the judged
    state owes the sentence is its own count over its own repeats -- `0/9` is the fact that made his
    `with the setting applied:` line a copy of the line above it.
    """
    return (
        f"ordering: the {split['naive_ms']:.1f} ms between the untuned state and `{state}` is not "
        f"the condition\'s doing -- the control, which is that untuned state measured again at the end "
        f"of the session with the condition back off, came to within {split['attributed_ms']:.1f} ms "
        f"of the tuned one. So {split['ordering_ms']:.1f} ms ({split['ordering_share']:.0%}) of the "
        f"gap is the tail of warming, and {split['attributed_ms']:.1f} ms is not enough to write the "
        f"ceiling off; `{state}` itself put {judged['below_ceiling']} of {repeats} of its own repeats "
        f"under {judged['ceiling_ms']:.0f} ms. Take more repeats, or carry the shortfall"
    )


def own_count(before: dict, after: dict) -> str:
    """One denominator per fraction, and each state named as the state it belongs to.

    The two witness lines compare `(untuned, ANALYZE)` and `(untuned, tuned)`, so a bare "2/9 and 0/9"
    reads as a pair of facts about one comparison. Naming the owners is what keeps the tuned state's `0/9`
    -- the row his run was missing entirely -- from looking like a second copy of the line above it.
    """
    return (
        f"{before['below_ceiling']}/{len(before['runs_ms'])} of the untuned state and "
        f"{after['below_ceiling']}/{len(after['runs_ms'])} of the state under test"
    )


def describe(
    before: dict,
    after: dict,
    *,
    state: str = "ANALYZE",
    plans_changed: bool = True,
    split: dict | None = None,
    gap: dict | None = None,
    inert: dict | None = None,
) -> str:
    """The sentence the report needs, in the shape of a decision rather than a number.

    `state` names what changed between the two measurements, because the remedy differs entirely: warm
    statistics, a session setting, or nothing at all. `plans_changed` is the attribution gate: a delta
    can only be credited to a condition that changed the plan it ran under. Without it, a single fast
    repeat at the end of a warmed-up session reads as a cure -- which is exactly what one live run of
    `--set max_parallel_workers_per_gather=4` looked like before anyone compared the two plans.

    The sentence also opens with the word `attribution()` returns -- `credited`, `conditional`,
    `uncredited` -- because the witness prints both, and a reader who sees `(conditional)` above a prose
    that says `artefact` is left with two vocabularies for one run. `refuted` is the exception: a state
    where no repeat met the ceiling opens with the word the report acts on, `query finding`.
    """
    n = len(after["runs_ms"])
    mechanism = (
        f"; and the plan says why: the planner asked for {inert['planned']} worker"
        f"{'' if inert['planned'] == 1 else 's'}, under a cap of {inert['cap']} it never reached -- so this "
        "condition has no mechanism here and no repeat count will give it one"
        if inert is not None and not inert["binding"]
        else ""
    )
    if gap is None:
        gap = {
            "observed_ms": round(before["best_ms"] - after["best_ms"], 3),
            "spread_ms": max(interval_ms(before), interval_ms(after)),
            "spread_of": "the untuned state" if interval_ms(before) >= interval_ms(after) else "that state",
        }
    kind = attribution(
        plans_changed=plans_changed,
        below_ceiling=after["below_ceiling"],
        repeats=n,
        ordering_share=None if split is None else split["ordering_share"],
        gap_ms=gap["observed_ms"],
        spread_ms=gap["spread_ms"],
    )
    if kind == "ordering":
        # the control arm is judged before the two branches on `before` below, because it is the only one
        # of the three that speaks about *this* pair. A priced ordering also outranks a straddling
        # untuned state: the straddle sentence is about the first state, and printing it twice -- once
        # under the ANALYZE verdict and again under `with the setting applied:` -- says the same thing
        # about two different comparisons and leaves the priced one unsaid. `ordering` is a verdict the
        # untuned state cannot speak for, so it returns where it is computed rather than at the bottom.
        return ordering_sentence(split, state, after, n)
    if before["passes"] and not straddles_ceiling(before):
        return (
            f"finding: the frame already meets the ceiling ({before['best_ms']:.1f} ms best of "
            f"{before['below_ceiling']}/{len(before['runs_ms'])} repeats under "
            f"{before['ceiling_ms']:.0f} ms); this run is context, not a fix"
        )
    if before["passes"] and kind != "refuted" and split is None:
        # ...but "meets" needs the state to have met it all the way through. His nine repeats read 467.3
        # to 599.8 ms against a 500 ms ceiling: the same session holds a pass and a breach of the same
        # query, and picking the fast one is the 0030 rule again -- `min` over a sample is a statistic
        # about the sample, and here the sample says both. This branch refuses the sentence rather than
        # decorating it, because the sentence is what the report copies. `refuted` is the one kind word it
        # must not swallow: with a control arm in hand the pair below has a priced sentence of its own, and
        # printing this one twice for two different comparisons left the tuned state's `0/9` unsaid. The
        # `split is None` half of the test is what `0042` got wrong: it excluded only `refuted`, so his
        # 15-repeat `noise` pair -- priced by the arm, and with a `15/15` tuned state to report -- was
        # swallowed again. No arm, no price: then the straddle really is this pair's own finding.
        return (
            f"noise: the untuned state both met and missed {before['ceiling_ms']:.0f} ms -- best "
            f"{before['best_ms']:.1f}, worst {before['worst_ms']:.1f}, {before['below_ceiling']} of "
            f"{len(before['runs_ms'])} repeats under -- so the ceiling is inside the measurement, and this "
            "run cannot write the frame off in either direction. The line is unresolved on this machine at "
            "this repeat count; say that, not `meets`, not `breaches`"
        )
    if kind == "noise":
        # each state's own count over its own repeats: quoting "1 of 5" from a nine-repeat state would be
        # the same class of error this whole function exists to stop
        price = ""
        if split is not None:
            # once a control arm has priced the gap, "this pair never measured the condition" and "the gain
            # you were about to credit is ordering" are the same finding -- and with a *negative* gap
            # (his 15-repeat run: the tuned state was 3.4 ms slower than the ANALYZE state) there is no
            # gain to price at all, so the arm says nothing is creditable rather than quoting a share of it
            price = (
                f", and the control arm says {split['ordering_share']:.0%} of the gap is ordering: "
                f"{split['attributed_ms']:.1f} ms is what is left for the condition"
                if split["ordering_share"] > 0
                else ", and the control arm says there is nothing here to credit"
            )
        straddle = ""
        if before["passes"] and split is not None and not after["passes"]:
            # the untuned state still both met and missed the line, and it is worth naming here -- as a
            # clause of this pair, not as a replacement for it. With no control arm the branch above owns
            # this fact; with one, the pair gets its own sentence and the straddle rides along as a clause.
            # (0041 left the straddle branch above ungated, so this branch never ran and
            # `with the setting applied:` printed the ANALYZE comparison's sentence verbatim.)
            straddle = (
                f"; the untuned state both met and missed {before['ceiling_ms']:.0f} ms (best "
                f"{before['best_ms']:.1f}, worst {before['worst_ms']:.1f})"
            )
        return (
            f"noise: the {abs(gap['observed_ms']):.1f} ms between the two states is smaller than the "
            f"{gap['spread_ms']:.1f} ms that {gap['spread_of']}'s own repeats spread, so this pair never "
            "measured the condition -- it measured how wide that state is. Nothing is owed to "
            f"`{state}`: {before['below_ceiling']}/{len(before['runs_ms'])} of the untuned repeats and "
            f"{after['below_ceiling']}/{n} of these ones met the ceiling"
            + (
                ", and a session that answers both ways is not a finding about the query"
                if bool(before["passes"]) != bool(after["passes"])
                else " -- both sides met it, so this pair measured the width of a band, not a movement"
            )
            + f"{price}{straddle}{mechanism}"
        )
    if kind == "conditional":
        return (
            f"conditional pass: {state} puts {after['below_ceiling']} of {n} of its own repeats under "
            f"{after['ceiling_ms']:.0f} ms (best {after['best_ms']:.1f}, worst {after['worst_ms']:.1f}) "
            f"while both states ran the same plan shape -- same `Workers Launched`, same estimates, same "
            f"spill. The number is reproducible and the mechanism is not, so `report.md` writes "
            f"\"meets the ceiling only under {state}\" and not \"fixed\": the step that ships the frame "
            "never issues this condition, and a verdict that needs one is a finding about the harness"
        )
    if kind == "uncredited":
        return (
            f"uncredited: {state} moves the fastest repeat from {before['best_ms']:.1f} ms to "
            f"{after['best_ms']:.1f} ms, but the two states ran the same plan shape -- same "
            f"`Workers Launched`, same spill markers -- so the delta is not {state}'s doing; and only "
            f"{after['below_ceiling']} of {n} repeats in that state were under the ceiling, with the "
            f"fastest as repeat {after['best_repeat']} of {n}. carry this as one fast outlier, not as "
            "a pass, and not as a condition the report can rely on"
            + mechanism
        )
    if kind == "credited" and after["passes"]:
        remedy = (
            "so the breach was a stale plan on the rebuilt snapshot, not a cost of the frame -- the "
            "remedy is to keep those statistics warm"
            if state == "ANALYZE"
            else "so the breach is a session condition rather than the frame itself -- carry the "
                 "setting into the report as the condition the number was measured under, and the "
                 "step that ships the frame has to issue it for the number to mean anything"
        )
        return (
            f"credited: {before['best_ms']:.1f} ms becomes {after['best_ms']:.1f} ms after {state}, "
            f"{remedy}"
        )
    if before["below_ceiling"] == 0 and after["below_ceiling"] == 0:
        spread = "; no repeat in either state met it"
    else:
        spread = f" ({own_count(before, after)} met the ceiling)"
    priced = ""
    if split is not None and split["ordering_share"] >= 0.5:
        # the same number that would have made this an `ordering` verdict, kept even though `refuted`
        # outranks it: "no repeat met the ceiling" and "the gain you were about to credit is ordering"
        # are two different findings, and dropping the second one is how the first gets read as a
        # surprise instead of as the price of a condition that never moved a plan
        priced = (
            f" -- and the control arm says the {split['naive_ms']:.1f} ms that `{state}` appears to "
            f"recover is {split['ordering_share']:.0%} ordering ({split['attributed_ms']:.1f} ms left "
            "for the condition)"
        )
    unresolved = ""
    if before["passes"]:
        # the untuned state both met and missed the line, and saying so *here* (a clause of this pair) is
        # the fix for the duplicate: the old code returned the straddle sentence instead of this one, so
        # `with the setting applied:` quoted the ANALYZE comparison's numbers and the tuned state's own
        # `0/9` -- the fact that made the row worth reading -- never appeared anywhere
        unresolved = (
            f"; and since the untuned measurement spans the line (best {before['best_ms']:.1f}, worst "
            f"{before['worst_ms']:.1f}, {before['below_ceiling']} of {len(before['runs_ms'])} of its own "
            "repeats under), this run says neither `meets` nor `breaches`"
        )
    return (
        f"query finding: {state} moves the fastest repeat from {before['best_ms']:.1f} ms to "
        f"{after['best_ms']:.1f} ms and the ceiling still fails{spread}{priced}, so `report.md` has to "
        f"carry this as a measured shortfall and the plan below is the starting point{unresolved}"
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
        help="base witness; --set and --control write <base>.tuned-<tag>.txt instead of over it",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite a witness measured at a different ceiling, row count or tuning",
    )
    parser.add_argument(
        "--control",
        action="store_true",
        help="after the last measurement, RESET the settings and measure the same state once more, so "
        "a gain that is really the tail of warming can be priced instead of argued about",
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

    tagged = tag_settings(settings, control=args.control)
    witness = witness_path(args.evidence, tagged)
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
            print(refusal(witness, stored, claims, witness_path(DEFAULT_EVIDENCE, tagged)))
            return 2

        # read the stamps first: the question is who left the statistics the `before` state is about to
        # run against, and this run's own `ANALYZE` is one of the possible answers
        cursor.execute(statistics_query(ANALYZE_TARGETS))
        prov = statistics_provenance(cursor.fetchall(), now=datetime.now(timezone.utc))
        prov_text = provenance_line(prov)
        before_runs = measure(cursor, frame, repeat=args.repeats, clock=time.perf_counter)
        plan_before = read_plan(cursor, frame)
        read_before, read_rows = measure_read(cursor, frame, clock=time.perf_counter)
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
        read_after, _ = measure_read(cursor, frame, clock=time.perf_counter)
        tuned_runs = None
        plan_tuned = ""
        read_tuned = None
        if settings:
            for statement in setting_lines(settings):
                cursor.execute(statement)
            tuned_runs = measure(cursor, frame, repeat=args.repeats, clock=time.perf_counter)
            plan_tuned = read_plan(cursor, frame)
            read_tuned, _ = measure_read(cursor, frame, clock=time.perf_counter)
        control_runs = None
        if args.control:
            # the arm is only worth taking if it re-measures the state whose gap it prices: put the session
            # back the way the untuned states found it, then read that state again, last
            for statement in reset_lines(settings):
                cursor.execute(statement)
            connection.commit()
            control_runs = measure(cursor, frame, repeat=args.repeats, clock=time.perf_counter)
        # the plan the error line asked for is the one from the state the run ends in
        plan = plan_tuned or plan_after
    finally:
        connection.close()

    before = verdict(before_runs, ceiling)
    after = verdict(after_runs, ceiling)
    tuned = verdict(tuned_runs, ceiling) if tuned_runs else None
    control = verdict(control_runs, ceiling) if control_runs else None
    before["read_ms"], after["read_ms"] = round(read_before, 3), round(read_after, 3)
    if tuned is not None and read_tuned is not None:
        tuned["read_ms"] = round(read_tuned, 3)
    for state in (before, after, tuned, control):
        if state is not None:
            state["trend_ms"] = trend_ms(state)
            state["straddles_ceiling"] = straddles_ceiling(state)
    gap_analyze = gap_pair(before, after, first_name="the untuned state", second_name="the ANALYZE state")
    gap_setting = (
        gap_pair(after, tuned, first_name="the ANALYZE state", second_name="the tuned state")
        if tuned is not None
        else None
    )
    # the control re-measures `after`, so the gap it prices is the after -> tuned one: the setting's claim
    split = (
        gain_split(
            naive_ms=after["best_ms"] - tuned["best_ms"],
            drift_ms=after["best_ms"] - control["best_ms"],
        )
        if tuned is not None and control is not None
        else None
    )
    # the two verdicts are computed once, so the .txt and the .json cannot disagree about what was found
    moved_analyze = plan_changed(plan_before, plan_after)
    moved_setting = plan_changed(plan_after, plan_tuned) if tuned is not None else False
    kind_analyze = attribution(
        plans_changed=moved_analyze,
        below_ceiling=after["below_ceiling"],
        repeats=len(after["runs_ms"]),
        gap_ms=gap_analyze["observed_ms"],
        spread_ms=gap_analyze["spread_ms"],
    )
    kind_setting = (
        attribution(
            plans_changed=moved_setting,
            below_ceiling=tuned["below_ceiling"],
            repeats=len(tuned["runs_ms"]),
            ordering_share=None if split is None else split["ordering_share"],
            gap_ms=gap_setting["observed_ms"],
            spread_ms=gap_setting["spread_ms"],
        )
        if tuned is not None
        else "not measured"
    )
    # mechanism before arithmetic: what the plan says the setting could do at all, and what the estimate
    # says is left for ANALYZE to correct on this database. Both are read from text already in hand, so
    # neither depends on how many repeats the run took
    inert = cap_binding(
        settings,
        {"before": plan_before, "after": plan_after, "tuned": plan_tuned} if tuned is not None
        else {"before": plan_before, "after": plan_after},
    )
    drift = estimate_drift(plan_before)
    inert_text = inert_line(inert)
    stats_text = stats_line(drift, moved_analyze, ceiling)
    verdict_text = describe(before, after, plans_changed=moved_analyze, gap=gap_analyze)
    verdict_tuned_text = (
        describe(
            before,
            tuned,
            state="ANALYZE + SET " + tuning,
            plans_changed=moved_setting,
            split=split,
            gap=gap_setting,
            inert=inert,
        )
        if tuned is not None
        else None
    )
    lines = [
        "# ceiling on the feature frame, measured across one ANALYZE (scripts/analyze_ceiling_feature.py)",
        f"config {args.config.name}; frame {frame}; ceiling {int(ceiling)} ms; frame rows {rows}"
        + (f"; tuned by {tuning}" if tuning else ""),
        f"basis: {features.TIMING_BASIS}, the same statistic the feature step judges on",
        *(
            [
                "control: the last untuned state, measured again after RESET -- what that recovers by "
                "itself is what this run refuses to credit to the condition"
            ]
            if args.control
            else []
        ),
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
        f"    fingerprint: {'; '.join(plan_fingerprint(plan_before))}",
        f"  after  ANALYZE: {plan_summary(plan_after)}",
        f"    fingerprint: {'; '.join(plan_fingerprint(plan_after))}",
        *(
            [
                f"  tuned ({tuning}): {plan_summary(plan_tuned)}",
                f"    fingerprint: {'; '.join(plan_fingerprint(plan_tuned))}",
            ]
            if tuned is not None
            else []
        ),
        f"  plan moved across the ANALYZE: {moved_analyze} ({kind_analyze})",
        *( [stats_text] if stats_text else [] ),
        *( [prov_text] if prov_text else [] ),
        "  transfer: the judged statistic discards the rows on the server (`features.FRAME_TIMING_SQL`),"
        f" so the {read_rows:,} rows the frame returns are priced only by the read sample above",
        "",
        verdict_text,
    ]
    if tuned is not None:
        lines += [
            state_line(f"after  ANALYZE + SET {tuning}", tuned),
            f"  plan moved across the setting: {moved_setting} ({kind_setting})",
            *( [inert_text] if inert_text else [] ),
            f"with the setting applied: {verdict_tuned_text}",
        ]
    lines += bounds_lines(gap_analyze, gap_setting)
    if control is not None:
        lines += [state_line("control (ANALYZE, no setting, measured last)", control)]
    if split is not None:
        # a `--control` run with no `--set` has a second measurement of the same state and nothing to
        # divide: the gap that would be priced belongs to the setting, and it does not exist here
        priced = (
            f" -- inside the {gap_setting['spread_ms']:.1f} ms spread of the states being compared, so "
            "this line prices a gap neither arm could have resolved"
            if gap_setting is not None and abs(split["naive_ms"]) < gap_setting["spread_ms"]
            else ""
        )
        lines += [
            f"  gain split: {split['naive_ms']:.1f} ms observed = {split['attributed_ms']:.1f} ms "
            f"credited to the condition + {split['ordering_ms']:.1f} ms ordering "
            f"({split['ordering_share']:.0%} of the gap){priced}",
        ]
    lines += [
        "",
        "## the plan the error line asked for, read after the last measurement in "
        + ("the tuned state " + f"({tuning})" if tuned is not None else "the untuned state"),
        plan,
    ]
    code, note = exit_note(before, after, kind_setting, control, tuned)
    if note:
        # both colours go in the witness, not only on the terminal: a red exit nobody can read is a red
        # exit that gets "fixed" by dropping the flag that made it honest, and a green exit with no
        # sentence beside it is a pass that gets quoted without its attribution -- the file is what a
        # reader re-opens, so the exit's *reason* travels with it either way
        lines.append(note)
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
                "control": control,
                "gain_split": split,
                "plan_shapes": {
                    "before_analyze": plan_summary(plan_before),
                    "after_analyze": plan_summary(plan_after),
                    "tuned": plan_summary(plan_tuned) if tuned is not None else None,
                },
                # the comparison itself, in machine-readable form: diff these three lists instead of the
                # ~100-line plan when the question is "did anything about the plan move?"
                "plan_shapes_compared": {
                    "before_analyze": plan_shape(plan_before),
                    "after_analyze": plan_shape(plan_after),
                    "tuned": plan_shape(plan_tuned) if tuned is not None else None,
                },
                "plans_moved": {"analyze": moved_analyze, "setting": moved_setting},
                # the same two questions answered from the plan text instead of the clock: what the
                # planner asked for against the cap it was given, and how far the untuned estimate was
                # from the rows the same run counted
                "parallel_cap": inert,
                "plan_estimate_drift": drift,
                # and who left the statistics the `before` state ran against: autovacuum, a previous
                # manual pass, or nobody -- the catalog's answer, kept next to the two mechanism reads
                "statistics_provenance": prov,
                "witness_lines": {
                    "setting_inert": inert_text,
                    "statistics": stats_text,
                    "provenance": prov_text,
                },
                "attribution": {"analyze": kind_analyze, "setting": kind_setting},
                "noise_bounds": {"analyze": gap_analyze, "setting": gap_setting},
                "frame_rows_read": read_rows,
                "verdict": verdict_text,
                "verdict_tuned": verdict_tuned_text,
                "exit": {"code": code, "note": note},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\n".join(lines))
    print(f"\nwrote {shown(witness)} and {shown(witness.with_suffix('.json'))}")
    # `code` is the one `exit_note` computed above: an exit code is read as a verdict, not as a number, and
    # it must be the same verdict the file and the terminal just printed
    return code


if __name__ == "__main__":
    raise SystemExit(main())
