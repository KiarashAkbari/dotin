# Rejected: Q4 single-pass review averaging

The rewrite removed the second `seller_orders` scan but joined all 99,224
review rows before aggregation. It changed Q4 from a 259.11 ms median to a
291.30 ms median, so it was not retained.

Evidence: `data/curated/benchmark-q4-single-pass-20261028-214136.json`.
