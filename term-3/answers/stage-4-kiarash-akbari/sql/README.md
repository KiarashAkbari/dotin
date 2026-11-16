# ویژگی‌های stage-4 در SQL — قرارداد فایل‌ها

پنج فایلِ برنامه‌ریزی‌شده نوشته شده‌اند و همین پوشه‌اند. یک نکته را از اول روشن
می‌گذاریم که پیش از این مانع نوشتنشان بود: زمان‌بندیِ کوئری‌سازِ frame با `EXPLAIN
(ANALYZE)` روی دیتابیس زنده اندازه‌گیری نشده (بند ۵.۳)، و این فایل‌ها هم ادعایش را
نمی‌کنند. چیزی که الان هست، خودِ SQL و تست‌های ایستای روی همان SQL است؛ عددِ زمان،
`rows examined` و تأیید `pg_depend` در گره‌های زندهٔ سندباکس می‌آید و تا آن موقع هر
جا که لازم است «بررسی نشده» نوشته می‌شود. SQL بدون اندازه‌گیری اشکال ندارد؛
اندازه‌گیریِ بی‌SQL اشکال داشت.

## نام‌گذاری و ترتیب اجرا

`NNN_نام_کوتاه.sql`، با سه رقم، بدون پرش، اجرا به ترتیب نام فایل — همان الگوی
`stage-2/sql/` و `stage-3/sql/`. هر فایل یک‌بار اجراشدنی باشد و اجرای دوباره‌اش چیزی
را خراب نکند (`idempotent`): `CREATE OR REPLACE VIEW`، `DROP VIEW IF EXISTS` پیش از
ساختن، و برای `procedure`ها `CREATE OR REPLACE FUNCTION`.

فایل‌های برنامه‌ریزی‌شده، به همان ترتیبی که فاز ۳ و ۴ می‌سازندشان:

| فایل | چه می‌سازد | چرا جدا |
|---|---|---|
| `001_capstone_schema.sql` | schema، جدول آینهٔ ممیزی `capstone.column_audit`، و `capstone.build_run` | `ROADMAP` فاز ۳ بند ۳: منشأ داده در schema جدا ممیزی می‌شود |
| `002_time_guard.sql` | `v_decision_moment`، دید‌های شمارش D1، `v_audit_rejections`، و توابع `audit_is_allowed` / `require_allowed` | مرز نشت باید در خود SQL باشد، نه در توضیح (بند ۴.۲) |
| `003_feature_views.sql` | `v_feature_frame` (۲۹ ویژگی)، دیدهای جمعِ سفارش/پرداخت/جغرافیا، و `v_order_label` جدا | یک دید، یک بار خوانش؛ زمان‌گیری روی همین است (بند ۵.۱) |
| `004_as_of_functions.sql` | تابع‌های تجمیعیِ تاریخی با شرط as-of؛ نسخهٔ کند و صریحِ همان عددِ `003` | شرط «فقط سطرهای تأییدشدهٔ قبل» باید در خود تابع دیده شود (بند ۵.۲) |
| `005_external_signal.sql` | `external_municipio`، `v_external_signal`، و دید نرخ تطابق/شهرهای بی‌تطابق | نتیجهٔ منفی هم همین‌جا ثبت می‌شود، نه در گزارش (بند ۱۲.۵) |

شماره‌ها قفل نیستند؛ اگر فاز ۴ تصمیم گرفت view به‌جای procedure یا برعکس، فایل اضافه
با شمارهٔ بعدی می‌آید و همین جدول به‌روز می‌شود. پرش شماره مجاز نیست، چون «فایل ۰۰۴
نیست» با «فایل ۰۰۴ حذف شد» یکی به نظر می‌رسد.

## checksum، مثل دو مرحلهٔ قبل

هر فایل `.sql` در `stage-4/docs/evidence/sql-checksums.txt` با sha256 ثبت می‌شود و یک
تست همان فهرست را با محتوای پوشه مقایسه می‌کند (الگو:
`stage-3/tests/test_credit_sql_checksums.py`). اگر checksum نخورد، اجرا باید بایستد؛
نه اینکه هشدار بدهد. دلیلش همان درس مرحلهٔ ۲ است: دیدی که دستی در pgAdmin ساخته شده،
در مخزن وجود ندارد.

