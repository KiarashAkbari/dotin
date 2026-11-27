"""Derivation witness for the two thresholds of 0044 (estimate drift 1.0 %, cap binding >=).

Run on a *throwaway copy* of a chain-built database -- it deletes planner statistics (superuser only):

    CAPSTONE_DATABASE_URI=<uri of the copy> python docs/evidence/ceiling-threshold-derivation.py \
        analyzed no-statistics analyzed never-analyzed analyzed cap=0 cap=1 cap=2 cap=4 cap=8

Each argument is a state, applied in order, and the frame's top plan node is read after it: its row
estimate against the rows it returned (what `estimate_drift()` judges against 1.0 %), and
`Workers Planned` (what `cap_binding()` judges against the cap with `>=`). Prints no URI.
"""
import os, re, sys, psycopg
uri = os.environ["CAPSTONE_DATABASE_URI"]
FRAME = "capstone.v_feature_frame"
INPUTS_SQL = """
SELECT DISTINCT n.nspname || '.' || c.relname
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','m') AND n.nspname IN ('olist','capstone')
ORDER BY 1"""
def plan(cur):
    cur.execute(f"EXPLAIN (ANALYZE, TIMING OFF) SELECT * FROM {FRAME}")
    return "\n".join(r[0] for r in cur.fetchall())
def top(p):
    first = p.splitlines()[0]
    est = re.search(r"rows=(\d+)", first); act = re.search(r"actual rows=(\d+)", first)
    e, a = int(est.group(1)), int(act.group(1))
    wp = re.findall(r"Workers Planned: (\d+)", p)
    return first.split("(cost")[0].strip(), e, a, abs(e - a) / a * 100, wp
out = []
with psycopg.connect(uri, autocommit=True) as conn:
    cur = conn.cursor()
    cur.execute("SELECT current_database(), version()")
    db, ver = cur.fetchone()
    cur.execute("SHOW max_parallel_workers_per_gather"); cap = cur.fetchone()[0]
    out.append(f"database {db}; {ver.split(',')[0]}; max_parallel_workers_per_gather={cap}")
    cur.execute(INPUTS_SQL); rels = [r[0] for r in cur.fetchall()]
    for label in sys.argv[1:]:
        if label == "analyzed":
            for r in rels: cur.execute(f"ANALYZE {r}")
        elif label == "no-statistics":
            # the state a frame is in when its inputs were (re)built and nothing ANALYZEd them
            cur.execute("DELETE FROM pg_statistic WHERE starelid = ANY(%s::regclass[])", (rels,))
            cur.execute("SELECT count(*) FROM pg_statistic WHERE starelid = ANY(%s::regclass[])", (rels,))
            assert cur.fetchone()[0] == 0
        elif label == "never-analyzed":
            # as above, and the table sizes forgotten too: what a relation looks like before its first
            # ANALYZE or VACUUM (pg_class.reltuples = -1)
            cur.execute("DELETE FROM pg_statistic WHERE starelid = ANY(%s::regclass[])", (rels,))
            cur.execute(
                "UPDATE pg_class SET reltuples = -1, relpages = 0, relallvisible = 0 "
                "WHERE oid = ANY(%s::regclass[])", (rels,)
            )
        elif label.startswith("cap="):
            cur.execute(f"SET max_parallel_workers_per_gather = {int(label[4:])}")
        p = plan(cur)
        node, e, a, pct, wp = top(p)
        out.append(f"state {label:14s} top node {node!r}: estimated rows {e}, actual {a}, off {pct:.3f} %; Workers Planned {wp or '-'}")
print("\n".join(out))
