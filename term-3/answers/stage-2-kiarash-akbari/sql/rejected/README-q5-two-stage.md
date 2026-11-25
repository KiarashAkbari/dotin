# Rejected: Q5 two-stage hash aggregation

This rewrite removed the 102k-row sort and preserved exact results, improving
the Q5 median from about 543 ms to 479 ms. It was still above the 200 ms
budget because the per-order totals CTE was scanned and aggregated repeatedly.

Evidence: `data/curated/benchmark-q5-two-stage-20261028-220038.json`.

> Note (2026-11): the evidence file named above was never committed, so this number cannot be checked from the repository. The query this experiment tried to fix has since been rewritten; current before/after evidence is in `data/curated/evidence/before-after.json`.
