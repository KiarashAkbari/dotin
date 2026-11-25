# Rejected: Q4 single-pass review averaging

The rewrite removed the second `seller_orders` scan but joined all 99,224
review rows before aggregation. It changed Q4 from a 259.11 ms median to a
291.30 ms median, so it was not retained.

Evidence: `data/curated/benchmark-q4-single-pass-20261028-214136.json`.

> Note (2026-11): the evidence file named above was never committed, so this number cannot be checked from the repository. The query this experiment tried to fix has since been rewritten; current before/after evidence is in `data/curated/evidence/before-after.json`.
