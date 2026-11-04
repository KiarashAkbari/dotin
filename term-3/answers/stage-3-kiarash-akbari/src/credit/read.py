"""The only way the notebook reaches data: a SQLAlchemy query against the views.

The challenge forbids ``pd.read_csv`` in the analysis, and the reason is not
tidiness - a CSV read silently skips the contract, the quarantine and the sentinel
policy that the ingest applied. Everything here goes through the connection, so the
notebook sees exactly the rows that passed, with the sentinel codes already NULL.
"""

from __future__ import annotations

import pandas as pd
import sqlalchemy as sa

from .columns import (
    DB_INPUT_VIEW,
    DB_MISSINGNESS_VIEW,
    DB_STAGING_VIEW,
    DB_TABLE,
    SQL_IDENTIFIER,
)

#: the queries this module will run, by short name. A view name is an identifier and
#: cannot be a bind parameter, so it is checked against this set rather than quoted,
#: and the schema that qualifies it has to pass SQL_IDENTIFIER before it reaches the
#: text - the one place in this package where a config value touches SQL.
VIEWS: dict[str, str] = {
    "input": DB_INPUT_VIEW,
    "missingness": DB_MISSINGNESS_VIEW,
    "staging_input": DB_STAGING_VIEW,
    # the pre-view table: measurement only. the sentinel counts have to see the
    # codes the view turned into NULL, and nothing modelled is read from here
    "raw": DB_TABLE,
}


def resolve_view(name: str, schema: str = "credit") -> str:
    try:
        table = VIEWS[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown view {name!r}; expected one of {sorted(VIEWS)}"
        ) from exc
    if not SQL_IDENTIFIER.match(schema):
        raise ValueError(f"schema {schema!r} is not a lowercase SQL identifier")
    return f"{schema}.{table}"


def load_frame(
    engine: sa.Engine,
    view: str = "input",
    *,
    schema: str = "credit",
    chunk_size: int = 20_000,
) -> pd.DataFrame:
    """Read a view in chunks, building the frame without loading raw text.

    ``fetchmany`` keeps the client-side peak at one chunk of rows; the server still
    holds the result, which for 150,000 narrow numeric rows is the right trade -
    materialising it into a temp table would need a write, and the notebook is a
    reader.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    table = resolve_view(view, schema)
    frames: list[pd.DataFrame] = []
    with engine.connect() as connection:
        result = connection.execution_options(yield_per=chunk_size).execute(
            sa.text(f"SELECT * FROM {table} ORDER BY row_id")
        )
        columns = list(result.keys())
        rows = result.fetchmany(chunk_size)
        while rows:
            frames.append(pd.DataFrame.from_records(list(rows), columns=columns))
            rows = result.fetchmany(chunk_size)
    if not frames:
        return pd.DataFrame(columns=columns)
    # infer_objects is not decoration: a chunk that happens to hold only NULLs in a
    # column arrives as object, and concatenating it with float chunks would keep the
    # whole column as object - the model matrix would then differ by chunk_size alone
    return pd.concat(frames, ignore_index=True).infer_objects()


def row_count(engine: sa.Engine, view: str = "input", *, schema: str = "credit") -> int:
    table = resolve_view(view, schema)
    with engine.connect() as connection:
        return int(
            connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        )
