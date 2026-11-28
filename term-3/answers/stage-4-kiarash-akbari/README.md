# مرحلهٔ ۴ — سامانهٔ هشدار زودهنگام

سامانه‌ای که در لحظهٔ تأیید هر سفارش Olist می‌گوید کدام سفارش‌ها احتمالاً به نظر منفی (امتیاز ۱ یا ۲)
می‌رسند، و ۱۰٪ بالای فهرست را برای پیگیری تیم پشتیبانی بیرون می‌دهد. یک فرمان، از دیتابیسِ خالی تا فهرست.

- گزارش «دلیل حل»: `report.md` — تصمیم‌های معماری: `decisions.md` — جمع‌بندی برای مدیر عملیات: `manager_summary.md`
- نوتبوک‌ها: `01_exploration.ipynb` (کشف، ممیزی زمانی، فرضیه‌ها، شکست‌ها) و `02_system.ipynb` (روایت سامانه)
- وضعیت هر الزام: `docs/REQUIREMENTS_CHECKLIST.md` — شاهدها: `docs/evidence/`. شناسهٔ بندها در کد با رقم ASCII
  (`4.1`) و در چک‌لیست با رقم فارسی (`۴.۱`) نوشته می‌شود؛ یکی‌اند.
- سقف‌ها: کل زنجیره زیر 45 دقیقه، کوئری ویژگی زیر 500 میلی‌ثانیه (`config` → `budget.chain_minutes`، `features.feature_query_ms`).

## پیش‌نیاز

- Python 3.12 و PostgreSQL 16 (یک دیتابیس خالی و کاربری که بتواند در آن schema بسازد).
- نه فایل CSVِ Olist (`olistbr/brazilian-ecommerce` از Kaggle) در `data/raw/olist/` — یا هر پوشه‌ای که
  `CAPSTONE_OLIST_DIR` به آن اشاره کند. داده در Git نمی‌آید.
- دسترسی به `servicodados.ibge.gov.br` (منبع بیرونی؛ رایگان و بی‌کلید).
- پوشهٔ `../stage-1-kiarash-akbari` کنار همین پوشه (لایهٔ دریافت همان package مرحلهٔ ۱ است).

## راه‌اندازی و اجرای کل زنجیره

```bash
cd term-3/answers/stage-4-kiarash-akbari
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt                 # stage 1 هم با -e ../stage-1-kiarash-akbari نصب می‌شود
export PYTHONPATH="$PWD/src:$PWD/tests:$PWD/../stage-1-kiarash-akbari/src"

# آدرس دیتابیس فقط در یک فایل 0600 بیرون از مخزن؛ هرگز در فرمان، history یا config
mkdir -p ~/.config/capstone && (umask 077 && ${EDITOR:-nano} ~/.config/capstone/db_uri)
#   یک خط:  CAPSTONE_DATABASE_URI=postgresql://USER:PASSWORD@localhost:5432/DBNAME
set -a; . ~/.config/capstone/db_uri; set +a

export CAPSTONE_OLIST_DIR="/path/to/olist/csvs"   # اگر CSVها در data/raw/olist نیستند
python scripts/run_chain.py                      # یک فرمان: ingest → load → audit → features → external → model → list
```

`CAPSTONE_DATABASE_URI` تنها تنظیمِ دیتابیس است: گامِ stage 1 رمز را از همین URI می‌گیرد (یک
`OLIST_DB_PASSWORD` کهنه در shell دیگر بر آن غلبه نمی‌کند) و هیچ پیامِ خطایی رمز را چاپ نمی‌کند.

خروجی‌ها در `data/curated/`: `followup_list.csv` (فهرست پیگیری)، `capacity_table.json` (جدول ۵/۱۰/۲۰٪)،
`model_metrics.json`، `model.joblib`، و manifest زمان هر مرحله. کد خروج غیرصفر یعنی یک مرحله ایستاده و
manifest می‌گوید کدام. روی ThinkPadِ دانشجو کل زنجیره روی دیتابیسِ تازه‌ساخته ۲۳۶٫۲ ثانیه طول کشید، ۷ از ۷ گام
`ok` (`docs/evidence/chain-run-manifest.rash-ThinkPad-L14-Gen-1-2026-11-28.json`؛ بودجه ۴۵ دقیقه)؛ روی سندباکس (۲ vCPU)
۱۲۷٫۴ ثانیه (`docs/evidence/chain-run-manifest.json`). روی ماشین خودتان دوباره بگیرید.

