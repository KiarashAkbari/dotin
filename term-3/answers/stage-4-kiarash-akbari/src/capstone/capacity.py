"""Capacity arithmetic: rank, cut at a share, count what was caught.

The brief's decision is a quota, not a probability threshold: the team can follow up
on 10 % of orders, so the system ranks and takes the top share. Keeping that in one
module means the 5 / 10 / 20 table, the secondary metric on the last month, and the
capacity paragraph of ``report.md`` all come from the same three lines of arithmetic.

The functions are pure and take plain sequences, so a notebook can hand them lists and
a test can hand them literals.
"""

from __future__ import annotations

from math import ceil

from .config import CapstoneConfig


#: where the final list goes, under features.curated_dir
FOLLOWUP_NAME = "followup_list.csv"
CAPACITY_NAME = "capacity_table.json"

LATEST_RUN_SQL = (
    "SELECT run_id, model, metrics FROM capstone.model_run ORDER BY created_at DESC, run_id DESC LIMIT 1"
)
#: the evaluation window of the latest run, ranked; ties break by order_id so a rerun is identical
RANKED_SQL = (
    "SELECT s.order_id, s.order_approved_at, s.score FROM capstone.order_score AS s "
    "WHERE s.run_id = %s AND s.window_name = 'eval' ORDER BY s.score DESC, s.order_id"
)


def report(config: CapstoneConfig) -> dict:
    """The ``capacity-list`` chain step: the 10 % follow-up list and the 5 / 10 / 20 table.

    Reads the latest ``capstone.model_run`` and its scored evaluation window from the database
    (the model step wrote them), writes ``followup_list.csv`` -- the top ``capacity.share`` of the
    window's approved orders, ranked -- and ``capacity_table.json`` with the table the operations
    manager decides with. The list is over *all* approved orders of the window, the way the team
    would receive it; the catch rates in the table are the model step's, over labelled orders
    (D2), with the all-approved variant beside them.
    """
    import csv
    import json

    from .features import connect, database_uri

    uri = database_uri(config, purpose="build the follow-up list")
    connection = connect(uri, purpose="read the scored frame for the follow-up list")
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT to_regclass('capstone.model_run') IS NOT NULL")
        if not cursor.fetchone()[0]:
            from .errors import NotReadyError

            raise NotReadyError("no capstone.model_run: run the model step first")
        cursor.execute(LATEST_RUN_SQL)
        latest = cursor.fetchone()
        if latest is None:
            from .errors import NotReadyError

            raise NotReadyError("capstone.model_run is empty: run the model step first")
        run_id, model_name, metrics = latest
        cursor.execute(RANKED_SQL, (run_id,))
        ranked = cursor.fetchall()
    finally:
        connection.close()
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    chosen = top_share_indexes(len(ranked), config.capacity.share)
    curated = config.features.curated_dir
    curated.mkdir(parents=True, exist_ok=True)
    target = curated / FOLLOWUP_NAME
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rank", "order_id", "order_approved_at", "score", "model_run_id"])
        for index in chosen:
            order_id, approved, score = ranked[index]
            writer.writerow([index + 1, order_id, approved.isoformat(sep=" "), f"{score:.6f}", run_id])
    evaluation = metrics["evaluation"][model_name]
    table = {
        "run_id": run_id,
        "model": model_name,
        "window": metrics["split"]["evaluation"],
        "share": config.capacity.share,
        "list_rows": len(chosen),
        "window_rows": len(ranked),
        "capacity_labelled": evaluation["capacity"],
        "capacity_all_approved": evaluation["capacity_all_approved"],
        "rule_baseline": {
            "rule": metrics["rule_baseline"]["chosen"]["rule"],
            "capacity_labelled": metrics["rule_baseline"]["evaluation"]["capacity"],
        },
    }
    (curated / CAPACITY_NAME).write_text(json.dumps(table, indent=2), encoding="utf-8")
    return {"list": str(target), "rows": len(chosen), "of": len(ranked), "run_id": run_id,
            "table": str(curated / CAPACITY_NAME)}


def top_share_indexes(size: int, share: float) -> list[int]:
    """Indexes of the top ``share`` of ``size`` rows, ties broken by input order.

    ``ceil`` rather than ``round``: a team with capacity for 10 % of 13 orders can
    still reach one more order, and dropping the remainder would quietly shrink the
    quota the report claims to have measured.
    """
    if size < 0:
        raise ValueError("size cannot be negative")
    if not 0.0 < share < 1.0:
        raise ValueError(f"share must be between 0 and 1, got {share}")
    if size == 0:
        return []
    return list(range(min(size, max(1, ceil(size * share)))))


def rank_then_select(scores: list[float], share: float) -> list[int]:
    """Indexes of the highest-scoring rows inside the quota, descending by score.

    Input order decides ties on purpose: it is the order the model produced, so a
    re-run with the same seed selects the same rows, and the selection is a function of
    the ranking alone rather than of a sort's stability.
    """
    if not scores:
        return []
    if not 0.0 < share < 1.0:
        raise ValueError(f"share must be between 0 and 1, got {share}")
    order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
    return order[: max(1, ceil(len(scores) * share))]


def catch_rate(negatives: list[bool], selected: list[bool]) -> float | None:
    """Share of negative reviews the selected rows caught -- the brief's main metric.

    ``None`` means the denominator was empty. It is reported rather than turned into
    0.0 or 1.0, because on a thin evaluation month "no negatives to catch" and "caught
    nothing" are different findings and the report has to be able to tell them apart.
    """
    if len(negatives) != len(selected):
        raise ValueError(
            f"negatives has {len(negatives)} rows, selected has {len(selected)}; they must be "
            "the same frame"
        )
    total = sum(1 for flag in negatives if flag)
    if not total:
        return None
    caught = sum(
        1 for negative, chosen in zip(negatives, selected, strict=True) if negative and chosen
    )
    return caught / total


def capacity_table(negatives: list[bool], scores: list[float], shares: list[float]) -> list[dict]:
    """The 5 / 10 / 20 table: quota, rows reviewed, negatives caught, extra work.

    ``extra`` is the number of selected rows that were not negative -- the follow-up the
    team spends for free, which is the half of the trade-off a recall-only table hides.
    """
    if len(negatives) != len(scores):
        raise ValueError("negatives and scores must line up row by row")
    rows: list[dict] = []
    for share in shares:
        chosen = rank_then_select(scores, share)
        hits = sum(1 for index in chosen if negatives[index])
        total_negative = sum(1 for flag in negatives if flag)
        rows.append(
            {
                "share": share,
                "reviewed": len(chosen),
                "caught": hits,
                "caught_share": (hits / total_negative) if total_negative else None,
                "extra": len(chosen) - hits,
            }
        )
    return rows
