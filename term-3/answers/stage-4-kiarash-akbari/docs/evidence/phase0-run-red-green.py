"""Red/green harness for stage-1/tests/test_load_database_snapshots.py.

Boots pgserver (a real PostgreSQL 16.2, unix socket only), creates an empty
disposable database, then (1) runs the new test module, (2) runs the whole stage-1
suite, and (3) replays the phase-0 scenario: two consecutive
`load-database --reset --skip-factory-sink` runs with the view state printed after
each one.

usage: python run_pg_tests.py red|green [suite-only]
"""
import hashlib
import io
import os
import pathlib
import subprocess
import sys
import contextlib

import sqlalchemy as sa

MODE = sys.argv[1]
assert MODE in ("red", "green")
STAGE1 = pathlib.Path("/home/user/data-factory-v2/stage-1")
REPO = STAGE1.parent
CLI_TARGET = STAGE1 / "src/data_factory/cli.py"
src = pathlib.Path("/tmp/cli_head.py" if MODE == "red" else "/tmp/cli_fixed.py")
CLI_TARGET.write_bytes(src.read_bytes())

import pgserver  # noqa: E402

srv = pgserver.get_server(pathlib.Path("/tmp/pgdata_v4"), cleanup_mode=None)
admin_url = str(sa.make_url(srv.get_uri()).set(drivername="postgresql+psycopg2"))
db = f"phase0_{MODE}"
url = f"postgresql+psycopg2://postgres@/{db}?host=/tmp/pgdata_v4"
admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
with admin.connect() as c:
    c.execute(sa.text(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)"))
    c.execute(sa.text(f"CREATE DATABASE {db}"))
admin.dispose()

with sa.create_engine(url).connect() as c:
    print("server       :", c.execute(sa.text("show server_version")).scalar(),
          "| listen_addresses =", c.execute(sa.text("show listen_addresses")).scalar(),
          "| unix socket only")
print(f"database     : {db} (created empty, dropped and recreated by this script)")
print(f"cli.py       : {'stage-1 HEAD, pre-fix' if MODE == 'red' else 'working tree, with the fix'}"
      f" sha256={hashlib.sha256(CLI_TARGET.read_bytes()).hexdigest()[:12]}")
print(f"config       : url={url}  (driver postgresql+psycopg2: the pgserver build returns "
      "bytes to psycopg3, so SQLAlchemy cannot parse its version)")
sql_hash = hashlib.sha256(b"".join(sorted(
    p.read_bytes() for p in (REPO / "stage-2/sql").glob("*.sql")))).hexdigest()[:12]
print(f"stage-2/sql  : sha256={sql_hash} over {len(list((REPO/'stage-2/sql').glob('*.sql')))} files")
print(f"csv fixtures : /tmp/fixtures/olist_csv, one row per table, header from the loader predicate")

# PGHOST only because pgserver's postmaster listens on a unix socket in this
# sandbox; SQLAlchemy 2.1 does not pass a `?host=` query parameter on
# postgresql+psycopg2 URLs to libpq. With a normal TCP URL nothing is needed.
env = dict(os.environ, PYTHONPATH="src:tests", TEST_DATABASE_URL=url,
           PGHOST="/tmp/pgdata_v4", SCENARIO_ROOT=f"/tmp/scenario_{MODE}")


def run(args, label):
    print(f"\n########## {label} ##########", flush=True)
    proc = subprocess.run([sys.executable, *args], cwd=str(STAGE1), env=env,
                          capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    io.StringIO()
    print(out, end="", flush=True)
    print(f"[exit {proc.returncode}] {label}", flush=True)
    return proc.returncode


code = run(["-m", "unittest", "-v", "tests.test_load_database_snapshots"],
           "unittest tests.test_load_database_snapshots")
run(["-m", "unittest", "discover", "-s", "tests"], "full stage-1 suite")

SCENARIO = r"""
import contextlib, dataclasses, io, json, os, pathlib, sys
from datetime import date
from urllib.parse import urlsplit
sys.path[:0] = ["src"]
import sqlalchemy as sa
from data_factory.cli import _load_database
from data_factory.config import parse_config

VIEWS = ("mv_q1_monthly_category_sales", "mv_q3_delay_satisfaction",
         "mv_q4_seller_dashboard", "mv_q5_payment_distribution")
root = pathlib.Path(os.environ["SCENARIO_ROOT"])
root.mkdir(parents=True, exist_ok=True)
sql_dir = root / "sql"
sql_dir.mkdir(exist_ok=True)
for p in sorted(pathlib.Path("../stage-2/sql").glob("*.sql")):
    (sql_dir / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")

raw = {
 "source": {"url": "https://example.test/x.json", "agency": "NYPD",
   "complaint_types": ["Noise - Residential"], "columns": ["unique_key", "created_date"],
   "start_date": date(2025, 1, 1), "end_date": date(2025, 6, 30), "page_size": 1000,
   "app_token_env": "TOKEN", "timezone": "America/New_York"},
 "http": {"timeout_seconds": 10, "max_retries": 1, "backoff_base_seconds": 1.0,
   "backoff_factor": 2.0, "retry_statuses": [503]},
 "paths": {"raw_dir": str(root/"raw"), "curated_dir": str(root/"curated"),
   "quarantine_dir": str(root/"q"), "manifest_path": str(root/"m.json")},
 "quality": {"max_quarantine_ratio": 0.5, "min_rows_for_threshold": 1},
 "database": {"url": os.environ["TEST_DATABASE_URL"], "password_env": "ABSENT_FOR_TEST",
   "olist_csv_dir": "/tmp/fixtures/olist_csv", "stage1_curated_dir": str(root/"curated"),
   "batch_size": 100, "sql_migrations_dir": str(sql_dir)},
}
cfg = parse_config(raw)

def state(engine):
    out = {}
    with engine.connect() as c:
        for v in VIEWS:
            pop = c.scalar(sa.text("SELECT ispopulated FROM pg_matviews"
                                   " WHERE schemaname='olist' AND matviewname=:n"), {"n": v})
            try:
                rows = c.scalar(sa.text(f"SELECT count(*) FROM olist.{v}"))
            except Exception:
                c.rollback()
                rows = "error"
            out[v] = {"ispopulated": pop, "rows": rows}
    return out


def counts(engine):
    out = {}
    with engine.connect() as c:
        for t in ("product_category_name_translation", "customers", "sellers", "products",
                  "geolocation", "orders", "order_items", "payments", "order_reviews"):
            out[t] = c.scalar(sa.text(f"SELECT count(*) FROM olist.{t}"))
    return out


engine = sa.create_engine(os.environ["TEST_DATABASE_URL"])
print("tables before any run:", json.dumps(
    {"olist schema exists": engine.connect().execute(sa.text(
        "SELECT count(*) FROM pg_namespace WHERE nspname='olist'")).scalar()}, ))
for run_no in (1, 2):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _load_database(cfg, reset=True, with_sink=False)
    print(f"\n--- run {run_no}: load-database --reset --skip-factory-sink stdout ---")
    print(buf.getvalue().strip())
    print(f"--- run {run_no}: table counts ---")
    print(json.dumps(counts(engine), indent=1, sort_keys=True))
    print(f"--- run {run_no}: materialized view state ---")
    print(json.dumps(state(engine), indent=1, sort_keys=True))
engine.dispose()
"""
run(["-c", SCENARIO], "phase-0 scenario: two consecutive --reset runs")
