"""Build solution.ipynb (cells only; run it with Restart & Run All or run_notebook.py)."""
from pathlib import Path

import nbformat as nbf

STAGE2 = Path(__file__).resolve().parents[1]
cells = []


def md(text):
    cells.append(nbf.v4.new_markdown_cell(text.strip()))


def code(text):
    cells.append(nbf.v4.new_code_cell(text.strip()))


md("""
# مرحلهٔ دوم: پشت میز DBA (Olist روی PostgreSQL)

این نوت‌بوک برای هر پنج سؤال تیم محصول، نسخهٔ اولیهٔ کوئری را کنار نسخهٔ بهینه اجرا می‌کند، پلن اجرای هر دو را نشان می‌دهد و هر ادعای گزارش را با یک فایل شاهد در `data/curated/evidence/` پشتیبانی می‌کند. منطق اندازه‌گیری در پکیج `src/olist_perf` است و نوت‌بوک فقط آن را صدا می‌زند و روایت می‌کند.

### قرارداد اجرا

1. پایگاه‌داده با **یک فرمان** از PostgreSQL خالی ساخته و پر می‌شود (از پوشهٔ مرحلهٔ اول):
   `python -m data_factory load-database --config config/default.toml --reset --skip-factory-sink`
   این فرمان مخرب است (schema `olist` را از نو می‌سازد)، پس نوت‌بوک آن را اجرا نمی‌کند. زمان این بارگذاری جزو سقف ۱۰ دقیقه نیست.
2. رمز پایگاه‌داده فقط از متغیر محیطی `OLIST_DB_PASSWORD` خوانده می‌شود. اگر تنظیم نشده باشد نوت‌بوک بلافاصله با پیام روشن متوقف می‌شود و چیزی نمی‌پرسد؛ پس Restart & Run All بدون دخالت دستی کار می‌کند.
3. اگر پایگاه‌داده یا نام کاربر شما با `../stage-1-kiarash-akbari/config/default.toml` فرق دارد، مسیر یک فایل تنظیمات محلی را در `OLIST_CONFIG` بگذارید.
4. همهٔ آزمایش‌هایی که ساختار را تغییر می‌دهند (حذف موقت Index، ساخت Index ردشده، درج انبوه) داخل تراکنشی اجرا می‌شوند که در پایان rollback می‌شود؛ پایگاه‌داده بعد از نوت‌بوک همان است که قبلش بود.
""")

code("""
SEED = 42  # one seed for the whole notebook (nothing here samples, kept per the delivery guide)
import random
random.seed(SEED)

import json
import time
from pathlib import Path

import pandas as pd
import sqlalchemy as sa
from IPython.display import Markdown, display

from olist_perf import bench, bridge, db, evidence

pd.set_option("display.max_columns", 30)
pd.set_option("display.width", 200)
started = time.perf_counter()

STAGE2 = Path.cwd()
if not (STAGE2 / "sql" / "queries").is_dir():
    raise RuntimeError("Run the notebook from the stage-2-kiarash-akbari folder.")
REPEATS, WARMUPS = 15, 3

engine = db.make_engine()          # password from the environment only; fails fast
print("database:", engine.url.database, "| session settings:", db.SESSION_SETTINGS)
""")

md("""
## ۱. وضعیت پایگاه‌داده

شمارش ردیف‌ها، viewها و فایل‌های SQL اعمال‌شده از خود پایگاه‌داده خوانده می‌شوند، نه از اعداد ثابت. فهرست migrationها نشان می‌دهد ساختار از فایل‌های نسخه‌دار `sql/*.sql` ساخته شده است (`004_performance_indexes.sql` همراه با 001 تا 003 و پیش از بارگذاری داده اعمال می‌شود).
""")

