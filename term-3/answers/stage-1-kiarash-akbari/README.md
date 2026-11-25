# کارخانه داده 311 نیویورک

یک package پایتونی که شکایت‌های 311 نیویورک (NYPD، شش نوع noise، ژانویه 2025 تا ژوئن 2026) را دریافت، اعتبارسنجی، پاک‌سازی و با جمعیت هر محله ترکیب می‌کند.

## اجرا

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    pip install -e .
    python -m unittest discover -s tests      # تست‌ها، بدون اینترنت
    python -m data_factory run                # کل کارخانه با یک فرمان

نیازمند Python 3.12 یا بالاتر است (`requires-python` در `pyproject.toml`). علت، متادیتای خودِ قفل است: `numpy==2.5.4` در PyPI فقط روی `>=3.12` نصب می‌شود. برای اجرای نوت‌بوک بدون `requirements.txt` می‌شود از `pip install -e ".[analysis]"` استفاده کرد.

اجرای کامل از پوشه‌ی خالی شامل دانلود حدود 1.08 میلیون رکورد است. اجرای دوباره روی همان بازه، بدون درخواست جدید (`requests=0`) و با hash یکسان خروجی‌ها انجام می‌شود. اجرای `--since-last-run` فقط داده‌ی بعد از آخرین اجرا را می‌گیرد. هر افزایش (increment) پوشه‌ی خودش را دارد: `data/curated/rolling/<run_id>/` (با manifest همان افزایش) و `data/quarantine/rolling/<run_id>/`؛ افزایش‌های قبلی بازنویسی نمی‌شوند. اگر چیز تازه‌ای نباشد، اجرا با کد ۰ و صفر سطر تمام می‌شود. `data/run_manifest.rolling.json` کپیِ manifest آخرین افزایش است و watermark اجرای بعدی از آن خوانده می‌شود. توکن Socrata اختیاری است: متغیر `SOCRATA_APP_TOKEN`.

## خروجی‌ها (data/)

- `curated/cases.csv`: dataset سطح پرونده با مدت تا بسته‌شدن
- `curated/complaints_by_area_month.csv`: جدول wide منطقه × نوع شکایت × ماه (تعداد و نرخ به ازای 10,000 نفر)
- `curated/data_quality_report.json`: گزارش کیفیت هر ستون
- `quarantine/quarantine.jsonl`: رکوردهای ردشده با کد دلیل
- `run_manifest.json`: شمارش‌ها، بودجه‌ی درخواست و hash هر خروجی

## فرمان‌های دیگر

    python -m data_factory fetch | fetch-reference | validate | clean
    python -m data_factory run --since-last-run   # فقط داده‌ی بعد از آخرین اجرا

تنظیمات در `config/default.toml`. مسیرهای نسبیِ این فایل نسبت به `[paths] root` حل می‌شوند و خودِ `root` نسبت به پوشه‌ی فایل config است (`".."` یعنی پوشه‌ی stage-1)، پس فرمان از هر پوشه‌ای با `--config <مسیر>` همان فایل‌ها را پیدا می‌کند. اگر نسخه‌ی محلیِ دیگری از config دارید، `root = ".."` را به آن اضافه کنید. نوت‌بوک: `solution.ipynb` (Restart & Run All).

## بارگذاری در PostgreSQL (`load-database`)

    OLIST_DB_PASSWORD=... python -m data_factory load-database --reset --skip-factory-sink

- همه‌ی مراحل (reset، migrationها، بارگذاری Olist، refresh نماها و sink) روی یک اتصال و در یک تراکنش اجرا می‌شوند و یک commit دارند. اگر هر مرحله خطا بدهد، `--reset` هم برمی‌گردد و داده‌ی قبلی سالم می‌ماند.
- `--reset` از جدول مشترک `public.data_factory_migrations` فقط ردیف‌های 001 تا 008 را پاک می‌کند (و بدون `--skip-factory-sink` ردیف 009 را)؛ تاریخچه‌ی stageهای دیگر (مثل 010) می‌ماند. migration شاخص‌های stage 2 (`004_*.sql`) همراه 001 تا 003 و پیش از بارگذاری اجرا می‌شود.
- پیش از بارگذاری دوباره، جدول‌ها با یک `TRUNCATE ... RESTART IDENTITY` خالی می‌شوند، نه با `DELETE` جدول‌به‌جدول. ستون‌های کلید خارجی index ندارند، پس `DELETE` برای هر ردیف والد یک بار جدول فرزند را scan می‌کند. اندازه‌گیری روی داده‌ی کامل Olist (99,441 سفارش، sandbox مشترک با 2 vCPU): `TRUNCATE` حدود 3 میلی‌ثانیه (میانه‌ی 3 اجرا) و حلقه‌ی `DELETE` قدیمی حدود 554 ثانیه (orders 206، customers 268، products 73 ثانیه). بارگذاری دوباره‌ی کامل بدون `--reset` 51 ثانیه و با `--reset` 82.5 ثانیه طول کشید. شواهد: `docs/evidence/reload_delete_vs_truncate.json`؛ اسکریپت: `scripts/benchmark_reload.py`.