`config/capstone.default.toml` قرارداد تصمیم‌گرفته و بی‌رمز است و همه‌چیز از آن خوانده می‌شود؛ اگر روی ماشین
خودتان چیزی متفاوت لازم است، `config/capstone.toml` بسازید (در Git نمی‌آید).

## پیش‌بینی برای سفارش‌های تازه

```bash
python -m capstone predict --input path/to/fresh_orders/        # پوشه‌ای با CSVهای هم‌نام و هم‌schemaِ Olist
```

پوشه باید `olist_orders_dataset.csv`، `olist_customers_dataset.csv`، `olist_order_items_dataset.csv` و
`olist_order_payments_dataset.csv` داشته باشد (محصولات و فروشندگان اختیاری). سرستون‌ها با DDLِ مرحلهٔ ۲ سنجیده
می‌شوند و فایل غلط با پیامی که نام فایل و ستون را می‌گوید رد می‌شود. ردیف‌ها با loaderِ مرحلهٔ ۱ در یک schemaِ
موقت بار می‌شوند، همان Viewها ویژگی را می‌سازند، مدلِ ذخیره‌شده رتبه می‌دهد، و تراکنش rollback می‌شود؛ دیتابیس
دست‌نخورده می‌ماند. پیش‌نیاز: زنجیره یک بار اجرا شده باشد. تمرین همین مسیر روی یک ماه:
`python scripts/sealed_cut_rehearsal.py --olist "$CAPSTONE_OLIST_DIR"`.

## فرمان‌های دیگر

| فرمان | کار |
|---|---|
| `python -m capstone doctor` | پیش‌نیازها، config، جدول ممیزی و فایل‌های تحویل را می‌سنجد و می‌گوید چه چیزی کم است |
| `python -m capstone audit --coverage --require-complete` | هر ستونی که به مدل می‌رسد در `docs/evidence/column_audit.csv` حکم و منبع دارد؟ |
| `python -m capstone chain` | همان زنجیرهٔ `scripts/run_chain.py`؛ با `--dry-run` فقط برنامه را چاپ می‌کند |
| `python -m capstone predict --input <folder>` | مسیر پیش‌بینی (بالا) |
| `python -m capstone external --fetch --report` | یک درخواست به IBGE و نرخ تطابق شهرها |

## تست‌ها

```bash
export PYTHONPATH="$PWD/src:$PWD/tests:$PWD/../stage-1-kiarash-akbari/src"   # stage 1 لازم است
python -m unittest discover -s tests -t .           # بدون CAPSTONE_DATABASE_URI تست‌های زنده skip می‌شوند
set -a; . ~/.config/capstone/db_uri; set +a
python -m unittest discover -s tests -t .           # با دیتابیسِ ساخته‌شده توسط زنجیره: قراردادِ SQLِ زنده هم اجرا می‌شود
# تست‌های smoke/سرتاسری یک دیتابیسِ دورریختنی را پاک و دوباره می‌سازند — هرگز دیتابیسِ اصلی:
CAPSTONE_SMOKE_DATABASE_URI=postgresql://USER:PASSWORD@localhost:5432/capstone_scratch \
CAPSTONE_SMOKE_ALLOW_DESTROY=1 python -m unittest tests.test_postgres_smoke tests.test_end_to_end
```

تست‌های `test_postgres_smoke` به `psql` روی `PATH` نیاز دارند و بدون آن skip می‌شوند؛ چهار تست سرتاسری در هر حال اجرا
می‌شوند. شمارِ آخرین اجراها: `docs/evidence/suite-runs-2026-11-27.txt`.