code("""
with engine.connect() as connection:
    counts = pd.DataFrame([
        {"table": name, "rows": connection.scalar(sa.select(sa.func.count()).select_from(
            sa.table(name, schema="olist")))}
        for name in bridge.CSV
    ])
    views = pd.read_sql_query(sa.text(
        "SELECT viewname AS view FROM pg_views WHERE schemaname = :s "
        "UNION ALL SELECT matviewname || ' (materialized)' FROM pg_matviews "
        "WHERE schemaname = :s ORDER BY 1"), connection, params={"s": "olist"})
    applied = pd.read_sql_query(sa.text(
        "SELECT migration_name, applied_at FROM public.data_factory_migrations "
        "ORDER BY migration_name"), connection)
display(counts)
display(views)
display(applied)
""")

md("""
## ۲. یکپارچگی ارجاعی دادهٔ خام (بخش ۱ صورت مسئله)

برای هر ارجاع، کلیدهای فرزند در CSV خام با کلیدهای والد مقایسه می‌شوند (فقط ستون‌های کلید خوانده می‌شوند). سه ارجاع آخر در schema کلید خارجی ندارند و دلیلش در `sql/001_create_olist_schema.sql` و `docs/schema_decisions.md` آمده است: نام دستهٔ بدون ترجمه و پیشوند کدپستی بدون مختصات، دادهٔ واقعی‌اند و حذف آن‌ها فروش واقعی را پاک می‌کرد. جدول دوم تعداد رکورد خام و بارگذاری‌شدهٔ هر جدول را مقایسه می‌کند؛ تنها اختلاف، ردیف‌های کاملاً تکراری geolocation است که loader پیش از درج حذف می‌کند.
""")

code("""
fk = bridge.fk_audit(engine, bridge.csv_dir_from_config())
display(pd.DataFrame(fk["references"]))
display(pd.DataFrame(fk["row_counts"]))
evidence.save("fk-audit.json", fk);
""")

md("""
## ۳. نگهداری پس از بارگذاری و ساخت snapshot ماه‌های بسته

**VACUUM (ANALYZE):** بارگذاری انبوه، visibility map و آمار planner را خالی می‌گذارد تا autovacuum سراغ جدول‌ها بیاید. بدون آن، Index Only Scanها باید برای هر سطر به heap سر بزنند و planner با تخمین پیش‌فرض کار می‌کند؛ در اجرای آزمایشی این نوت‌بوک که بلافاصله بعد از بارگذاری و پیش از autovacuum شروع شد، Q2 بیش از سه برابر کندتر بود (`data/curated/evidence/benchmark-final-unvacuumed-run.json`). این سلول همان کار را صریح انجام می‌دهد تا اندازه‌گیری به زمان‌بندی autovacuum وابسته نباشد.

**snapshot:**
`olist.refresh_monthly_category_snapshot_range` (در 004) برای هر ماه، procedure یک‌ماههٔ 003 را صدا می‌زند؛ همه در یک تراکنش، پس خطا در هر ماه هیچ ماهی را نیمه‌کاره نمی‌گذارد. «ماه بسته» یعنی هر ماه پیش از ماهِ آخرین خرید. این همان کاری است که در عمل پس از هر بارگذاری یا هر شب زمان‌بندی می‌شود؛ Q1 ماه‌های بسته را از این جدول و ماه‌های بعد از آن را زنده حساب می‌کند.
""")

code("""
snapshot_state = evidence.refresh_closed_months(engine)
display(pd.DataFrame(db.vacuum_analyze(engine)).set_index("table").T)
display(pd.Series(snapshot_state))
""")

