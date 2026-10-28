"""Build the Stage 2 notebook without executing database operations."""
from pathlib import Path
import ast
import textwrap

import nbformat as nbf


STAGE1 = Path(__file__).resolve().parent.parent
STAGE2 = STAGE1.parent / "stage-2-kiarash-akbari"
OUTPUT = STAGE2 / "solution.ipynb"

cells = []


def markdown(source: str) -> None:
    cells.append(nbf.v4.new_markdown_cell(textwrap.dedent(source).strip()))


def code(source: str) -> None:
    source = textwrap.dedent(source).strip()
    ast.parse(source)
    cells.append(nbf.v4.new_code_cell(source))


markdown("""
# مرحلهٔ دوم: بهینه‌سازی پایگاه‌داده Olist

## هدف و قرارداد اجرا

هدف، بررسی صحت مسیر اجرای پنج کوئری داشبورد و اندازه‌گیری تکرارپذیر
زمان آن‌ها در PostgreSQL است. بودجهٔ هر اجرای اندازه‌گیری‌شده ۲۰۰ میلی‌ثانیه است.

این نوتبوک داده یا زمان اجرا را جعل نمی‌کند. جدول‌ها از پایگاه‌داده و
فایل‌های شواهد خوانده می‌شوند و بنچمارک جدید با پکیج مرحلهٔ اول اجرا می‌شود.

### پیش‌نیازها

- پکیج مرحلهٔ اول در محیط kernel نصب شده باشد.
- PostgreSQL و داده‌های Olist آماده باشند.
- بازسازی اولیه با فرمان زیر، از پوشهٔ مرحلهٔ اول، انجام شده باشد:

```bash
python -m data_factory load-database --config config/default.toml --reset
```

این فرمان مخرب است و اسکیمای Olist را بازسازی می‌کند؛ بنابراین نوتبوک
آن را به‌صورت خودکار اجرا نمی‌کند. اجرای دوبارهٔ نوتبوک نباید داده‌ها را حذف کند.

فایل‌های SQL و منطق بنچمارک در پروژه باقی می‌مانند؛ نوتبوک وظیفهٔ روایت،
اجرای آزمایش و نمایش شواهد را دارد.
""")

code("""
from pathlib import Path
from datetime import datetime, timezone
from getpass import getpass
import json
import os
import random
import time

import pandas as pd
import sqlalchemy as sa
from IPython.display import display, Markdown

from data_factory import db_benchmark
from data_factory.config import load_config
from data_factory.database import build_database_url, OLIST_TABLES

SEED = 42
BUDGET_MS = 200
REPEATS = 15
WARMUPS = 3

random.seed(SEED)
notebook_started = time.perf_counter()

cwd = Path.cwd().resolve()
candidates = [
    cwd,
    cwd / "stage-2-kiarash-akbari",
    cwd.parent / "stage-2-kiarash-akbari",
]

STAGE2 = next(
    (
        path for path in candidates
        if path.name == "stage-2-kiarash-akbari"
        and (path / "sql" / "queries").is_dir()
    ),
    None,
)
if STAGE2 is None:
    raise RuntimeError(
        "Open the notebook from stage 2, stage 1, or their parent directory."
    )

STAGE1 = STAGE2.parent / "stage-1-kiarash-akbari"
CONFIG_PATH = STAGE1 / "config" / "default.toml"
QUERY_DIR = STAGE2 / "sql" / "queries"
CURATED_DIR = STAGE2 / "data" / "curated"
CURATED_DIR.mkdir(parents=True, exist_ok=True)

config = load_config(CONFIG_PATH)
if config.database is None:
    raise RuntimeError("The database configuration is missing.")

password_env = config.database.password_env
if not os.environ.get(password_env):
    password = getpass(f"PostgreSQL password ({password_env}): ")
    if not password:
        raise RuntimeError("A database password is required.")
    os.environ[password_env] = password
    del password

engine = sa.create_engine(
    build_database_url(config),
    connect_args={
        "options": "-c jit=off -c work_mem=32MB -c statement_timeout=120000"
    },
)

with engine.connect() as connection:
    environment = connection.execute(sa.text(
        "SELECT current_database() AS database, "
        "current_user AS role, "
        "current_setting('jit') AS jit, "
        "current_setting('work_mem') AS work_mem, "
        "version() AS postgres_version"
    )).mappings().one()

display(pd.DataFrame([dict(environment)]))
""")

