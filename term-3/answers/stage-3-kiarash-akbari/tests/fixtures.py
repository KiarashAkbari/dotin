"""Synthetic credit-shaped data for the offline suite.

The Kaggle file is not in Git and cannot be downloaded by a test, so the suite
runs against a generated frame with the same header, the same kinds of defects
(codes in count columns, blanks, utilisation above 1) and a fixed seed. Real
numbers about the real file belong to the notebook and the database, not here.
"""

from __future__ import annotations

import csv
import math
import random
from pathlib import Path

from credit.columns import CSV_COLUMNS, DB_COLUMNS

#: rows generated per test call; small on purpose, the suite must stay instant
DEFAULT_ROWS = 400


def write_csv(path: Path, *, rows: int = DEFAULT_ROWS, seed: int = 20261103) -> Path:
    """Write a file that *fails* nothing but looks messy, to exercise the loader.

    Defects are placed deliberately and countably: the tests assert exact
    numbers, so a change in the generator shows up as a failing assertion rather
    than a silently different baseline.
    """
    rng = random.Random(seed)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(list(CSV_COLUMNS))
        for index in range(rows):
            target = 1 if index % 13 == 0 else 0
            late_30 = str(rng.choice([0, 0, 1, 2, 96, 98]))
            late_60 = str(rng.choice([0, 0, 0, 1, 96]))
            late_90 = str(rng.choice([0, 0, 0, 0, 2, 98]))
            income = "" if index % 7 == 0 else str(rng.randrange(0, 20_001))
            age = "" if index % 29 == 0 else str(rng.randrange(21, 80))
            dependents = "" if index % 9 == 0 else str(rng.randrange(0, 5))
            # one value above 1 every tenth row, the documented outlier shape
            utilization = f"{rng.uniform(0.0, 1.6):.6f}" if index % 10 == 0 else "0.500000"
            writer.writerow(
                [
                    index + 1,
                    target,
                    utilization,
                    age,
                    late_30,
                    f"{rng.uniform(0.0, 3.5):.6f}",
                    income,
                    str(rng.randrange(0, 30)),
                    late_90,
                    str(rng.randrange(0, 5)),
                    late_60,
                    dependents,
                ]
            )
    return path


def row(**values: object) -> dict[str, object]:
    """One raw record: every contract column present, blanks where not given."""
    record = {name: None for name in CSV_COLUMNS}
    for name, value in values.items():
        if name not in record:
            raise KeyError(f"{name!r} is not part of the credit contract")
        record[name] = value
    return record


def frame(rows: list[dict[str, object]]) -> object:
    """A DataFrame of database-named columns, from ``{db_name: value}`` dicts.

    Kept lazy so the module imports without pandas; the tests all need it, none
    of them needs it at import time.
    """
    import pandas as pd

    filled = [{column: math.nan for column in DB_COLUMNS} | dict(item) for item in rows]
    return pd.DataFrame(filled, columns=list(DB_COLUMNS))
