# پایگاه‌داده تحلیلی Olist و بهینه‌سازی کوئری‌های داشبورد

این مرحله شامل پایگاه‌داده تحلیلی PostgreSQL برای داده‌های Olist و پنج کوئری داشبورد بهینه‌شده است.

## پیش‌نیازها

- PostgreSQL 14 یا جدیدتر
- Python 3.11 یا جدیدتر
- محیط مجازی پکیج `data-factory` در مرحلهٔ ۱
- فایل‌های CSV داده‌های Olist در مسیر پیکربندی‌شده

## ساخت پایگاه‌داده

از مسیر پکیج مرحلهٔ ۱:

```bash
cd ../stage-1-kiarash-akbari
source .venv/bin/activate

python -m data_factory load-database --config config/default.toml --reset
```

این دستور اسکیمای `olist` را بازسازی می‌کند، داده‌ها را بارگذاری می‌کند و materialized snapshotها را refresh می‌کند.

## کوئری‌های داشبورد

```text
sql/queries/
```

| فایل | هدف |
|---|---|
| `q1_monthly_category_sales.sql` | فروش ماهانه بر اساس دستهٔ محصول |
| `q2_delivery_by_state_month.sql` | عملکرد تحویل بر اساس ایالت و ماه |
| `q3_delay_and_satisfaction.sql` | تأخیر تحویل و رضایت مشتری |
| `q4_top_sellers.sql` | فروشندگان برتر بر اساس درآمد |
| `q5_payment_distribution.sql` | توزیع نوع پرداخت |

## نتایج بنچمارک

| کوئری | میانه | بیشینه |
|---|---:|---:|
| Q1 | ۱۳٫۸۸ میلی‌ثانیه | ۶۲٫۵۰ میلی‌ثانیه |
| Q2 | ۱۵۳٫۳۰ میلی‌ثانیه | ۱۷۳٫۶۷ میلی‌ثانیه |
| Q3 | ۱٫۲۷ میلی‌ثانیه | ۲٫۴۵ میلی‌ثانیه |
| Q4 | ۳٫۵۵ میلی‌ثانیه | ۴٫۸۴ میلی‌ثانیه |
| Q5 | ۳٫۱۳ میلی‌ثانیه | ۴٫۲۱ میلی‌ثانیه |

## ساختار پوشهٔ SQL

```text
sql/
├── 001_create_olist_schema.sql
├── 002_analytical_views.sql
├── 003_functions_and_snapshots.sql
├── 005_q4_seller_snapshot.sql
├── 006_q5_payment_snapshot.sql
├── 007_q1_monthly_category_snapshot.sql
├── 008_q3_delay_satisfaction_snapshot.sql
├── queries/
└── rejected/
```

## اجرای تست‌ها

```bash
cd ../stage-1-kiarash-akbari
source .venv/bin/activate
pytest -q
```