markdown("""
## ۱. بررسی ساختار و داده‌های بارگذاری‌شده

در این بخش وجود هر نه جدول، تعداد ردیف‌های واقعی، viewها و
materialized viewها بررسی می‌شود. شمارش ردیف‌ها از پایگاه‌داده خوانده می‌شود،
نه از اعداد ثابت داخل گزارش.

همچنین فهرست SQLهای نسخه‌دار نمایش داده می‌شود تا مسیر بازسازی قابل بررسی باشد.
""")

code("""
with engine.connect() as connection:
    inspector = sa.inspect(connection)
    existing_tables = set(inspector.get_table_names(schema="olist"))
    missing_tables = set(OLIST_TABLES) - existing_tables
    if missing_tables:
        raise RuntimeError(
            f"Missing Olist tables: {sorted(missing_tables)}. "
            "Run the documented database rebuild first."
        )

    table_rows = []
    for table_name in OLIST_TABLES:
        table = sa.Table(
            table_name,
            sa.MetaData(),
            schema="olist",
            autoload_with=connection,
        )
        row_count = connection.scalar(
            sa.select(sa.func.count()).select_from(table)
        )
        table_rows.append({"table": table_name, "rows": row_count})

    views = pd.read_sql_query(sa.text(
        "SELECT schemaname, viewname "
        "FROM pg_views WHERE schemaname = :schema "
        "ORDER BY viewname"
    ), connection, params={"schema": "olist"})

    snapshots = pd.read_sql_query(sa.text(
        "SELECT schemaname, matviewname, ispopulated "
        "FROM pg_matviews WHERE schemaname = :schema "
        "ORDER BY matviewname"
    ), connection, params={"schema": "olist"})

display(pd.DataFrame(table_rows))
display(views)
display(snapshots)

if snapshots.empty or not snapshots["ispopulated"].all():
    raise RuntimeError("Materialized views are missing or not populated.")

migration_files = sorted(
    path.name for path in (STAGE2 / "sql").glob("*.sql")
)
display(pd.DataFrame({"versioned_sql": migration_files}))
""")

markdown("""
## ۲. شواهد تاریخی و قابلیت ردیابی گزارش

نتایج آزمایش‌های قبلی از فایل‌های JSON موجود در `data/curated/` خوانده می‌شوند.
فایل‌ها به‌عنوان شواهد تاریخی نمایش داده می‌شوند، نه جایگزین اجرای زندهٔ کوئری‌ها.

نبودن یک اندازه‌گیری به معنی موفق بودن آن نیست. برای ادعاهایی مانند صحت
خروجی، هزینهٔ درج و مقایسهٔ قبل و بعد، باید فایل شاهد مناسب وجود داشته باشد.
""")

code("""
artifact_paths = sorted(CURATED_DIR.glob("*.json"))
display(pd.DataFrame([
    {"artifact": path.name, "bytes": path.stat().st_size}
    for path in artifact_paths
]))

historical_rows = []
for path in artifact_paths:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        continue
    if not isinstance(document, dict):
        continue
    query_results = document.get("queries")
    if not isinstance(query_results, dict):
        continue
    for query_name, result in query_results.items():
        if not isinstance(result, dict) or "median_ms" not in result:
            continue
        historical_rows.append({
            "artifact": path.name,
            "query": query_name,
            "median_ms": result["median_ms"],
            "max_ms": result.get("max_ms"),
            "under_budget": result.get("all_measured_runs_under_budget"),
        })

historical_summary = pd.DataFrame(historical_rows)
display(historical_summary)

refresh_artifacts = [
    path for path in artifact_paths
    if path.name.startswith("refresh-q3-snapshot-")
]
for path in refresh_artifacts:
    display(Markdown(f"### شاهد refresh: `{path.name}`"))
    display(json.loads(path.read_text(encoding="utf-8")))
""")