md("""
## ۴. چرخهٔ بهینه‌سازی: پلن و زمان قبل و بعد برای هر پنج کوئری

**روش اندازه‌گیری (برای همهٔ کوئری‌ها یکسان):** یک session با `jit=off`، `work_mem=32MB` و `max_parallel_workers_per_gather=0` (ماشین اندازه‌گیری دو هستهٔ مشترک دارد و workerهای موازی زمان را به بار بقیه وابسته می‌کردند). سه اجرای گرم‌کردن، سپس پانزده اجرای اندازه‌گیری‌شده؛ زمان سمت کلاینت از ارسال تا دریافت آخرین ردیف است. حالت warm-cache است و تضمینی برای cold-cache یا بار هم‌زمان نیست. پلن با `EXPLAIN (ANALYZE, BUFFERS)` جدا گرفته می‌شود (زمان آن به‌خاطر سربار اندازه‌گیری هر گره کمی بیشتر است).

**قبل** = نسخهٔ اولیهٔ کوئری (`sql/baseline/`، کامیت 534c6ac) روی پایگاه‌داده‌ای که Indexهای 004 در یک تراکنش rollback‌شونده از آن حذف شده‌اند. **بعد** = `sql/queries/` با Indexها. خروجی دو نسخه باید ردیف‌به‌ردیف یکی باشد (SHA-256 پس از گرد کردن اعداد اعشاری شناور به ۱۲ رقم معنادار).
""")

code("""
before_after = evidence.before_after(engine, STAGE2, repeats=REPEATS, warmups=WARMUPS)
summary = pd.DataFrame([
    {"query": name,
     "before_median_ms": r["before"]["median_ms"], "before_max_ms": r["before"]["max_ms"],
     "after_median_ms": r["after"]["median_ms"], "after_max_ms": r["after"]["max_ms"],
     "speedup": r["speedup"], "same_result": r["same_result"],
     "before_seq_scans": ", ".join(r["before"]["scans"]["seq_scan"]),
     "after_index_scans": ", ".join(r["after"]["scans"]["index_scan"])}
    for name, r in before_after.items()])
display(summary)
evidence.save("before-after.json", before_after)
assert summary["same_result"].all(), "an optimized query changed the answer"
""")

code("""
def show_plan(name):
    for side in ("before", "after"):
        nodes = pd.DataFrame(before_after[name][side]["plan_nodes"])
        nodes["node"] = ["  " * d + n for d, n in zip(nodes["depth"], nodes["node"])]
        display(Markdown(f"**{name} — {side}** (server execution "
                         f"{before_after[name][side]['server_execution_ms']:.1f} ms)"))
        display(nodes.drop(columns=["depth"]))
""")

