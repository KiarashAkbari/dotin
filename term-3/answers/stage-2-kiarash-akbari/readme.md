# پایگاه‌داده تحلیلی Olist — مرحلهٔ دوم

این پوشه شامل SQL نسخه‌دار، نوتبوک اجراشده، گزارش تصمیم‌ها و تست‌های PostgreSQL است. پکیج اجرایی همان `data_factory` در پوشهٔ مرحلهٔ اول است.

## محیط بررسی‌شده و نصب

اجرای گزارش‌شده با Python 3.12.3 و PostgreSQL 16.15 انجام شده است. سازگاری نسخه‌های دیگر و نصب از صفر در محیط تازه در این دور تأیید نشده‌اند.

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

## دامنهٔ تحویل

جزئیات تصمیم‌ها، Function و Procedure و پوشش تست در `report.md` آمده‌اند. ممیزی FK، هزینهٔ درج ایندکس، مقایسهٔ کامل view/snapshot، بازسازی از صفر و تست نصب تازه هنوز نیازمند شواهد تکمیلی‌اند. این مستندات، تکمیل تمام الزامات مرحله را اعلام نمی‌کنند.