markdown("""
## ۳. اجرای زنده و بنچمارک نهایی

هر کوئری سه warmup و پانزده اجرای اندازه‌گیری‌شده دارد.
این آزمایش warm-cache است و نباید به‌عنوان تضمین زمان در حالت cold-cache
یا تحت بار هم‌زمان تعبیر شود.

تنظیمات اتصال شامل `jit=off` و `work_mem=32MB` است.
زمان سمت کلاینت با زمان اجرای سرور یکسان نیست؛ هر دو باید جدا بررسی شوند.
""")

code("""
expected_queries = {
    "q1_monthly_category_sales.sql",
    "q2_delivery_by_state_month.sql",
    "q3_delay_and_satisfaction.sql",
    "q4_top_sellers.sql",
    "q5_payment_distribution.sql",
}
actual_queries = {path.name for path in QUERY_DIR.glob("*.sql")}
if actual_queries != expected_queries:
    raise RuntimeError(
        f"Unexpected query inventory: {sorted(actual_queries)}"
    )

print("Starting benchmark...", flush=True)
benchmark_report = db_benchmark.benchmark(
    engine, QUERY_DIR, repeats=REPEATS, warmups=WARMUPS
)

benchmark_summary = pd.DataFrame([
    {
        "query": filename,
        "median_ms": result["median_ms"],
        "max_ms": result["max_ms"],
        "all_runs_under_budget": result["all_measured_runs_under_budget"],
    }
    for filename, result in sorted(benchmark_report["queries"].items())
])

display(benchmark_summary.round(3))
all_queries_pass = bool(
    benchmark_summary["all_runs_under_budget"].all()
)
print(f"All measured runs below {BUDGET_MS} ms: {all_queries_pass}")
""")

markdown("""
## ۴. برنامه‌های اجرای نهایی

برنامه‌های JSON تولیدشده توسط بنچمارک نمایش داده می‌شوند تا مسیر scan،
join و aggregation قابل بررسی باشد. وجود یک پلن نهایی به‌تنهایی ثابت نمی‌کند
که بهینه‌سازی مؤثر بوده است؛ برای هر کوئری باید آن را با پلن اولیهٔ ذخیره‌شده
مقایسه کرد.

در این بخش فقط دادهٔ موجود نمایش داده می‌شود و کلیدهای ناموجود جعل نمی‌شوند.
""")

code("""
for query_name, result in sorted(benchmark_report["queries"].items()):
    display(Markdown(f"### `{query_name}`"))
    plan_fields = {
        key: value
        for key, value in result.items()
        if "plan" in key.lower() or "explain" in key.lower()
    }
    if plan_fields:
        display(plan_fields)
    else:
        print("No plan field found. Available result keys:", sorted(result))
""")

markdown("""
## ۵. فضای ایندکس‌ها

ایندکس هزینهٔ ذخیره‌سازی و نگهداری دارد. این جدول اندازهٔ واقعی ایندکس‌ها
و نسبت اندازهٔ آن‌ها به جدول را نشان می‌دهد.

این اندازه‌گیری جایگزین آزمون قبل/بعد سرعت درج نیست؛ هزینهٔ نوشتن باید در
یک آزمایش مستقل و کنترل‌شده اندازه‌گیری و به فایل شاهد مرتبط ارجاع داده شود.
""")

code("""
with engine.connect() as connection:
    index_sizes = pd.read_sql_query(sa.text(
        "SELECT t.relname AS table_name, i.relname AS index_name, "
        "pg_relation_size(i.oid) AS index_bytes, "
        "pg_relation_size(t.oid) AS table_bytes, "
        "ROUND(pg_relation_size(i.oid)::numeric / "
        "NULLIF(pg_relation_size(t.oid), 0), 4) AS index_to_table_ratio "
        "FROM pg_index x "
        "JOIN pg_class t ON t.oid = x.indrelid "
        "JOIN pg_class i ON i.oid = x.indexrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "WHERE n.nspname = :schema "
        "ORDER BY t.relname, i.relname"
    ), connection, params={"schema": "olist"})
display(index_sizes)
""")