QUERY_NOTES = {
    "q1_monthly_category_sales.sql": """
### Q1 — سبد و درآمد ماهانه به تفکیک دسته

**خواندن پلن قبل:** سه Seq Scan (order_items، orders، products) و دو Hash Join، بعد یک Sort روی همهٔ ~۱۱۲ هزار سطر آیتم برای `COUNT(DISTINCT order_id)` در GroupAggregate. گران‌ترین گره همین Sort و تجمیع است و هیچ فیلتری نیست که Index بتواند سطرها را کم کند: کوئری ذاتاً همهٔ تاریخ را می‌خواند. (این کوئری مورد بخش ۵ است؛ بخش ۸ را ببینید.)

**تغییر:** ماه‌های بسته از `olist.monthly_category_snapshot` خوانده می‌شوند و فقط ماه‌های بعد از آخرین ماه snapshot زنده تجمیع می‌شوند (`ix_orders_purchase_ts` بازهٔ زمانی را با Bitmap Index Scan می‌خواند).

**چرا کار کرد:** کاری که در هر باز شدن داشبورد تکرار می‌شد (تجمیع دو سال) یک بار در procedure انجام می‌شود؛ پلن بعد فقط حدود ۱۳۰۰ سطر آماده را به ترتیب Index یکتای snapshot می‌خواند و دُم زنده تقریباً خالی است.
""",
    "q2_delivery_by_state_month.sql": """
### Q2 — نسبت تحویل دیرتر از وعده به تفکیک ایالت و ماه

**خواندن پلن قبل:** Seq Scan روی orders و customers و Hash Join؛ ~۹۶ هزار سفارش تحویل‌شده (حدود ۹۷٪ جدول) لازم است، پس Seq Scan انتخاب درستی است. تخمین و واقعیت سطرها نزدیک‌اند.

**تغییر:** (پیش‌تر در همین پروژه) تجمیع در یک CTE از نوع `MATERIALIZED` پیش از مرتب‌سازی نهایی، تا Sort روی ~۵۵۰ گروه انجام شود نه روی همهٔ سطرها. Index جدیدی برای Q2 ساخته نشد؛ Index پوشای جزئی امتحان شد و چون planner آن را انتخاب نکرد حذف شد (بخش ۷).

**چرا کار کرد:** مرتب‌سازی از سطح سفارش به سطح گروه منتقل شد؛ اسکن‌ها همان‌اند.
""",
    "q3_delay_and_satisfaction.sql": """
### Q3 — رابطهٔ تأخیر تحویل و امتیاز نظر

**خواندن پلن قبل:** Seq Scan روی order_reviews، HashAggregate برای میانگین امتیاز هر سفارش (عددی `numeric`) و Hash Join با سفارش‌های تحویل‌شده. بیشترین زمان در تجمیع هر سفارش و ساخت جدول hash است.

**تغییر:** `ix_order_reviews_order_score (order_id) INCLUDE (review_score)` و `ORDER BY order_id` در زیرکوئری نظرها.

**چرا کار کرد:** نظرها به ترتیب `order_id` از Index Only Scan می‌آیند (بدون دسترسی به heap و بدون Sort)، پس GroupAggregate جریانی کار می‌کند و جدول hash بزرگ میانی ساخته نمی‌شود. بهبود کوچک است و کوئری نزدیک بودجه می‌ماند؛ کار باقی‌مانده خواندن همهٔ سفارش‌های تحویل‌شده است.
""",
    "q4_top_sellers.sql": """
### Q4 — ۲۰ فروشندهٔ برتر

**خواندن پلن قبل:** چهار Seq Scan و تجمیع همهٔ فروشنده‌ها (حدود ۳ هزار) با جزئیات آماده‌سازی و امتیاز، در حالی که فقط ۲۰ ردیف لازم است. تخمین سطرهای reviews پس از تجمیع بسیار کمتر از واقعیت است.

**تغییر:** اول فقط درآمد هر فروشنده (با `NOT EXISTS` روی Index جزئی کوچک `ix_orders_inactive` به‌جای Join با همهٔ سفارش‌ها) و انتخاب ۲۰ نفر؛ بعد فقط سطرهای همین ۲۰ نفر (Hash Join با order_items)، تاریخ‌های هر سفارش از `ix_orders_dispatch_cover` (Index Only Scan) و امتیاز هر سفارش از `ix_order_reviews_order_score`.

**چرا کار کرد:** جزئیات گران فقط برای حدود یک‌پنجم سفارش‌ها حساب می‌شود و دسترسی به سفارش‌ها و نظرها با کلید و بدون heap است. این کوئری بیشترین نزدیکی را به بودجه دارد.
""",
    "q5_payment_distribution.sql": """
### Q5 — توزیع روش پرداخت و اقساط و رابطه با ارزش سفارش

**خواندن پلن قبل:** CTE مجموع هر سفارش دو بار خوانده می‌شود، Hash Join با ~۹۸ هزار سفارش و یک Sort بزرگ برای `COUNT(DISTINCT order_id)` در هر گروه.

**تغییر:** یک WindowAgg روی `ix_payments_order_type_inst` که از قبل به ترتیب (سفارش، روش، اقساط) است: مجموع هر سفارش، اولین سطر هر سفارش (برای همبستگی) و اولین سطر هر گروه در هر سفارش (برای شمارش یکتا) در یک گذر؛ سپس یک `GROUPING SETS` برای ۲۷ گروه و سطر کل.

**چرا کار کرد:** دادهٔ پرداخت یک بار و بدون Sort خوانده می‌شود و Join با orders به یک anti-join کوچک تبدیل شده است. همبستگی از نوع double precision است؛ ترتیب جمع زدن رقم‌های آخر آن را جابه‌جا می‌کند، به همین دلیل مقایسه با ۱۲ رقم معنادار است.
""",
}
for name in [
    "q1_monthly_category_sales.sql", "q2_delivery_by_state_month.sql",
    "q3_delay_and_satisfaction.sql", "q4_top_sellers.sql", "q5_payment_distribution.sql",
]:
    md(QUERY_NOTES[name])
    code(f'show_plan("{name}")')

