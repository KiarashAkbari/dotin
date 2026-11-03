# Olist Stage 2: پایگاه‌داده تحلیلی

این پوشه شامل SQL نسخه‌دار، نوتبوک اجراشده، گزارش تصمیم‌ها و تست‌های PostgreSQL است. پکیج اجرایی همان `data_factory` در پوشهٔ مرحلهٔ اول است.

## وضعیت شواهد

| مورد | وضعیت |
|---|---|
| `stage-1-kiarash-akbari/tests` | `Ran 257 tests`، `OK` |
| `stage-2-kiarash-akbari/tests` بدون `TEST_DATABASE_URL` | `Ran 46 tests`، `OK (skipped=25)` |
| `tests/test_factory_sink_sql.py` (آفلاین) | `Ran 21 tests`، `OK` |
| `tests/test_factory_sink_postgres.py` (PostgreSQL واقعی) | `Ran 16 tests`، `OK` |
| `tests/test_postgres_integration.py` (PostgreSQL واقعی) | `Ran 9 tests`، `OK` |
| `scripts/bootstrap_test_db.sh` عليه سرور واقعی | OK (۹ + ۱۶ آزمون، صفر skip) |
| `load-database --reset` کامل روی `olist_stage2_test` با داده‌ی واقعی | `exit=0`؛ `case_file` برابر 1,083,410 ردیف |

ردیف‌های PostgreSQL و CLI از اجرای واقعی روی دیتابیس یک‌بارمصرف `olist_stage2_test` آمده‌اند؛ خروجی‌ها در `docs/evidence/` هستند. skip شدن تست‌ها قبولی حساب نمی‌شود.

## محیط و نصب

اجرای گزارش‌شده با Python 3.12.3 و PostgreSQL 16.15 انجام شده است. سازگاری نسخه‌های دیگر و نصب از صفر در محیط تازه تأیید نشده‌اند. کف نسخهٔ پایتون در `../stage-1-kiarash-akbari/pyproject.toml` اعلام می‌شود (`>=3.12`)؛ علت آن متادیتای خودِ قفل است: `numpy==2.5.4` در PyPI به `requires_python >=3.12` نیاز دارد.

فرمان‌های زیر، مگر خلافش نوشته شده باشد، از پوشهٔ `stage-2-kiarash-akbari` اجرا می‌شوند. پوشهٔ هم‌سطح `../stage-1-kiarash-akbari` باید در همان checkout باشد.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

`requirements.txt` نسخه‌های محیط موفق را ثبت می‌کند و پکیج مرحلهٔ اول را با `-e ../stage-1-kiarash-akbari` نصب می‌کند. حضور pytest در آن به معنی استفاده از آن نیست؛ runner این مرحله `unittest` است.

## آماده‌سازی پایگاه‌داده اصلی

پیش‌نیازها: PostgreSQL در دسترس، نقش و دیتابیس موردنظر، CSVهای Olist و خروجی مرحلهٔ اول در مسیرهای پیکربندی‌شده. تنظیمات در `../stage-1-kiarash-akbari/config/default.toml` هستند. نام متغیر رمز از `database.password_env` همان فایل خوانده می‌شود و مقدار آن باید خارج از Git تنظیم شود.

از پوشهٔ مرحلهٔ اول و با Python محیطی که requirements مرحلهٔ دوم در آن نصب است:

```bash
cd ../stage-1-kiarash-akbari
../stage-2-kiarash-akbari/.venv/bin/python -m data_factory load-database \
  --config config/default.toml --reset
cd ../stage-2-kiarash-akbari
```

`--reset` مخرب است و schemaها را از نو می‌سازد؛ روی دیتابیس دارای دادهٔ ارزشمند اجرا نشود. موفقیت بنچمارک روی دیتابیس موجود اثبات اجرای این مسیر از دیتابیس خالی نیست.

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

`solution.ipynb` را در VS Code یا Jupyter باز کنید، Python محیط نصب‌شده را به‌عنوان kernel انتخاب کنید، سپس `Restart Kernel` و `Run All` و در پایان ذخیره. نوتبوک reset را خودکار اجرا نمی‌کند؛ پس Database باید قبلاً آماده باشد. رمز در صورت نیاز از ورودی پنهان گرفته می‌شود.

## بنچمارک تأییدشده

سه warmup و پانزده نمونه برای هر کوئری. زمان کلاینت شامل اجرای SQL، دریافت دسته‌ای و هش نتیجه است. معیار قبولی بیشینهٔ کمتر از 200ms است. پروتکل cache گرم دارد و بار هم‌زمان یا cold-cache را تضمین نمی‌کند.

| کوئری | میانه، ms | بیشینه، ms |
|---|---:|---:|
| Q1 | 11.994 | 16.128 |
| Q2 | 127.679 | 181.523 |
| Q3 | 1.215 | 2.245 |
| Q4 | 4.523 | 5.941 |
| Q5 | 3.525 | 4.089 |

شاهد: `data/curated/benchmark-final-20261028-203102-777061.json` و خروجی ذخیره‌شدهٔ نوتبوک. زمان نوتبوک 8.49 ثانیه و بدون بارگذاری اولیه است. مقایسهٔ قبل/بعد Q2 در `data/curated/q2-cte-comparison-20261028-202722-343310.json` است.

Q2 با CTE از نوع MATERIALIZED از دادهٔ زنده محاسبه می‌شود. Q1، Q3، Q4 و Q5 از materialized view می‌خوانند و تازگی‌شان به refresh بستگی دارد.

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

unset PGPASSWORD TEST_DATABASE_URL
```

اگر از محیط مرحلهٔ اول استفاده می‌کنید، به‌جای `.venv/bin/python` بنویسید `../stage-1-kiarash-akbari/.venv/bin/python`. `unset TEST_DATABASE_URL` مهم است: اگر در shell بماند، اجرای بعدی بدون رمز با خطای احراز هویت ۲۴ تست را خطا می‌کند.

`test_factory_sink_sql.py` به دیتابیس نیاز ندارد و متن `009` را با قرارداد loader مقایسه می‌کند. دو فایل دیگر بدون `TEST_DATABASE_URL` کاملاً skip می‌شوند.

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

جزئیات تصمیم‌ها، Function و Procedure و پوشش تست در `report.md` است. ممیزی FK، هزینهٔ درج ایندکس، مقایسهٔ کامل view/snapshot، بازسازی از صفر و تست نصب تازه هنوز شواهد تکمیلی لازم دارند، و این مستندات تکمیل همهٔ الزامات مرحله را اعلام نمی‌کنند. برای schema فکتوری، اعمال ۰۰۹، محدودیت‌ها، همگرایی و بازگشت‌پذیری روی PostgreSQL واقعی اجرا شده‌اند؛ همین مسیر با اجرای کامل CLI روی داده‌ی واقعی هم بسته شد. اسکریپت bootstrap با `--run-only` و `-A kiarash` شاهد دارد؛ فقط بارگذاری هم‌زمان هنوز بدون شاهد است.
