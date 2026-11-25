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


def describe(
    before: dict,
    after: dict,
    *,
    state: str = "ANALYZE",
    plans_changed: bool = True,
    split: dict | None = None,
    gap: dict | None = None,
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
    if before["passes"] and not straddles_ceiling(before):
        return (
            f"finding: the frame already meets the ceiling ({before['best_ms']:.1f} ms best of "
            f"{before['below_ceiling']}/{len(before['runs_ms'])} repeats under "
            f"{before['ceiling_ms']:.0f} ms); this run is context, not a fix"
        )
    if before["passes"]:
        # ...but "meets" needs the state to have met it all the way through. His nine repeats read 467.3
        # to 599.8 ms against a 500 ms ceiling: the same session holds a pass and a breach of the same
        # query, and picking the fast one is the 0030 rule again -- `min` over a sample is a statistic
        # about the sample, and here the sample says both. This branch refuses the sentence rather than
        # decorating it, because the sentence is what the report copies.
        return (
            f"noise: the untuned state both met and missed {before['ceiling_ms']:.0f} ms -- best "
            f"{before['best_ms']:.1f}, worst {before['worst_ms']:.1f}, {before['below_ceiling']} of "
            f"{len(before['runs_ms'])} repeats under -- so the ceiling is inside the measurement, and this "
            "run cannot write the frame off in either direction. The line is unresolved on this machine at "
            "this repeat count; say that, not `meets`, not `breaches`"
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
    if kind == "noise":
        # each state's own count over its own repeats: quoting "1 of 5" from a nine-repeat state would be
        # the same class of error this whole function exists to stop
        return (
            f"noise: the {abs(gap['observed_ms']):.1f} ms between the two states is smaller than the "
            f"{gap['spread_ms']:.1f} ms that {gap['spread_of']}'s own repeats spread, so this pair never "
            "measured the condition -- it measured how wide that state is. Nothing is owed to "
            f"`{state}`: {before['below_ceiling']}/{len(before['runs_ms'])} of the untuned repeats and "
            f"{after['below_ceiling']}/{n} of these ones met the ceiling, and a session that answers both "
            "ways is not a finding about the query"
        )
    if kind == "ordering":
        # deliberately no `before`/`after` numbers in here: the control prices the gap between the state
        # *before the condition* and the state *inside* it, which is not the before -> after pair this
        # function is otherwise asked about. Quoting both would print two different gaps as one story.
        return (
            f"ordering: the {split['naive_ms']:.1f} ms between the untuned state and `{state}` is not "
            f"the condition\'s doing -- the control, which is that untuned state measured again at the end "
            f"of the session with the condition back off, came to within {split['attributed_ms']:.1f} ms "
            f"of the tuned one. So {split['ordering_ms']:.1f} ms ({split['ordering_share']:.0%}) of the "
            f"gap is the tail of warming, and {split['attributed_ms']:.1f} ms is not enough to write the "
            "ceiling off: take more repeats, or carry the shortfall"
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
    verdict_text = describe(before, after, plans_changed=moved_analyze, gap=gap_analyze)
    verdict_tuned_text = (
        describe(
            before,
            tuned,
            state="ANALYZE + SET " + tuning,
            plans_changed=moved_setting,
            split=split,
            gap=gap_setting,
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
        "  transfer: the judged statistic discards the rows on the server (`features.FRAME_TIMING_SQL`),"
        f" so the {read_rows:,} rows the frame returns are priced only by the read sample above",
        "",
        verdict_text,
    ]
    if tuned is not None:
        lines += [
            state_line(f"after  ANALYZE + SET {tuning}", tuned),
            f"  plan moved across the setting: {moved_setting} ({kind_setting})",
            f"with the setting applied: {verdict_tuned_text}",
        ]
    lines += bounds_lines(gap_analyze, gap_setting)
    if control is not None:
        lines += [state_line("control (ANALYZE, no setting, measured last)", control)]
    if split is not None:
        # a `--control` run with no `--set` has a second measurement of the same state and nothing to
        # divide: the gap that would be priced belongs to the setting, and it does not exist here
        lines += [
            f"  gain split: {split['naive_ms']:.1f} ms observed = {split['attributed_ms']:.1f} ms "
            f"credited to the condition + {split['ordering_ms']:.1f} ms ordering "
            f"({split['ordering_share']:.0%} of the gap)",
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
                "attribution": {"analyze": kind_analyze, "setting": kind_setting},
                "noise_bounds": {"analyze": gap_analyze, "setting": gap_setting},
                "frame_rows_read": read_rows,
                "verdict": verdict_text,
                "verdict_tuned": verdict_tuned_text,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\n".join(lines))
    print(f"\nwrote {shown(witness)} and {shown(witness.with_suffix('.json'))}")
    # a tuned state exits green only when the condition is credited (the plan moved) or reproducible
    # (a majority of its own repeats met the ceiling); an `uncredited` pass is one fast repeat in a
    # warming session, and an exit code is read as a verdict, not as a number
    met = before["passes"] or after["passes"] or kind_setting in ("credited", "conditional")
    return 0 if met else 1


if __name__ == "__main__":
    raise SystemExit(main())
