# پایگاه‌داده تحلیلی Olist — مرحلهٔ دوم

این پوشه شامل SQL نسخه‌دار، نوتبوک اجراشده، گزارش تصمیم‌ها و تست‌های PostgreSQL است. پکیج اجرایی همان `data_factory` در پوشهٔ مرحلهٔ اول است.

## محیط بررسی‌شده و نصب

اجرای گزارش‌شده با Python 3.12.3 و PostgreSQL 16.15 انجام شده است. سازگاری نسخه‌های دیگر و نصب از صفر در محیط تازه در این دور تأیید نشده‌اند. کف نسخهٔ پایتون در `../stage-1-kiarash-akbari/pyproject.toml` اعلام می‌شود (`>=3.12`)؛ علت آن متادیتای خودِ قفل این محیط است: `numpy==2.5.4` در PyPI `requires_python >=3.12` می‌خواهد، پس اعلام 3.11 بازنمایی همین lock نیست.

تمام فرمان‌های زیر، مگر آنکه خلافش نوشته شده باشد، از پوشهٔ `stage-2-kiarash-akbari` اجرا می‌شوند. پوشهٔ هم‌سطح `../stage-1-kiarash-akbari` باید در همان checkout موجود باشد.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

`requirements.txt` snapshot نسخه‌های محیط موفق است و پکیج مرحلهٔ اول را با `-e ../stage-1-kiarash-akbari` نصب می‌کند. حضور pytest در snapshot محیط به معنی استفاده از آن در تست‌های این مرحله نیست؛ runner این مرحله `unittest` است.

## آماده‌سازی پایگاه‌داده اصلی

پیش‌نیازها: PostgreSQL در دسترس، نقش و دیتابیس موردنظر، CSVهای Olist و خروجی مرحلهٔ اول در مسیرهای پیکربندی‌شده. تنظیمات در `../stage-1-kiarash-akbari/config/default.toml` قرار دارند. نام متغیر رمز را از `database.password_env` همان تنظیمات بخوانید و مقدار آن را خارج از Git تنظیم کنید.

از پوشهٔ مرحلهٔ اول، با Python محیطی که requirements مرحلهٔ دوم در آن نصب شده است:

```bash
cd ../stage-1-kiarash-akbari
../stage-2-kiarash-akbari/.venv/bin/python -m data_factory load-database \
  --config config/default.toml --reset
cd ../stage-2-kiarash-akbari
```

هشدار: `--reset` مخرب است و بازسازی schema را درخواست می‌کند. روی دیتابیس دارای دادهٔ ارزشمند اجرا نشود. این دستور، مسیر مستند آماده‌سازی است؛ موفقیت بنچمارک روی دیتابیس موجود، اثبات اجرای آن از Database خالی نیست.

## بارگذاری خروجی مرحلهٔ اول در schema فکتوری

فرمان `load-database` پس از بارگذاری CSVهای Olist و پیش از snapshotها، فایل `sql/009_factory_sink_schema.sql` را اعمال می‌کند و سه خروجی مرحلهٔ اول را در schema مستقل `factory` می‌نویسد: `case_file` (ردیف‌های `cases.csv`)، `complaint_area_month` (جدول wide در شکل narrow، یک ردیف برای هر ماه) و `column_quality` به‌همراه `quality_meta`، `borough_conflict` و `load_run`.

خواندن این فایل‌ها از مسیر همان پکیج انجام می‌شود که آن‌ها را نوشته است: پیشوند ستون‌های ماه از `outputs.COUNT_PREFIX` و `outputs.RATE_PREFIX` می‌آید و نام فایل‌ها از `outputs.DATASET_FILENAME`، `outputs.WIDE_FILENAME` و `profile.REPORT_FILENAME`. گزارش کیفیت به‌عنوان JSON پارس می‌شود و با پارامتر bound درج می‌گردد؛ هرگز به CSV reader داده نمی‌شود.