markdown("""
## ۶. محدودیت‌ها و دامنهٔ اعتبار

- عبور از بودجه در این اجرا، تضمین عملکرد روی هر سخت‌افزار یا تحت هر بار نیست.
- دادهٔ materialized view تا refresh بعدی ممکن است قدیمی باشد.
- seed ثابت، زمان اجرای پایگاه‌داده را قطعی نمی‌کند؛ نتایج معنایی باید ثابت
  بمانند اما زمان‌ها با بار سیستم و وضعیت cache تغییر می‌کنند.
- بنچمارک موفق به‌تنهایی صحت معنایی کوئری را ثابت نمی‌کند.
- این نوتبوک هنوز جایگزین شواهد لازم برای Procedure، Function،
  idempotency، هزینهٔ درج و مقایسهٔ view با snapshot نیست.
  هر بخش باید با آزمون یا artifact واقعی پروژه تکمیل شود.
""")

markdown("""
## ۷. ذخیرهٔ نتیجه و جمع‌بندی

فایل خروجی پیش از کنترل نهایی بودجه ذخیره می‌شود؛ بنابراین اگر کوئری‌ای
از بودجه عبور کند، شاهد شکست نیز باقی می‌ماند.

اعداد گزارش باید به فایل مشخص و خروجی اجراشدهٔ همین نوتبوک ارجاع دهند؛
نباید اعداد قدیمی با اجرای جدید بدون توضیح جایگزین شوند.
""")

code("""
benchmark_report["experiment"] = {
    "name": "Stage 2 notebook live benchmark",
    "query_directory": "sql/queries",
    "repeats": REPEATS,
    "warmups": WARMUPS,
    "budget_ms": BUDGET_MS,
    "cache_protocol": "warmups; cache not flushed",
    "connection_settings": {
        "jit": "off",
        "work_mem": "32MB",
        "statement_timeout": "120s",
    },
    "seed": SEED,
    "measured_at": datetime.now(timezone.utc).isoformat(),
}

timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
output_path = CURATED_DIR / f"benchmark-final-{timestamp}.json"
with output_path.open("x", encoding="utf-8") as handle:
    json.dump(benchmark_report, handle, ensure_ascii=False, indent=2)
    handle.write("\\n")

print("Saved:", output_path.relative_to(STAGE2))
print("Notebook elapsed seconds:", round(time.perf_counter() - notebook_started, 2))
engine.dispose()

assert all_queries_pass, (
    "At least one measured run exceeded the budget; "
    "inspect the saved artifact instead of discarding this run."
)
""")

markdown("""
## AI usage

از دستیار هوش مصنوعی برای تهیه و اصلاح سازندهٔ نوتبوک، تشخیص خطای مسیرها،
هماهنگ‌کردن کلیدهای خروجی بنچمارک و تنظیم روایت کمک گرفته شد.
بخش‌هایی از کد نیز توسط دستیار پیشنهاد شد.

اجرای واقعی، بازبینی خروجی‌ها و تأیید ادعاهای گزارش باید توسط صاحب پروژه
انجام شود. تولید فایل نوتبوک به معنی اجرا یا تأیید کامل الزامات مرحله نیست.
""")

nb = nbf.v4.new_notebook(
    cells=cells,
    metadata={
        "kernelspec": {
            "display_name": "Python 3 (data-factory)",
            "language": "python",
            "name": "data-factory",
        },
        "language_info": {"name": "python"},
    },
)

nbf.validate(nb)
STAGE2.mkdir(parents=True, exist_ok=True)

if OUTPUT.exists():
    backup = OUTPUT.with_name("solution.before-fix.ipynb")
    if not backup.exists():
        backup.write_bytes(OUTPUT.read_bytes())

nbf.write(nb, OUTPUT)
print(f"Created: {OUTPUT}")
print(f"Cells: {len(cells)}")
print("Notebook structure and Python syntax validated; database cells not executed.")