تست ضدنشت در `tests/test_capstone_sql_guards.py` و `tests/test_capstone_features.py` است؛ تست سرتاسری
(زنجیره روی دادهٔ ساختگی تا پیش‌بینی) در `tests/test_end_to_end.py`.

## زمانِ کوئریِ ویژگی (سقف ۵۰۰ ms) روی ماشین خودتان

```bash
python scripts/analyze_ceiling_feature.py --tag "$(hostname -s)-$(date +%F)" --repeats 15
# شاهد: docs/evidence/ceiling-feature-analysis.<tag>.txt (+ .json) با پلن؛ نامِ تکراری بی --force رد می‌شود
```

گامِ `features` پیش از پنج تکرارِ زمان‌گیری، هر جدول و snapshot را که قاب به آن وابسته است (فهرست از کاتالوگ)
با `VACUUM (ANALYZE)` جا می‌اندازد. دلیلش این است که درست بعد از `load-database --reset`، autovacuum و
صفحه‌های تازه‌نوشته‌شده عدد را بالا می‌بردند، بی آن‌که پلن عوض شود (`decisions.md` §۴۲). در خروجیِ گام،
`settled_relations` و `settle_seconds` نشان می‌دهند چه چیزی جا افتاد و چقدر طول کشید، و `autovacuum_workers_seen`
نشان می‌دهد autovacuum هنگامِ تکرارها فعال بود یا نه. اگر سقف شکسته شود، همین‌ها در پیامِ خطا هم می‌آیند. نقشِ
دیتابیس باید مالکِ جدول‌ها باشد (همان نقشی که بارگذاری کرده)، چون `VACUUM` روی جدولِ دیگران فقط هشدار می‌دهد و
کاری نمی‌کند.

**نتیجه روی ThinkPadِ دانشجو (۲۰۲۶-۱۱-۲۸):** سقف با آمارِ داوری‌شده (بهترینِ تکرارها) برقرار است، با حاشیهٔ کم، و
پایدار نیست. گامِ `features` در زنجیره بهترین ۴۲۵٫۶ و میانگین ۴۳۷٫۴ ms از ۵ تکرار گرفت و autovacuum در هر ۶ نمونه ۰ بود
(`docs/evidence/chain-run-log.rash-ThinkPad-L14-Gen-1-2026-11-28.txt`). تحلیلگرِ ۱۵ تکراری بعد از `ANALYZE` بهترین
۴۱۲٫۲، میانگین ۴۵۷٫۲ و بدترین ۵۳۰٫۱ ms داد و ۲ از ۱۵ اجرا بالای ۵۰۰ بود؛ حکمِ خودِ ابزار «red: unresolved at this repeat
count» است (`docs/evidence/ceiling-feature-analysis.rash-ThinkPad-L14-Gen-1-2026-11-28.txt`). عددهای سندباکس
(`ceiling-feature-analysis.sandbox-2026-11-27.txt`، بهترین ۳۵۷٫۲ ms) ثانوی‌اند.

## ساختن نوتبوک‌ها و شاهدها (بعد از زنجیره)

```bash
python scripts/build_exploration_notebook.py --execute --kernel --force   # 01_exploration.ipynb در kernel تازه
python scripts/build_system_notebook.py                                    # 02_system.ipynb در kernel تازه
python scripts/cheating_model_gap.py                                       # مدل تقلبی ← docs/evidence/
python scripts/boundary_sensitivity.py                                     # حساسیتِ مرز ← docs/evidence/
python docs/evidence/model-ablation-run.py                                 # با/بدون منبع بیرونی، همهٔ خانواده‌ها
```

## ساختار

```
src/capstone/   ingest، features، external، system (مدل)، capacity، predict، chain، cli
sql/            001–006: schema، نگهبان زمانی، Viewهای ویژگی، توابع as-of، منبع بیرونی، پل
scripts/        run_chain، notebook builders، cheating/sealed-cut harnessها، checksums
config/         capstone.default.toml (تصمیم‌ها) و capstone.example.toml
docs/           evidence/، feature_hypotheses.csv، سلول‌های دو نوتبوک، چک‌لیست
tests/          تست‌های مرحله (آفلاین + زنده با DB)
```