این بارگذاری truncate-then-insert در یک تراکنش است: اجرای دوباره روی همان خروجی‌ها تعداد ردیف‌ها را دو برابر نمی‌کند، و خطا در میانهٔ راه ردیف‌های بار پیشین را دست‌نخورده باقی می‌گذارد. جدول `load_run` نام فایل، SHA-256 و شمارٔ ردیف هر مقصد را ثبت می‌کند تا بررسی آن به `run_manifest.json` وابسته نباشد.

گزینه‌ها و پیام‌ها:

- `--skip-factory-sink`: هیچ درگی از schema فکتوری اعمال نمی‌شود و بارگذاری انجام نمی‌گیرد؛ در این حالت `--reset` هم فقط `olist` را حذف می‌کند.
- نبودن `009_factory_sink_schema.sql` خطاست (`Factory sink migration is missing`)، نه skip. اگر `--skip-sql-migrations` داده شود، پیام `factory_sink=skipped` چاپ می‌شود.
- `--reset` دو schema را با دو تابع جدا (`reset_olist_schema` و `reset_factory_schema`) حذف می‌کند تا دامنهٔ عمل مخرب در کد خوانده شود.
- خروجی استاندارد: `factory_migrations=…` و سپس برای هر جدول `factory_table=… loaded=…`.

وضعیت اجرا: کد و تست‌های offline آن در محیط بازبینی اجرا شده‌اند. خودِ بارگذاری روی PostgreSQL در این محیط **اجرا نشده است** (NOT RUN)؛ سرور PostgreSQL در دسترس نبود. آنچه برای بستن این شکاف لازم است در `report.md` بخش ۴.۷ نوشته شده است.

## اجرای نوتبوک

`solution.ipynb` را در VS Code یا Jupyter باز کنید و Python محیط نصب‌شده را به‌عنوان kernel انتخاب کنید. سپس `Restart Kernel` و `Run All` و در پایان ذخیرهٔ فایل را انجام دهید. نوتبوک reset را خودکار اجرا نمی‌کند؛ پس Database باید قبلاً آماده باشد. رمز در صورت نیاز از ورودی پنهان دریافت می‌شود.

## بنچمارک تأییدشده

سه warmup و پانزده نمونه برای هر کوئری؛ زمان کلاینت شامل اجرای SQL، دریافت دسته‌ای و هش نتیجه است. معیار قبولی، بیشینهٔ کمتر از ۲۰۰ms است. این پروتکل cache گرم است، نه تضمین بار هم‌زمان یا cold-cache.

| کوئری | میانه، ms | بیشینه، ms |
|---|---:|---:|
| Q1 | 11.994 | 16.128 |
| Q2 | 127.679 | 181.523 |
| Q3 | 1.215 | 2.245 |
| Q4 | 4.523 | 5.941 |
| Q5 | 3.525 | 4.089 |

شاهد: `data/curated/benchmark-final-20261028-203102-777061.json` و خروجی ذخیره‌شدهٔ نوتبوک. زمان نوتبوک 8.49 ثانیه و بدون بارگذاری اولیه است. مقایسهٔ قبل/بعد Q2 در `data/curated/q2-cte-comparison-20261028-202722-343310.json` ثبت شده است.

Q2 با CTE از نوع MATERIALIZED از دادهٔ زنده محاسبه می‌شود. Q1، Q3، Q4 و Q5 از materialized view استفاده می‌کنند و تازگی آن‌ها وابسته به refresh است. خواندن سریع آن‌ها به معنی محاسبهٔ دوبارهٔ دادهٔ پایه در هر درخواست نیست.

## تست‌های یکپارچگی

فقط از Database جداگانه، خالی و قابل‌دورانداختن استفاده کنید؛ هرگز اتصال تست را به دیتابیس اصلی ندهید. نمونهٔ ساخت توسط مدیری که مجوز لازم دارد:

```bash
createdb -h localhost -U postgres -O kiarash olist_stage2_test
```

این فرمان وابسته به احراز هویت و مجوز محیط است. در صورت وجود دیتابیس، آن را خودکار حذف یا reset نکنید.

