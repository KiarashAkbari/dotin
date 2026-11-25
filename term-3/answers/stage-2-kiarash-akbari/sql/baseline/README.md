# Baseline queries (commit 534c6ac)

The first live versions of the five dashboard queries, kept unchanged so the
notebook can time and EXPLAIN them next to the optimized files in
`sql/queries/`. Each optimized query must return the same rows (same SHA-256
after rounding floats to 12 significant digits); the notebook checks it.
