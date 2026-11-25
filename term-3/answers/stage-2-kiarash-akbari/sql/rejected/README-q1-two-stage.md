# Rejected: Q1 two-stage order-grain aggregation

This rewrite removed the 112k-row sort and preserved exact results, improving
the Q1 median from about 352-378 ms to 273.69 ms. It remained above the
200 ms budget because it still aggregated 112,101 item rows and 99,002
order-grain rows on every dashboard read.

Evidence: `data/curated/benchmark-q1-two-stage-20261028-220655.json`.

> Note (2026-11): the evidence file named above was never committed, so this number cannot be checked from the repository. The query this experiment tried to fix has since been rewritten; current before/after evidence is in `data/curated/evidence/before-after.json`.