برای اجرای تست بدون نوشتن رمز در خط فرمان یا history، از Bash استفاده کنید:

```bash
bash
read -r -s -p 'PostgreSQL password for test role: ' PGPASSWORD
printf '\n'
export PGPASSWORD
export TEST_DATABASE_URL='postgresql+psycopg://kiarash@localhost:5432/olist_stage2_test'

.venv/bin/python -m unittest discover \
  -s tests -p 'test_postgres_integration.py' -v

unset PGPASSWORD
exit
```

اگر از محیط موفق مرحلهٔ اول استفاده می‌کنید، به‌جای `.venv/bin/python` بنویسید `../stage-1-kiarash-akbari/.venv/bin/python`.

آخرین خروجی ارسالشدهٔ کاربر: ۹ تست در 2.294 ثانیه، همگی موفق، بدون skip. شاهد رونویسی‌شدهٔ خروجی در `docs/evidence/postgres-integration-user-run.txt` است. این فایل خروجی اجرای تازهٔ دستیار نیست. نبودن `TEST_DATABASE_URL` باعث skip شدن تست‌ها می‌شود؛ skip را موفقیت حساب نکنید.

برای آماده‌سازی و اجرای هر دو فایل نیازمند سرور، اسکریپت این پوشه را ببینید:

```bash
./scripts/bootstrap_test_db.sh --help   # گزینه‌ها و کاری که عمداً نمی‌کند
./scripts/bootstrap_test_db.sh -n       # dry run؛ هیچ فرمانی اجرا نمی‌شود
./scripts/bootstrap_test_db.sh -d olist_stage2_test -U kiarash -A postgres
```

اسکریپت دیتابیس را اگر نباشد می‌سازد، در برابر دیتابیسی که schema یا جدول دارد امتناع می‌کند (مگر با `--force`)، هیچ SQL اعمال نمی‌کند و هیچ چیزی را حذف نمی‌کند. رمز با `read -s` در فایل موقت با مجوز ۰۶۰۰ نوشته و در `trap` پاک می‌شود؛ در URL، در log و در هیچ فایل مخزن نوشته نمی‌شود. خروجی هر دو suite با `tee` نمایش داده می‌شود و کد خروج از `PIPESTATUS[0]` خوانده می‌شود، نه کد `tee`.

اجرای دستی، پس از آماده‌سازی دیتابیس خالی:

```bash
export TEST_DATABASE_URL='postgresql+psycopg://kiarash@localhost:5432/olist_stage2_test'

.venv/bin/python -m unittest discover \
  -s tests -p 'test_factory_sink_sql.py' -v
.venv/bin/python -m unittest discover \
  -s tests -p 'test_factory_sink_postgres.py' -v
```

اولی به دیتابیس نیاز ندارد و متن `009` را با قرارداد loader مقایسه می‌کند. دومی بدون `TEST_DATABASE_URL` کاملاً skip می‌شود و این skip نشانهٔ قبولی نیست. تا این دور هیچ تست نیازمند PostgreSQL برای schema فکتوری اجرا نشده است؛ تنها شواهد PostgreSQL موجود، همان ۹ تست یکپارچگیِ ارسال‌شدهٔ کاربر است که schema فکتوری را در بر نمی‌گیرد.

## دامنهٔ تحویل

جزئیات تصمیم‌ها، Function و Procedure و پوشش تست در `report.md` آمده‌اند. ممیزی FK، هزینهٔ درج ایندکس، مقایسهٔ کامل view/snapshot، بازسازی از صفر و تست نصب تازه هنوز نیازمند شواهد تکمیلی‌اند. این مستندات، تکمیل تمام الزامات مرحله را اعلام نمی‌کنند. بارگذاری schema فکتوری نیز در همین دسته است: درج، محدودیت‌ها و بازگشت‌پذیری آن در تست‌های offline پوشش داده شده‌اند، اما SQL آن روی PostgreSQL اجرا نشده است.
