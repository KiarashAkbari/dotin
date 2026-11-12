"""Turn the evidence file into report.md's numbers, and refuse untraceable ones.

The delivery guide's rule is that every number in the report must be traceable to a
notebook output. That is checkable, so it is checked: :func:`check_traceability` takes
the report text and the evidence payload and returns every number in the text that
does not appear in the payload. A report written this way cannot quietly contain a
remembered figure, a rounded-up claim, or a number from a run that was later changed -
each of those shows up as an untraceable token, in a test, before a human reads it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from .measure import numbers

#: numbers that are definitions rather than measurements: the cost matrix, the report
#: scale, the threshold grid, the acceptance bar, the fold count, row counts of test
#: fixtures, and versions. Anything else in the report has to come from the evidence.
ALLOWED_TOKENS: frozenset[str] = frozenset(
    {
        "0",
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
        "10",
        "12",
        "20",
        "25",
        "30",
        "50",
        "100",
        "1000",
        "150000",
        "0.05",
        "0.5",
        "1.0",
        "201",
        "20261103",
        "429",
        "3.12",
        "0.25",
        "0.01",
        "0.10",
        "0.20",
        "0.60",
        "0.02",
        "1.6",
        "0.15",
        "0.0",
    }
)
#: ``2026``-like years and section numbers are prose, not findings
YEAR_PATTERN = re.compile(r"^(19|20)\d{2}$")
NUMBER_PATTERN = re.compile(r"-?\d[\d,_]*(?:\.\d+)?")
PLACEHOLDER_PATTERN = re.compile(r"\{\{([a-z0-9_.\[\]-]+)\}\}")


def render_numbers_table(
    evidence: dict, *, preferred: Sequence[str] | None = None
) -> str:
    """A markdown table of the flattened evidence, keys in sorted order.

    ``preferred`` puts the headline rows first - the same payload, ordered the way the
    report reads it, so the table in ``report.md`` and the table printed by the
    notebook are the same table.
    """
    flat = numbers(evidence)
    keys = list(flat)
    if preferred:
        head = [key for key in preferred if key in flat]
        keys = head + [key for key in sorted(set(keys) - set(head))]
    else:
        keys = sorted(keys)
    lines = ["| evidence key | value |", "|---|---|"]
    for key in keys:
        value = flat[key]
        rendered = f"{value:g}" if isinstance(value, float) else str(value)
        lines.append(f"| `{key}` | {rendered} |")
    return "\n".join(lines)


def numeric_tokens(markdown: str) -> list[str]:
    """Every number written in the text, in order, thousands separators removed."""
    tokens: list[str] = []
    for raw in NUMBER_PATTERN.findall(markdown):
        tokens.append(raw.replace("_", "").replace(",", ""))
    return tokens


def _candidate_spellings(value: object) -> set[str]:
    """How one measured value can legitimately be written in prose.

    A float in the evidence is stored at full precision and quoted rounded; a
    percentage is stored as a fraction or vice versa. Both are the same finding, so
    both spellings count - and nothing else is allowed, which is the point.
    """
    out: set[str] = set()
    if isinstance(value, bool):
        return out
    if isinstance(value, int):
        out.add(str(value))
        return out
    if not isinstance(value, (int, float)):
        return out
    number = float(value)
    for places in (0, 1, 2, 3, 4, 6):
        rounded = round(number, places)
        out.add(
            f"{rounded:.{places}f}".rstrip("0").rstrip(".")
            if places
            else f"{int(rounded)}"
        )
        out.add(f"{rounded:g}")
        percent = round(number * 100, places)
        out.add(f"{percent:g}")
    return (
        {token for token in out if token not in {"", "-", "0"}} | {"0"}
        if number == 0
        else out
    )


def check_traceability(
    markdown: str,
    evidence: dict,
    *,
    allowed: frozenset[str] = ALLOWED_TOKENS,
) -> list[str]:
    """Numbers in the report that the evidence does not contain, deduplicated.

    An empty list is the claim "no number here was invented". The exceptions are
    :data:`ALLOWED_TOKENS` (definitions: the cost matrix, the grid size, the report
    scale) and four-digit years, which are prose.
    """
    spellings: set[str] = set(allowed)
    for value in numbers(evidence).values():
        spellings |= _candidate_spellings(value)
    for entry in _string_values(evidence):
        spellings |= {token for token in NUMBER_PATTERN.findall(entry)}
    untraceable: list[str] = []
    for token in numeric_tokens(markdown):
        if token in spellings or YEAR_PATTERN.match(token):
            continue
        if token not in untraceable:
            untraceable.append(token)
    return untraceable


def _string_values(evidence: dict) -> list[str]:
    """Text fields in the payload (file names, hashes) - their digits count as measured."""
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str):
            found.append(node)

    walk(evidence)
    return found


#: the five decisions the report has to defend, as data so the notebook, the report and
#: the tests quote the same options and the same evidence keys
KEY_DECISIONS: tuple[dict[str, object], ...] = (
    {
        "id": "sentinel-policy",
        "question": "What are 96 and 98 in the three late-payment columns?",
        "options": [
            "ordinary counts - keep them as numbers",
            "codes for a separate state - map to NULL and keep the raw column",
            "data corruption - reject the rows",
        ],
        "choice": "codes: mapped to NULL in the view, kept verbatim in the table",
        "evidence": ["queries.sentinel_spikes.rows", "load_run.rejected_count"],
        "cost": "the row-level default rate at the code is what makes 'reject' expensive to defend",
    },
    {
        "id": "missing-income",
        "question": "How should unreported monthly income be prepared?",
        "options": [
            "median imputation plus a missingness indicator",
            "zero, on the reading that unreported means no reported income",
            "drop the rows",
        ],
        "choice": "median plus indicator for the linear model, no indicator as the compared alternative",
        "evidence": ["queries.missingness_parity.rows", "prep.fitted_rows"],
        "cost": "dropping rows would remove the poorest-documented applicants from training entirely",
    },
    {
        "id": "threshold-rule",
        "question": "Which operating point does the model use?",
        "options": [
            "the default 0.5",
            "the minimum of the validation cost curve",
            "the F1 maximum",
        ],
        "choice": "validation cost-curve minimum on a 201-point grid, ties to the lower threshold",
        "evidence": ["threshold.value", "holdout.cost_per_1000"],
        "cost": "0.5 is a statement about equal costs, and this matrix is not equal costs",
    },
    {
        "id": "model-selection",
        "question": "What decides between the two families?",
        "options": ["AUC", "accuracy", "mean per-fold cost with its spread"],
        "choice": "mean per-fold cost, spread reported, worst fold reported",
        "evidence": [
            "cv.logistic.mean_cost_per_1000",
            "cv.boosted_trees.mean_cost_per_1000",
        ],
        "cost": "the acceptance bar is a cost reduction, so anything else selects on the wrong axis",
    },
    {
        "id": "engineered-features",
        "question": "Which engineered features survive?",
        "options": [
            "all of them, because they are plausible",
            "only those whose ablation changes measured cost",
            "none, use the file as delivered",
        ],
        "choice": "ablation on cross-validated cost; the ones that do not pay are dropped and named",
        "evidence": ["ablation.full_cost", "ablation.dropped"],
        "cost": "a feature that does nothing still costs review time and a leakage-audit line",
    },
)


def decisions_table(evidence: dict) -> str:
    """The decisions section, rendered with the evidence keys each one is argued from."""
    flat = numbers(evidence)
    lines = [
        "| decision | options considered | choice | evidence key |",
        "|---|---|---|---|",
    ]
    for decision in KEY_DECISIONS:
        keys = [str(key) for key in decision["evidence"]]
        values = ", ".join(
            f"`{key}`={flat[key]:g}" if key in flat else f"`{key}`" for key in keys
        )
        options = "; ".join(str(option) for option in decision["options"])
        lines.append(
            f"| {decision['id']} | {options} | {decision['choice']} | {values} |"
        )
    return "\n".join(lines)


def fill_placeholders(markdown: str, evidence: dict) -> str:
    """Replace ``{{evidence.key}}`` slots with the measured value.

    The report is authored with slots instead of numbers, so a number cannot exist in
    the prose before the run that produced it. An unknown key is left visibly broken
    rather than dropped: a silent empty slot in a report is worse than an ugly one.
    """
    flat = numbers(evidence)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in flat:
            return f"\u26a0\ufe0f missing({key})"
        value = flat[key]
        if isinstance(value, float):
            return (
                f"{value:.2f}".rstrip("0").rstrip(".")
                if abs(value) < 1_000_000
                else f"{value:,.2f}"
            )
        return f"{value:,}" if isinstance(value, int) else str(value)

    return PLACEHOLDER_PATTERN.sub(replace, markdown)


def apply_to_file(path: Path, evidence: dict) -> dict[str, object]:
    """Fill the report in place and report what it cost: slots, gaps, leftovers."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    slots = len(PLACEHOLDER_PATTERN.findall(text))
    filled = fill_placeholders(text, evidence)
    missing = filled.count("\u26a0\ufe0f")
    if filled != text:
        path.write_text(filled, encoding="utf-8")
    return {
        "slots": slots,
        "unresolved": missing,
        "changed": filled != text,
        "untraceable": check_traceability(filled, evidence),
    }