md("""
## ۵. بنچمارک نهایی پنج کوئری داشبورد

همان روش بخش ۴، این بار فقط روی `sql/queries/`. معیار پذیرش: میانه و بیشینهٔ هر پانزده اجرا زیر ۲۰۰ میلی‌ثانیه.
""")

code("""
final = bench.benchmark_dir(engine, STAGE2 / "sql" / "queries", repeats=REPEATS, warmups=WARMUPS)
final_table = pd.DataFrame([
    {"query": name, "median_ms": r["median_ms"], "p95_ms": r["p95_ms"], "max_ms": r["max_ms"],
     "server_execution_ms": round(r["server_execution_ms"], 1), "rows": r["rows"],
     "all_runs_under_200ms": r["all_runs_under_budget"]}
    for name, r in final["queries"].items()])
display(final_table)
evidence.save("benchmark-final.json", {
    **{k: v for k, v in final.items() if k != "queries"},
    "queries": {n: {k: v for k, v in r.items() if k != "plan"}
                for n, r in final["queries"].items()}})
print("all five under budget on every run:", bool(final_table["all_runs_under_200ms"].all()))
""")

md("""
## ۶. شاهد هر Index

برای هر Index فایل 004، کوئری‌ای که به خاطرش ساخته شده دو بار اندازه‌گیری می‌شود: با Index، و در تراکنشی که Index را حذف و سپس rollback می‌کند. ستون `used_in_plan` از خود پلن می‌آید. برای `ix_orders_purchase_ts` snapshot موقتاً تا 2018-06 کوتاه می‌شود (باز هم داخل همان تراکنش) تا دُم زندهٔ Q1 سه ماه واقعی داشته باشد؛ با snapshot کامل این دُم تقریباً خالی است.
""")

code("""
index_ev = evidence.index_evidence(engine, STAGE2)
display(pd.DataFrame([
    {"index": name, "table": r["table"], "query": r["query"], "used_in_plan": r["used_in_plan"],
     "median_with_ms": r["median_with_ms"], "median_without_ms": r["median_without_ms"],
     "saved_ms": r["saved_ms"], "same_result": r["same_result"],
     "scans_without": ", ".join(r["without_index"]["scans"]["index_scan"]
                                + r["without_index"]["scans"]["seq_scan"])}
    for name, r in index_ev.items()]))
""")

md("""
## ۷. هزینهٔ پنهان Index

**حجم:** اندازهٔ هر Index و نسبت مجموع Indexهای 004 به حجم heap همان جدول‌ها. **نوشتن:** هر جدول یک بار در یک کپیِ دارای Indexهای 004 و یک بار در کپیِ فقط با کلید اصلی درج می‌شود (`INSERT ... SELECT` همهٔ سطرها، سه تکرار، در schema موقتی که rollback می‌شود؛ کلید خارجی در هیچ‌کدام از دو حالت نیست). **Indexهای حذف‌شده:** Index پوشای جزئی برای Q2 و Index `(seller_id, order_id)` برای Q4 ساخته، اندازه‌گیری و حذف شدند (هر کدام در تراکنشی که rollback می‌شود دوباره ساخته و اندازه‌گیری می‌شود).
""")

code("""
sizes = evidence.index_sizes(engine)
size_sum = evidence.size_summary(sizes)
display(pd.DataFrame(sizes))
display(pd.DataFrame(size_sum["tables"]).T)
print("004 indexes:", size_sum["total_004_bytes"], "bytes =",
      size_sum["ratio"], "x the heap of the tables they index")
""")