## چه چیزی به‌صورت خودکار کنترل می‌شود

`tests/test_capstone_sql_guards.py` دو دسته تست دارد و تفکیکشان مهم است:

- **ایستا، همیشه اجرا می‌شوند:** نام و ترتیب فایل‌ها با همین جدول، checksumها با
  `scripts/write_sql_checksums.py` (اجرای `--check` در CI هم همان را می‌سنجد)،
  «هر `CREATE` باید `OR REPLACE` یا `IF NOT EXISTS` باشد»، ممنوعیت `SELECT *` و
  هر نوشتن روی `olist.`، و اینکه هر ستون `olist.` که در بلوکِ یک view ویژگی
  خوانده می‌شود در `column_audit.csv` حکم `allowed` دارد. سه استثنا دارد، به‌نام:
  `v_order_label` (برچسب)، `v_d1_excluded_orders` و `v_d2_unlabelled_orders`
  (شمارش‌های D1/D2) — هیچ‌کدام در زنجیرهٔ `v_feature_frame` نیستند و بزرگ‌تر شدن
  این فهرست خودش یک تست را می‌شکند.
- **زنده، با `CAPSTONE_DATABASE_URI`:** ستون‌هایی که `pg_depend` واقعاً به
  `v_feature_frame` نسبت می‌دهد از `capstone.require_allowed` عبور می‌کنند، شمارش
  ≥۱۰ ویژگی و ≥۶ جدول از کاتالوگ خوانده می‌شود، نبودِ برچسب در frame و نبودِ
  `order_reviews` در وابستگی‌هایش بررسی می‌شود، و عدد `prior_orders_by_customer`
  که window می‌دهد با تابعِ کندِ as-of در `004` سطر‌به‌سطر مقایسه می‌شود. اگر
  متغیر ست نباشد، این‌ها skip می‌شوند — skip سبز نیست؛ پیامِ skip می‌گوید دقیقاً چه
  چیزی تأیید نشده.

اجرای دستی روی یک دیتابیسِ بارگذاری‌شده، به همان ترتیب جدول بالا:

```bash
for f in stage-4/sql/0*.sql; do psql "$DATABASE_URI" -v ON_ERROR_STOP=1 -f "$f"; done
python3 stage-4/scripts/write_sql_checksums.py --check
CAPSTONE_DATABASE_URI="$DATABASE_URI" python3 -m unittest discover -s stage-4/tests
```

## چیزی که این قرارداد ممنوع می‌کند

1. `SELECT *` روی `olist.v_delivered_orders`. آن دید با `WHERE order_status =
   'delivered'` و `order_delivered_customer_date IS NOT NULL` ساخته شده
   (`stage-2/sql/002_analytical_views.sql`، سطرهای ۹ تا ۱۱) و سوار شدن رویش یعنی
   سوگیری بقای بی‌صدا (بند ۴.۷ چک‌لیست).
2. هر ویژگی تجمیعی که `WHERE` محدودکنندهٔ زمانی در خودِ SQL نداشته باشد. توضیح در
   نوتبوک، شاهد نیست.
3. هر ستونی که در فایل ممیزی (`time_guard.audit_file`، یعنی
   `docs/evidence/column_audit.csv`) حکم `allowed` ندارد. دو در دارد: دروازهٔ پایتون
   (`capstone.audit.require_allowed`، که فاز ۴ پیش از ساخت frame صدا می‌زند — آن
   نیمه هنوز placeholder است) و دروازهٔ داخل دیتابیس
   (`capstone.require_allowed(…)`, در `002`) که وابستگی واقعی view را از کاتالوگ
   می‌خواند. `capstone.column_audit` تا بارگذاری شدنش خالی است و خالی یعنی «رد»،
   نه «قبول»: دروازه‌ای که داده ندارد باید ببندد، نه باز کند.
