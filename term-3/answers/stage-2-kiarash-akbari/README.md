# Olist Stage 2: پایگاه‌داده تحلیلی

SQL نسخه‌دار (`sql/`)، پکیج شواهد `olist_perf` (`src/`)، نوت‌بوک اجراشده، گزارش و تست‌ها. بارگذاری با پکیج `data_factory` مرحلهٔ اول انجام می‌شود.

## اجرای سریع (از پوشهٔ همین مرحله)

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt   # data_factory و olist_perf هم editable نصب می‌شوند
export OLIST_DB_PASSWORD=...            # فقط در shell؛ هیچ‌جا commit نشود
# اختیاری: export OLIST_CONFIG=../stage-1-kiarash-akbari/config/local.toml  (اگر نام دیتابیس/کاربر شما فرق دارد)
(cd ../stage-1-kiarash-akbari && ../stage-2-kiarash-akbari/.venv/bin/python -m data_factory load-database --config config/default.toml --reset --skip-factory-sink)
.venv/bin/python scripts/run_notebook.py   # یا: Jupyter، Restart Kernel & Run All
```

فرمان سوم مخرب است (schema `olist` را از نو می‌سازد و Indexهای `004` را پیش از بارگذاری می‌سازد). نوت‌بوک چیزی نمی‌پرسد: بدون `OLIST_DB_PASSWORD` بلافاصله با پیام روشن متوقف می‌شود. شواهد اجرا در `data/curated/evidence/*.json` بازنویسی می‌شوند.

## وضعیت شواهد (اجرای 2026-11-25، PostgreSQL 16.4، Python 3.12.16، ماشین مشترک ۲ هسته‌ای)

| مورد | نتیجه | شاهد |
|---|---|---|
| ساخت از schema خالی با یک فرمان، دو بار | دو بارگذاری مستقل: محتوای همهٔ جدول‌ها و نتیجهٔ پنج کوئری یکسان؛ هر بار حدود ۵۵ ثانیه | `data/curated/evidence/determinism-two-loads.json` |
| نوت‌بوک، Restart & Run All بدون دخالت (بلافاصله پس از بارگذاری تازه) | ۲۱ سلول کد با شمارهٔ ۱ تا ۲۱، بدون خطا، 136.1 ثانیه؛ هیچ session باقی‌مانده | `solution.ipynb`، `notebook-run.json` |
| پنج کوئری داشبورد، ۱۵ اجرای اندازه‌گیری‌شده | همهٔ اجراها زیر ۲۰۰ms (جدول پایین) | `benchmark-final.json` |
| `tests/` با `TEST_DATABASE_URL` | `Ran 72 tests`، `OK`، صفر skip | (پیش از این شاخه: 46 OK) |
| `tests/` بدون پایگاه‌داده | `Ran 72 tests`، `OK (skipped=32)` | skip قبولی حساب نمی‌شود |

شواهد قدیمی‌تر PostgreSQL و CLI (اجرای مالک پروژه روی `olist_stage2_test`) در `docs/evidence/` و بخش «بارگذاری خروجی مرحلهٔ اول» همین فایل است.

## محیط و نصب

اجراهای قبلی مالک پروژه با Python 3.12.3 و PostgreSQL 16.15 و اجرای شواهد 2026-11-25 با Python 3.12.16 و PostgreSQL 16.4 انجام شده‌اند. سازگاری نسخه‌های دیگر و نصب از صفر در محیط تازه تأیید نشده‌اند. کف نسخهٔ پایتون در `../stage-1-kiarash-akbari/pyproject.toml` اعلام می‌شود (`>=3.12`)؛ علت آن متادیتای خودِ قفل است: `numpy==2.5.4` در PyPI به `requires_python >=3.12` نیاز دارد.

فرمان‌های زیر، مگر خلافش نوشته شده باشد، از پوشهٔ `stage-2-kiarash-akbari` اجرا می‌شوند. پوشهٔ هم‌سطح `../stage-1-kiarash-akbari` باید در همان checkout باشد.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

`requirements.txt` نسخه‌های محیط موفق را ثبت می‌کند و پکیج مرحلهٔ اول را با `-e ../stage-1-kiarash-akbari` و پکیج همین مرحله (`src/olist_perf`) را با `-e .` نصب می‌کند. حضور pytest در آن به معنی استفاده از آن نیست؛ runner این مرحله `unittest` است.

## آماده‌سازی پایگاه‌داده اصلی

پیش‌نیازها: PostgreSQL در دسترس، نقش و دیتابیس موردنظر، CSVهای Olist و خروجی مرحلهٔ اول در مسیرهای پیکربندی‌شده. تنظیمات در `../stage-1-kiarash-akbari/config/default.toml` هستند. نام متغیر رمز از `database.password_env` همان فایل خوانده می‌شود و مقدار آن باید خارج از Git تنظیم شود.

از پوشهٔ مرحلهٔ اول و با Python محیطی که requirements مرحلهٔ دوم در آن نصب است:

```bash
cd ../stage-1-kiarash-akbari
../stage-2-kiarash-akbari/.venv/bin/python -m data_factory load-database \
  --config config/default.toml --reset
cd ../stage-2-kiarash-akbari
```

`--reset` مخرب است و schemaها را از نو می‌سازد؛ روی دیتابیس دارای دادهٔ ارزشمند اجرا نشود. ترتیب: 001 تا 004 (جدول‌ها، viewها، Function و Procedure، Indexها) پیش از بارگذاری، سپس CSVها، سپس 009 و snapshotهای 005 تا 008. Indexهای 004 پیش از بارگذاری ساخته می‌شوند؛ هزینهٔ این ترتیب (Indexهای کم‌تراکم‌تر) در `report.md` آمده است.

پس از هر بارگذاری، ماه‌های بسته در `olist.monthly_category_snapshot` باید دوباره ساخته شوند (Q1 از آن می‌خواند). نوت‌بوک این کار و `VACUUM (ANALYZE)` را در بخش ۳ انجام می‌دهد؛ بیرون از نوت‌بوک: `olist_perf.evidence.refresh_closed_months(engine)`.

اثبات تکرارپذیری (مخرب؛ فقط روی دیتابیس یک‌بارمصرف): `python scripts/determinism_two_loads.py --config <config> --out data/curated/evidence/determinism-two-loads.json`.

## بارگذاری خروجی مرحلهٔ اول در schema فکتوری

`load-database` پس از بارگذاری CSVهای Olist و پیش از snapshotها، `sql/009_factory_sink_schema.sql` را اعمال می‌کند و سه خروجی مرحلهٔ اول را در schema مستقل `factory` می‌نویسد.

| جدول | محتوا |
|---|---|
| `case_file` | ردیف‌های `cases.csv` |
| `complaint_area_month` | جدول wide در شکل narrow، یک ردیف برای هر ماه |
| `column_quality` | پروفایل ستون‌ها از گزارش کیفیت JSON |
| `quality_meta` | مقادیر واحد گزارش (سقف یکتا، تعداد ناسازگاری) |
| `borough_conflict` | یک ردیف برای هر جفت `(modzcta, borough)` متعارض |
| `load_run` | نام فایل، SHA-256 و تعداد ردیف هر مقصد |

نام فایل‌ها و پیشوند ستون‌های ماه از ماژول‌های نویسندهٔ آن‌ها می‌آید (`outputs.DATASET_FILENAME`، `outputs.WIDE_FILENAME`، `profile.REPORT_FILENAME`، `outputs.COUNT_PREFIX`، `outputs.RATE_PREFIX`). گزارش کیفیت به‌صورت JSON پارس و با پارامتر bound درج می‌شود و از CSV reader عبور نمی‌کند.

بارگذاری truncate-then-insert و در یک تراکنش است: اجرای دوباره تعداد ردیف‌ها را دو برابر نمی‌کند و خطا در میانهٔ کار ردیف‌های بار قبلی را دست‌نخورده می‌گذارد.

گزینه‌ها و پیام‌ها:

- `--skip-factory-sink`: هیچ DDL فکتوری اعمال نمی‌شود و بارگذاری انجام نمی‌گیرد؛ در این حالت `--reset` فقط `olist` را حذف می‌کند.
- نبودن `009_factory_sink_schema.sql` خطاست (`Factory sink migration is missing`)، نه skip. با `--skip-sql-migrations` پیام `factory_sink=skipped` چاپ می‌شود.
- `--reset` دو schema را با دو تابع جدا حذف می‌کند (`reset_olist_schema` و `reset_factory_schema`) تا دامنهٔ عمل مخرب در کد دیده شود.
- خروجی استاندارد: `factory_migrations=…` و سپس برای هر جدول `factory_table=… loaded=…`.

معنای `quality_meta.conflict_count`: تعداد جفت‌های `(modzcta, borough)` ذخیره‌شده است و همیشه با `COUNT(*)` جدول `borough_conflict` برابر است. فیلد `borough_modzcta_conflict_count` در گزارش JSON تعداد ورودی‌هاست (یک ورودی برای هر modzcta). همان عدد را `SELECT COUNT(DISTINCT modzcta) FROM factory.borough_conflict` می‌دهد.

## اجرای نوتبوک

`solution.ipynb` را از پوشهٔ همین مرحله باز کنید و Restart Kernel & Run All بزنید، یا `scripts/run_notebook.py` را اجرا کنید (kernel تازه، خروجی‌ها ذخیره می‌شوند). نوت‌بوک reset را اجرا نمی‌کند و رمز را فقط از `OLIST_DB_PASSWORD` می‌خواند (`getpass` حذف شد). همهٔ آزمایش‌های تغییردهندهٔ ساختار (حذف موقت Index، ساخت Index ردشده، درج انبوه روی کپی) در تراکنشی اجرا می‌شوند که rollback می‌شود. سلول‌ها با `scripts/build_solution_notebook.py` ساخته می‌شوند.

## بنچمارک تأییدشده

روش: یک session با `jit=off`، `work_mem=32MB`، `max_parallel_workers_per_gather=0`؛ سه اجرای گرم‌کردن و پانزده اجرای اندازه‌گیری‌شده؛ زمان سمت کلاینت تا آخرین ردیف؛ cache گرم (تضمین cold-cache یا بار هم‌زمان نیست). «قبل» = `sql/baseline/` (کامیت 534c6ac) بدون Indexهای 004؛ «بعد» = `sql/queries/` با آن‌ها. خروجی هر دو نسخه ردیف‌به‌ردیف یکی است.

| کوئری | قبل: میانه / بیشینه، ms | بعد، نهایی: میانه / بیشینه، ms | تغییر اصلی |
|---|---:|---:|---|
| Q1 | 319.316 / 325.264 | 7.758 / 8.716 | snapshot ماه‌های بسته + محاسبهٔ زندهٔ ماه‌های باز (نه Index) |
| Q2 | 148.673 / 152.966 | 115.135 / 125.743 | CTE `MATERIALIZED` (بدون Index) |
| Q3 | 201.542 / 229.714 | 155.104 / 157.688 | Index پوشای نظرها، تجمیع جریانی |
| Q4 | 458.818 / 472.255 | 187.469 / 190.169 | اول ۲۰ فروشنده، بعد جزئیات با کلید |
| Q5 | 469.093 / 476.689 | 159.039 / 160.093 | یک WindowAgg روی Index پرداخت + `GROUPING SETS` |

شاهد: ستون «قبل» از `data/curated/evidence/before-after.json` و ستون «بعد» از `data/curated/evidence/benchmark-final.json`. در مرحلهٔ before/after همان اجرا یکی از پانزده نمونهٔ Q3 به 209.5ms رسید؛ Q4 کم‌حاشیه‌ترین کوئری است. شواهد اکتبر (`data/curated/benchmark-final-2026102*.json`، `q2-cte-comparison-*.json`) تاریخی‌اند و به نسخهٔ materialized view کوئری‌ها تعلق دارند.

## تست‌های نیازمند PostgreSQL

فقط از دیتابیس جدا، خالی و قابل‌دورانداختن استفاده کنید و اتصال تست را هرگز به دیتابیس اصلی ندهید. مدیری که مجوز دارد:

```bash
createdb -h localhost -U postgres -O kiarash olist_stage2_test
```

اگر دیتابیس از قبل وجود دارد، خودکار حذف یا reset نشود. برای اجرا بدون نوشتن رمز در خط فرمان یا history از Bash استفاده کنید. رمز را بدون هیچ علامت اضافه تایپ کنید؛ نمایش داده نمی‌شود.

```bash
read -r -s -p 'PostgreSQL password for test role: ' PGPASSWORD
printf '\n'
export PGPASSWORD
export TEST_DATABASE_URL='postgresql+psycopg://kiarash@127.0.0.1:5432/olist_stage2_test'

.venv/bin/python -m unittest discover -s tests -p 'test_factory_sink_postgres.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_postgres_integration.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_olist_perf.py' -v

unset PGPASSWORD TEST_DATABASE_URL
```

اگر از محیط مرحلهٔ اول استفاده می‌کنید، به‌جای `.venv/bin/python` بنویسید `../stage-1-kiarash-akbari/.venv/bin/python`. `unset TEST_DATABASE_URL` مهم است: اگر در shell بماند، اجرای بعدی بدون رمز با خطای احراز هویت تست‌های PostgreSQL را خطا می‌کند.

`test_factory_sink_sql.py` به دیتابیس نیاز ندارد و متن `009` و جایگاه `004` را با قرارداد loader مقایسه می‌کند. `test_olist_perf.py` بیشتر آفلاین است (هش نتیجه، پلن، تنظیمات، نبود SQL ساخته‌شده با الحاق رشته) و دو آزمون PostgreSQL دارد (نوع ستون‌های `to_sql`، نبود session باقی‌مانده). `test_postgres_integration.py` علاوه بر fixture قبلی، برابری هر کوئری بهینه با نسخهٔ `sql/baseline/`، درستی Q1 با snapshot خالی/ناقص/کامل، Procedure بازه‌ای و قید یکتایی snapshot را می‌آزماید. فایل‌های PostgreSQL بدون `TEST_DATABASE_URL` skip می‌شوند. اجرای کامل این شاخه با پایگاه‌داده: `Ran 72 tests`، `OK`، صفر skip.

اجرای کامل CLI روی دیتابیس یک‌بارمصرف، بدون دست‌زدن به `olist_db`: یک کپی از config بسازید که فقط نام دیتابیس را عوض می‌کند، آن را commit نکنید و رمز را فقط در همان shell تنظیم کنید.

```bash
cp ../stage-1-kiarash-akbari/config/default.toml ../stage-1-kiarash-akbari/config/stage2-test.toml
sed -i 's#/olist_db"#/olist_stage2_test"#' ../stage-1-kiarash-akbari/config/stage2-test.toml
grep -n '^url = "postgresql' ../stage-1-kiarash-akbari/config/stage2-test.toml   # باید olist_stage2_test باشد
read -r -s -p 'PostgreSQL password: ' OLIST_DB_PASSWORD; printf '\n'; export OLIST_DB_PASSWORD
(cd ../stage-1-kiarash-akbari && .venv/bin/python -m data_factory load-database --config config/stage2-test.toml --reset)
unset OLIST_DB_PASSWORD
```

اسکریپت `scripts/bootstrap_test_db.sh` آماده‌سازی و اجرای هر دو فایل را خودکار می‌کند:

```bash
./scripts/bootstrap_test_db.sh --help   # گزینه‌ها و کارهایی که عمداً نمی‌کند
./scripts/bootstrap_test_db.sh -n       # dry run؛ هیچ فرمانی اجرا نمی‌شود
./scripts/bootstrap_test_db.sh -d olist_stage2_test -U kiarash -A postgres
```

دیتابیس را در صورت نبودن می‌سازد، در برابر دیتابیسی که schema یا جدول دارد امتناع می‌کند (مگر با `--force`)، هیچ SQL اعمال نمی‌کند و چیزی حذف نمی‌کند. رمز با `read -s` در فایل موقت ۰۶۰۰ نوشته و در `trap` پاک می‌شود. خروجی با `tee` نمایش داده می‌شود و کد خروج از `PIPESTATUS[0]` خوانده می‌شود. این اسکریپت عليه سرور واقعی اجرا نشده است؛ گزینهٔ `-A` به اعتبار یک نقش مدیر نیاز دارد.

شواهد PostgreSQL موجود:

- `docs/evidence/postgres-integration-user-run.txt`: ۹ تست یکپارچگی، رونویسی خروجی ارسالی کاربر.
- `docs/evidence/factory-sink-postgres-run.txt`: اجرای نخست، ۱۵ تست sink و ۹ تست یکپارچگی.
- `docs/evidence/factory-sink-postgres-run-2.txt`: اجرای پس از اصلاح ساعت پاییزی، ۱۶ تست sink و ۹ تست یکپارچگی.
- `docs/evidence/load-database-cli-run-1-failed.txt` و `docs/evidence/load-database-cli-run-2.txt`: اجرای CLI روی داده‌ی واقعی، به‌ترتیب شکست (`exit=1`) و موفق (`exit=0`).

## دامنهٔ تحویل

جزئیات تصمیم‌ها، چرخهٔ قبل/بعد هر کوئری، شاهد هر Index، هزینهٔ Index، کوئری‌ای که Index نجاتش نمی‌دهد، view در برابر snapshot، Function و Procedure و پل Python در `report.md` است و هر عدد آن به یک فایل در `data/curated/evidence/` ارجاع دارد. موارد بدون شاهد: بارگذاری هم‌زمان دو sink، زمان cold-cache و بار هم‌زمان، و اجرای نوت‌بوک روی ماشین مالک پروژه (اعداد بالا از ماشین مشترک ۲ هسته‌ای آمده‌اند و روی ماشین دیگر کمی فرق می‌کنند).