code("""
writes = evidence.write_cost(engine, repeats=3)
display(pd.DataFrame(writes).T[["rows_inserted", "indexes_004", "median_without_ms",
                                "median_with_ms", "slowdown_ratio"]])
rejected = evidence.rejected_indexes(engine, STAGE2)
display(pd.DataFrame([{"index": name, **{k: r[k] for k in (
    "query", "used_in_plan", "index_bytes", "build_ms", "median_without_ms",
    "median_with_ms", "same_result")}} for name, r in rejected.items()]))
evidence.save("index-evidence.json", {"indexes": index_ev, "rejected_indexes": rejected})
evidence.save("index-cost.json", {"sizes": sizes, "summary": size_sum, "write_cost": writes});
""")

md("""
## ۸. کوئری‌ای که Index نجاتش نمی‌دهد: Q1 (بخش ۵ صورت مسئله)

پلن «قبل» Q1 در بخش ۴ نشان می‌دهد هزینه در Sort و GroupAggregate روی همهٔ سطرهای آیتم است، نه در پیدا کردن سطرها. صورت سؤال (همهٔ ماه‌ها × همهٔ دسته‌ها، با سفارش یکتا) هیچ فیلتری ندارد که Index بتواند سطرهای خوانده‌شده را کم کند، و `COUNT(DISTINCT)` روی گروه‌ها مرتب‌سازی یا hash روی کل داده را اجباری می‌کند. حتی با همهٔ Indexهای 004 روی پایگاه‌داده، planner برای نسخهٔ اولیه همان Seq Scanها را انتخاب می‌کند (ستون `before` جدول بخش ۴ با Indexها حذف‌شده است؛ جدول زیر نسخهٔ اولیه را با Indexها هم نشان می‌دهد).

راه‌حل غیر Index: پیش‌محاسبهٔ ماه‌های بسته با procedure و محاسبهٔ زندهٔ فقط ماه‌های باز. **بهایی که پرداخت شد:** تازگی ماه‌های بسته به آخرین اجرای refresh بسته است، یک جدول و یک job نگهداری اضافه شد، و اگر job اجرا نشود دُم زنده بزرگ و کوئری کندتر می‌شود (جدول دوم: زمان Q1 بر حسب تعداد ماه‌هایی که snapshot پوشش می‌دهد؛ با snapshot خالی، پاسخ همچنان درست ولی کند است).
""")

code("""
with db.session(engine) as connection:
    baseline_with_indexes = bench.measure(
        connection, bench.read_query(STAGE2 / "sql" / "baseline" / "q1_monthly_category_sales.sql"),
        repeats=9, warmups=2)
display(pd.Series({"baseline q1 with all 004 indexes, median_ms": baseline_with_indexes["median_ms"],
                   "index scans in its plan": baseline_with_indexes["scans"]["index_scan"],
                   "seq scans in its plan": baseline_with_indexes["scans"]["seq_scan"]}))
tail = evidence.q1_tail_series(engine, STAGE2)
display(pd.DataFrame(tail).drop(columns=["scans"]))
""")

md("""
## ۹. View یا جدول snapshot برای سنگین‌ترین تحلیل (Q1)

چهار راه برای یک پاسخ: view زنده (`v_order_item_sales`)، فقط جدول snapshot، materialized view فایل 007، و ترکیب انتخاب‌شده. هر چهار باید همان SHA-256 را بدهند. آزمون تازگی: در یک تراکنش rollback‌شونده یک سفارش جدید (۱۰۰ واحد) یک بار در آخرین ماه بسته و یک بار در ماه باز درج می‌شود و بررسی می‌شود کدام روش آن را می‌بیند.
""")

