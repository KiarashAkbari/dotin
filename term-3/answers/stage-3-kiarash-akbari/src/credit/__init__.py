"""Cost-sensitive default prediction on the stage-2 database.

The dataset arrives through the stage-1 factory (see ``credit.source``) and is
read back from PostgreSQL through the stage-2 connection settings; nothing in
this package opens a CSV from a notebook. The pieces are deliberately small and
independent, because the graded part of stage 3 is the reasoning around them:

``columns``     the header contract and the CSV-name to database-name mapping
``source``      a file source that satisfies stage 1's ``PageSource`` protocol
``validate``    record-level reasons, so nothing is dropped silently
``cost``        confusion counts, the money, and the threshold the cost curve picks
``features``    engineered columns that carry their decision-time audit with them
``missingness`` whether a missing value means something, measured per column
"""

__all__ = [
    "columns",
    "cost",
    "features",
    "missingness",
    "source",
    "validate",
]
