# روش ثبت شواهد زمان و پلن

ابزار اندازه‌گیری این مرحله `src/olist_perf/bench.py` است و نوت‌بوک آن را صدا می‌زند؛ نتیجه‌ها در `data/curated/evidence/*.json` نوشته می‌شوند و `report.md` فقط از همین فایل‌ها عدد می‌آورد. ابزار قدیمی `data_factory.db_benchmark` در پکیج مرحلهٔ اول باقی است، اما شواهد فعلی با آن تولید نشده‌اند.

## تعریف اندازه‌گیری

- یک session با `jit=off`، `work_mem=32MB`، `statement_timeout=120s` و `max_parallel_workers_per_gather=0`، تنظیم‌شده با `set_config` فقط برای همان session (`olist_perf.db.SESSION_SETTINGS`). سرور تغییر نمی‌کند.
- سه اجرای گرم‌کردن و پانزده اجرای اندازه‌گیری‌شده برای هر کوئری. زمان سمت کلاینت، از ارسال تا دریافت آخرین ردیف.
- میانه، صدک ۹۵ (nearest-rank؛ با پانزده نمونه برابر بیشینه) و بیشینه ذخیره می‌شوند. معیار پذیرش: بیشینه کمتر از ۲۰۰ms. هیچ نمونه‌ای حذف نمی‌شود.
- هر اجرا از نو plan می‌شود (`prepare_threshold=None`)؛ یک `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` جدا گرفته می‌شود و `Execution Time` آن جدا از زمان کلاینت گزارش می‌شود.
- هش SHA-256 ردیف‌ها (اعداد اعشاری شناور گرد به ۱۲ رقم معنادار) در همهٔ تکرارها باید ثابت باشد و نسخهٔ بهینه باید همان هش نسخهٔ `sql/baseline/` را بدهد.
- پیش از اندازه‌گیری: `VACUUM (ANALYZE)` روی همهٔ جدول‌های olist و ساخت snapshot ماه‌های بسته.
- آزمایش‌هایی که ساختار را تغییر می‌دهند (حذف Index برای «بدون Index»، ساخت Indexهای ردشده، REINDEX، درج انبوه روی کپی‌ها) در تراکنشی اجرا می‌شوند که rollback می‌شود.
- warm-cache است؛ cold-cache و بار هم‌زمان ادعا نمی‌شود.

## فایل‌های شاهد

| فایل | محتوا |
|---|---|
| `before-after.json` | نسخهٔ اولیه بدون Indexهای 004 در برابر نسخهٔ بهینه؛ زمان، پلن خلاصه، برابری خروجی |
| `benchmark-final.json` | بنچمارک نهایی پنج کوئری و محیط اندازه‌گیری |
| `index-evidence.json` | هر Index با و بدون خودش؛ Indexهای ساخته‌شده و حذف‌شده |
| `index-cost.json` | حجم Indexها، حجم پس از REINDEX، هزینهٔ درج با و بدون Index |
| `view-vs-snapshot.json` | Q1 به چهار روش، آزمون تازگی، زمان refresh، حساسیت به طول دُم زنده |
| `fk-audit.json` | یتیم‌های کلید خارجی در CSV خام و تعداد خام/بارگذاری‌شده |
| `python-bridge.json` | خواندن دسته‌ای از view و نوع ستون‌های `to_sql` |
| `notebook-run.json` | زمان کل نوت‌بوک و sessionهای باقی‌مانده |
| `determinism-two-loads.json` | دو بارگذاری مستقل از schema خالی و مقایسهٔ محتوا (اسکریپت جدا، مخرب) |
| `benchmark-final-unvacuumed-run.json` | اجرای پیش از VACUUM، دلیل گام نگهداری |
