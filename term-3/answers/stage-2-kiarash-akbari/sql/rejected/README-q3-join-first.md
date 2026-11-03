# Rejected: Q3 join-first review aggregation

This rewrite joined reviews to delivered orders before per-order aggregation
and removed a redundant numeric cast. It preserved exact results and improved
the Q3 median from 201.18 ms to 197.70 ms, but the maximum remained 216.17 ms.
It was therefore not retained because every measured run must stay below 200 ms.

Evidence: `data/curated/benchmark-q3-join-first-20261028-221325.json`.
