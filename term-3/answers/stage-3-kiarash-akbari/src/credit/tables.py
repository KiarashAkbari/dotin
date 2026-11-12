"""Tables for notebook and report output, rendered without an optional dependency.

pandas prints a pipe table itself, but only through ``DataFrame.to_markdown``, which
imports ``tabulate`` - a package the delivery guide's allowed stack does not name and
this project's lock does not carry. The first run of the notebook on a fresh venv died
there, three cells in. These tables are a dozen rows wide and plain, so the rendering
is worth fewer moving parts: a notebook that runs anywhere beats one with aligned
columns.
"""

from __future__ import annotations

import pandas as pd

#: what an empty cell says, so a missing value is never an empty string that reads as zero
MISSING = "NaN"


def _cell(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return MISSING
    if pd.isna(value):
        return MISSING
    return str(value)


def md_table(data: object, *, index: bool = False) -> str:
    """Render a DataFrame or Series as a GitHub pipe table."""
    frame = data.to_frame() if isinstance(data, pd.Series) else pd.DataFrame(data)
    if not index:
        frame = frame.reset_index(drop=True)
    headers = [str(column) for column in frame.columns]
    if index:
        headers = [str(frame.index.name or "") or "index", *headers]
    rows = []
    for position, record in enumerate(frame.itertuples(index=False, name=None)):
        cells = [_cell(value) for value in record]
        if index:
            cells.insert(0, str(frame.index[position]))
        rows.append(cells)
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(" --- " for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)
