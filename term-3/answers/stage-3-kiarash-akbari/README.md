# مرحلهٔ سوم — مدل اعتباری با معیار «هزینهٔ تصمیم»

دادهٔ Give Me Some Credit با package مرحلهٔ اول در PostgreSQL بارگذاری می‌شود، نوت‌بوک فقط
از Database (view `credit.v_model_input`) می‌خواند و کد سنگین در `src/credit/` است. نتیجه و
دلیل تصمیم‌ها در `report.md` است؛ هر عدد آن از `docs/evidence/real/credit_evidence.json` می‌آید.

## ساختار

```
solution.ipynb          نوت‌بوک اجراشده روی cs-training.csv واقعی (تولید: scripts/build_solution_notebook.py)
report.md               گزارش فارسی؛ سلول آخر نوت‌بوک جای‌نگهدارهایش را از evidence پر می‌کند
src/credit/             بارگذاری، اعتبارسنجی، ویژگی، split، prep، مدل، هزینه، ارزیابی، پیش‌بینی
sql/                    010 اسکیما و view، 011 استیجینگ پیش‌بینی، sql/queries دو کوئری اندازه‌گیری
tests/                  آزمون‌های unittest
scripts/                ژنراتور نوت‌بوک، اجراکنندهٔ نوت‌بوک، سازندهٔ دادهٔ آزمایشی
docs/evidence/real/     evidence و manifest همان اجرای واقعی، و run.txt
config/credit.example.toml   الگوی تنظیمات منبع (کپی به config/credit.toml که ردگیری نمی‌شود)
data/raw, data/curated  فایل Kaggle و خروجی‌های اجرا؛ هیچ‌کدام در Git نیستند
```

## اجرا

همهٔ فرمان‌ها از داخل همین پوشه. Python 3.12 و PostgreSQL لازم است.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # مرحلهٔ اول را هم با -e ../stage-1-kiarash-akbari نصب می‌کند
export PYTHONPATH=src
read -s OLIST_DB_PASSWORD && export OLIST_DB_PASSWORD   # رمز فقط در محیط، هرگز در فایل
# اگر دیتابیس شما با config/default.toml مرحلهٔ اول فرق دارد، یک کپی ردگیری‌نشده بسازید:
# export CREDIT_STAGE1_CONFIG=/path/to/my-stage1.toml

cp config/credit.example.toml config/credit.toml
# cs-training.csv و cs-test.csv را از صفحهٔ رقابت Kaggle در data/raw/ بگذارید

python -m credit.load --dry-run --credit-config config/credit.toml   # بدون DB: شمارش و قرنطینه
python -m credit.load --reset   --credit-config config/credit.toml   # مهاجرت 010/011 و بارگذاری کامل

python scripts/run_notebook.py --budget-minutes 20    # Run All، و بررسی خطا/مسیر مطلق/سقف زمان
python -m credit.predict --file data/raw/cs-test.csv --bundle data/curated/model \
    --credit-config config/credit.toml                 # فایل تازه با همان schema، بدون برچسب

python -m unittest discover -s tests
```

بعد از Run All، `report.md` از evidence همان اجرا پر می‌شود و `git status` آن را تغییرکرده
نشان می‌دهد؛ این طراحی است. برای بایگانی یک اجرای تازه:

```bash
cp data/curated/credit_evidence.json data/curated/credit_run_manifest.json docs/evidence/real/
```

`ArchivedEvidenceConsistencyTests` بررسی می‌کند که دو فایل بایگانی دربارهٔ یک ورودی حرف بزنند
و هیچ عددی در `report.md` بیرون از evidence نباشد.

## وضعیت شواهد

شرح دقیق آخرین اجرا (بارگذاری، زمان نوت‌بوک، پیش‌بینی روی `cs-test.csv`، نتیجهٔ آزمون‌ها با
دیتابیس) در `docs/evidence/real/run.txt` است. عددهای مدل فقط در `report.md` و evidence آمده‌اند
تا یک منبع داشته باشند.

## قواعدی که آزمون‌ها نگه می‌دارند

- نوت‌بوک CSV نمی‌خواند، یک seed در سلول اول دارد، و متن سلول‌هایش دقیقاً خروجی ژنراتور است.
- همهٔ ثابت‌های آماده‌سازی فقط روی train ساخته می‌شوند؛ آستانهٔ هر تکهٔ CV روی ردیف‌های آموزشی
  همان تکه، و آستانهٔ نهایی روی validation انتخاب می‌شود؛ holdout فقط یک بار دیده می‌شود.
- مهاجرت‌های اعمال‌شده ویرایش نمی‌شوند (checksum در ledger)؛ تغییر schema فایل تازه می‌گیرد.
- `requirements.txt` قفل کامل است و هر import در کد یا نوت‌بوک باید در آن پین شده باشد.
- `scripts/make_fixture.py` فقط برای آزمودن ماشین‌افزار بدون فایل Kaggle است؛ خروجی آن نتیجهٔ چالش
  نیست و بایگانی نمی‌شود.