code("""
vs = evidence.view_vs_snapshot(engine, STAGE2, repeats=REPEATS, warmups=WARMUPS)
display(pd.DataFrame([{"variant": k, "median_ms": v["median_ms"], "max_ms": v["max_ms"],
                       "rows": v["rows"], "result_sha256": v["result_sha256"][:16]}
                      for k, v in vs["timings"].items()]))
display(pd.DataFrame(vs["space"]))
print("refresh of all closed months (ms, 3 runs, rolled back):",
      vs["snapshot_refresh_all_closed_months_ms"])
display(pd.DataFrame({k: v["sees_new_order"] for k, v in vs["freshness"].items()}))
evidence.save("view-vs-snapshot.json", {**vs, "q1_tail_series": tail,
                                        "baseline_q1_with_indexes": {
                                            k: v for k, v in baseline_with_indexes.items()
                                            if k != "plan"}});
""")

md("""
## ۱۰. Procedure و Function

`olist.delivery_delay_days` (003) یک Function از نوع `IMMUTABLE` است و داخل `SELECT`، `WHERE` و `CASE` کوئری‌های Q2 و پایه‌ٔ Q3 به کار می‌رود؛ یک مقدار برمی‌گرداند و نمی‌تواند تراکنش را مدیریت کند یا جدول را تغییر دهد (`IMMUTABLE`). `olist.refresh_monthly_category_snapshot` (003) و نسخهٔ بازه‌ای آن (004) Procedure هستند: با `CALL` اجرا می‌شوند، داخل `SELECT` قابل استفاده نیستند، و داده را حذف و درج می‌کنند. دو اجرای پشت‌سرهم برای یک ماه باید همان سطرها را بدهد (idempotent)؛ رفتار در خطای میانهٔ کار در `tests/test_postgres_integration.py` آزمون شده است.
""")

code("""
month = {"month": "2018-05-01"}
grain = sa.text("SELECT count(*) AS rows, sum(sales_value) AS sales FROM "
                "olist.monthly_category_snapshot WHERE month_start = CAST(:month AS date)")
call = sa.text("CALL olist.refresh_monthly_category_snapshot(CAST(:month AS date))")
with engine.begin() as connection:
    connection.execute(call, month)
    first = connection.execute(grain, month).mappings().one()
    connection.execute(call, month)
    second = connection.execute(grain, month).mappings().one()
display(pd.DataFrame([dict(first), dict(second)], index=["first CALL", "second CALL"]))
assert dict(first) == dict(second)

with engine.connect() as connection:
    delays = pd.read_sql_query(sa.text(
        "SELECT olist.delivery_delay_days(order_delivered_customer_date, "
        "order_estimated_delivery_date) > 0 AS late, count(*) AS orders "
        "FROM olist.v_delivered_orders GROUP BY 1 ORDER BY 1"), connection)
display(delays)
""")

md("""
## ۱۱. پل Python و SQL

- **خواندن دسته‌ای از View:** ~۱۱۲ هزار سطر `v_order_item_sales` با `stream_results` و `chunksize` خوانده و دسته‌به‌دسته تجمیع می‌شود؛ هیچ‌وقت کل نتیجه در حافظهٔ کلاینت نیست. مقدار `since` پارامتر bind است.
- **نوشتن با نوع ستون مشخص:** خروجی Q2 با `to_sql` و نگاشت صریح (`DATE`، `CHAR(2)`، `INTEGER`، `NUMERIC(6,4)`) در schema `reporting` نوشته و نوع ذخیره‌شده از `information_schema` خوانده می‌شود.
- هیچ کوئری با چسباندن رشته ساخته نمی‌شود؛ شناسه‌ها با `psycopg.sql.Identifier` و مقدارها با پارامتر (آزمون `NoStringBuiltSqlTests`).
""")

