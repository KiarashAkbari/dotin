# Rejected: Q5 two-stage hash aggregation

This rewrite removed the 102k-row sort and preserved exact results, improving
the Q5 median from about 543 ms to 479 ms. It was still above the 200 ms
budget because the per-order totals CTE was scanned and aggregated repeatedly.

Evidence: `data/curated/benchmark-q5-two-stage-20261028-220038.json`.