code("""
chunked = bridge.read_view_in_chunks(engine, since="2017-01-01", chunksize=20_000)
print({k: v for k, v in chunked.items() if k != "monthly"})
display(chunked["monthly"].tail(6))
with engine.connect() as connection:
    q2 = pd.read_sql_query(sa.text(bench.read_query(
        STAGE2 / "sql" / "queries" / "q2_delivery_by_state_month.sql")), connection)
stored_types = bridge.write_typed(engine, q2)
display(stored_types)
print("rows written:", stored_types.attrs["rows_written"])
evidence.save("python-bridge.json", {
    "chunked_read": {k: v for k, v in chunked.items() if k != "monthly"},
    "chunked_monthly_revenue": chunked["monthly"].to_dict(orient="records"),
    "to_sql_types": stored_types.to_dict(orient="records"),
    "to_sql_rows": stored_types.attrs["rows_written"]});
""")

md("""
## ۱۲. تکرارپذیری

`scripts/determinism_two_loads.py` پایگاه‌داده را دو بار از صفر با همان یک فرمان می‌سازد و محتوای همهٔ جدول‌ها و نتیجهٔ پنج کوئری را مقایسه می‌کند (این اسکریپت مخرب است و جزو نوت‌بوک اجرا نمی‌شود؛ نتیجه‌اش در `data/curated/evidence/determinism-two-loads.json` است). این سلول هش نتیجهٔ کوئری‌های همین اجرا را با آن فایل مقایسه می‌کند. زمان‌ها در هر اجرا کمی فرق می‌کنند؛ پاسخ‌ها نباید فرق کنند.
""")

code("""
two_loads = json.loads((evidence.EVIDENCE_DIR / "determinism-two-loads.json").read_text(encoding="utf-8"))
print("two independent loads -> identical tables:", two_loads["identical_tables"],
      "| identical query results:", two_loads["identical_query_results"])
check = pd.DataFrame([
    {"query": name, "this_run": final["queries"][name]["result_sha256"][:16],
     "two_loads_file": two_loads["runs"][0]["queries"][name]["result_sha256"][:16]}
    for name in bench.QUERY_FILES])
check["same"] = check["this_run"] == check["two_loads_file"]
display(check)
""")

md("""
## ۱۳. بستن اتصال‌ها و جمع‌بندی اجرا

همهٔ اتصال‌ها با `with` باز و بسته شده‌اند؛ در پایان pool موتور dispose می‌شود و از یک اتصال جداگانه بررسی می‌شود که هیچ session با نام برنامهٔ این نوت‌بوک (`olist_perf`) روی پایگاه‌داده باقی نمانده باشد.
""")

code("""
engine.dispose()
leftover = db.leftover_sessions(engine)
elapsed = round(time.perf_counter() - started, 1)
evidence.save("notebook-run.json", {"elapsed_seconds": elapsed, "leftover_sessions": leftover,
                                    "seed": SEED, "repeats": REPEATS, "warmups": WARMUPS,
                                    "snapshot": snapshot_state})
print("leftover sessions:", leftover)
print("notebook elapsed seconds:", elapsed, "(cap: 600)")
assert leftover == []
""")

md("""
## AI usage

از دستیار هوش مصنوعی برای بازنویسی کوئری‌های Q1، Q3، Q4 و Q5، پیشنهاد و اندازه‌گیری Indexهای 004، نوشتن پکیج `olist_perf` و سازندهٔ این نوت‌بوک کمک گرفته شد. همهٔ اعداد از اجرای همین نوت‌بوک روی دادهٔ واقعی Olist آمده‌اند و در `data/curated/evidence/` ذخیره شده‌اند؛ روی ماشین خودتان با Restart & Run All دوباره تولید می‌شوند و ممکن است کمی فرق کنند.
""")

nb = nbf.v4.new_notebook()
nb["cells"] = cells
nb["metadata"] = {"kernelspec": {"display_name": "Python 3", "language": "python",
                                 "name": "python3"},
                  "language_info": {"name": "python"}}
nbf.write(nb, STAGE2 / "solution.ipynb")
print("cells:", len(cells))
