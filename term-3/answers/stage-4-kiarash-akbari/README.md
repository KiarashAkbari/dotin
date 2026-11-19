# مرحلهٔ ۴ — سامانهٔ هشدار زودهنگام (اسکلت)

این پوشه فعلاً **اسکلت** است، نه سامانه. چیزی که اینجا هست را می‌توان اجرا کرد و چیزی
که نیست را با نام فاز و شمارهٔ بند می‌گوید. دلیلش یک قاعدهٔ ساده است: اگر «یک فرمان» و
مرزهای زمانی و شکل خطاها آخر کار ساخته شوند، فاز ۴ و ۵ کدشان را حول آن‌ها شکل نمی‌دهند
و همان لحظه‌ای که همه‌چیز باید زیر ۴۵ دقیقه تمام شود، کشف می‌شود.

وضعیت هر الزام در `docs/REQUIREMENTS_CHECKLIST.md` است. شناسه‌های بندها در کد و در این
فایل با رقم ASCII نوشته می‌شوند (`4.1`) و خودِ فایل چک‌لیست با رقم فارسی (`۴.۱`)؛
یکی‌اند.

## اجرا از صفر

```bash
cd stage-4
python3 -m venv .venv && source .venv/bin/activate
cp config/capstone.example.toml config/capstone.toml     # نسخهٔ محلی؛ در Git نمی‌آید
export PYTHONPATH="$PWD/src:$PWD/../stage-1/src"
python3 -m unittest discover -s tests                     # آفلاین: بدون DB، بدون شبکه
python3 -m capstone doctor                                # چه چیزی هنوز باز است
python3 -m capstone chain --dry-run                       # ترتیب زنجیره و گره‌هایش
python3 scripts/write_sql_checksums.py --check             # فایل‌های SQL هم‌checksum‌اند؟
```

`--config` را می‌شود هم قبل از فرمان داد و هم بعدش (`capstone doctor --config F` و
`capstone --config F doctor`)؛ اگر فایلی که نام برده‌ای وجود نداشته باشد، خطا با کد `78`
می‌آید و بی‌سروصدا سراغ پیش‌فرض نمی‌رویم — یک غلط تایپی نباید خودش را «قراردادِ
تصمیم‌نگرفته» جا بزند. تست‌های زندهٔ SQL (آن‌ها که `olist` بارگذاری‌شده می‌خواهند) با
`CAPSTONE_DATABASE_URI` فعال می‌شوند؛ بدون آن skip می‌شوند و پیامِ skip می‌گوید دقیقاً چه
چیزی تأیید نشده. خود SQL را این‌طور روی یک دیتابیس می‌گذاری:

```bash
for f in sql/0*.sql; do psql "$CAPSTONE_DATABASE_URI" -v ON_ERROR_STOP=1 -f "$f"; done
```

خروجی `doctor` یک خط به هر بررسی است و کد خروجش از همان جدول مرحلهٔ ۱ می‌آید: `78`
یعنی یک تصمیم دانشجو نوشته نشده، `65` یعنی جدول ممیزی مشکل دارد، `67` یعنی فاز
پشتِ یک مرحله هنوز کدنویسی نشده. هیچ‌کدام «صفر» نیستند و هیچ‌کدام خطای غیرمنتظره نیست.

## فرمان‌ها

| فرمان | امروز چه می‌کند | چه زمانی کامل می‌شود |
|---|---|---|
| `python -m capstone doctor [--json]` | config، جدول ممیزی، `sql/`، فایل‌های تحویل و پل‌های زنجیره را می‌خواند و می‌گوید کدام بند باز است | همین حالا کار می‌کند |
| `python -m capstone audit --print-header` | سرستون دقیق `column_audit.csv` را چاپ می‌کند | همین حالا |
| `python -m capstone audit --template-out <path>` | برای هر ستونِ DDL یک سطر `unverified` می‌نویسد؛ هیچ ستونی با این فایل مجاز نمی‌شود | فاز ۲ |
| `python -m capstone audit --coverage --require-complete` | می‌گوید کدام ستون‌ها هنوز ممیزی نشده‌اند و با کد `65` می‌ایستد | فاز ۲ |
| `python -m capstone external [--fetch] [--report] [--load]` | لایهٔ منبع بیرونی، سه حالت جدا: بدون آرگومان فقط می‌گوید چه تنظیم شده و به دیتابیس نیازی ندارد؛ `--fetch` **یک** درخواست می‌زند (بی retry، بی pagination — سقف درخواستِ IBGE مستند نیست) و تعداد سطری که از قراردادِ پاسخ رد نشده‌اند را چاپ می‌کند؛ `--report` نرخ تطابق را از `capstone.v_external_match_rate` برمی‌دارد؛ `--load` اول می‌گیرد بعد می‌ریزد، و ریختن فقط از `capstone.upsert_external_municipio` است. منبعِ تنظیم‌نشده کد `67` می‌دهد نه traceback، نبودِ دیتابیس `78`، و مسیرِ شبکه اگر بست بود پیام می‌دهد نه استثنا | همین حالا؛ خودِ API از این محیط سنجیده نشده (`decisions.md` §۸) |
| `python -m capstone chain --dry-run [--json]` | پنج گام زنجیره را با دلیلِ بلوکه‌شدن چاپ می‌کند | فاز ۶ |
| `python -m capstone chain` | هر گام را با زمان‌گیر جدا اجرا می‌کند و manifest می‌نویسد | فاز ۶ |
| `python -m capstone predict --input <csv>` | schema فایل تازه را می‌سنجد؛ اگر درست بود، جلوی مدلِ نوشته‌نشده می‌ایستد | فاز ۶ |

دو اسکریپت هم همان کاری را می‌کنند که فاز ۲ و ۶ باید در خود SQL و در زنجیره ببندند:

```bash
python3 scripts/audit_columns.py --only shipping_limit | psql -f -     # سه شاهد بند 4.3
python3 scripts/run_chain.py --dry-run                                 # همان برنامه، JSON
```

## ساختار

```
stage-4/
├── src/capstone/     config، audit، timing، chain، predict، cli و doctor
├── tests/            ۱۱۱ آزمون آفلاین (stdlib؛ بدون pandas، بدون PostgreSQL)
├── sql/              قرارداد فایل‌های نسخه‌دار -- هنوز بدون .sql، با دلیل
├── scripts/          audit_columns.py و run_chain.py
├── config/           capstone.example.toml (قرارداد ردیابی‌شده) و capstone.toml (محلی)
├── data/             raw / curated / quarantine -- هیچ‌کدام در Git نمی‌آیند
└── docs/             ROADMAP، چک‌لیست، گزارش فاز ۰، شواهد
```

## چیزی که این اسکلت *انجام نمی‌دهد*

- هیچ عددی اندازه نمی‌گیرد. سقف‌ها (`45` دقیقه برای زنجیره، `500` میلی‌ثانیه برای کوئری
  ویژگی) از config خوانده می‌شوند و مقایسه‌شان در `capstone.features.check_query_cost`
  است؛ عدد اجرا هنوز وجود ندارد و در گزارش هم عددِ بی‌منبع نوشته نمی‌شود.
- هیچ ستونی را مجاز نمی‌داند. `capstone.audit.require_allowed` نگهبانِ پایتون است و
  `capstone.require_allowed(text[])` در `sql/002_time_guard.sql` همان پرسش را داخل
  دیتابیس می‌پرسد؛ جدول `capstone.column_audit` تا پر نشود خالی است و «خالی» یعنی «رد»،
  نه «قبول». ورودی هر دو `docs/evidence/column_audit.csv` است.
- تصمیم‌های دانشجو را *برایت* تعیین نمی‌کند. `config/capstone.example.toml` عمداً روی
  `"unset"` است و `load_config` تا آن‌ها نوشته نشوند خطا می‌دهد؛ این «قفل» نیست،
  همان قاعده‌ای است که در `04 §تعریف دقیق مسئله` به تو واگذار شده (بندهای 3.3 تا 3.6).
  چهار تصمیم در `decisions.md` گرفته و ثبت شده‌اند، و چون `config/capstone.toml` در Git
  نمی‌آید، مرجعِ ردیابی‌شده همان `decisions.md` است.
- منبع API را پیشنهاد نمی‌دهد؛ انتخاب با توست (بند 12.1). `external.name = "none"` هم یک
  پاسخِ کامل است — «منبع نمی‌گیریم و این را به‌عنوان یافته گزارش می‌کنیم» — و باید مثل
  بقیه در `decisions.md` نوشته شود؛ خالی‌گذاشتن یعنی هنوز تصمیم نگرفته‌ای.

## پیش‌نیازهایی که این اسکلت فرض می‌کند

`stage-1/src` باید روی مسیر باشد تا دو گام اول زنجیره (`ingest`، `load-database`)
از تفسیرِ تو import شوند؛ `data_factory` در `stage-1/pyproject.toml` تعریف شده و
مرحلهٔ ۱ همان‌جا نصب می‌شود. کف پایتون از `stage-1/pyproject.toml` میراث است
(`3.12`)، و `capstone doctor` نسخهٔ جاری را با همان عدد مقایسه می‌کند.
`requirements.txt` این مرحله هنوز نوشته نشده (بند 2.9 باز است)؛ آزمون‌های آفلاین
هیچ وابستگی بیرونی نمی‌خواهند، و برای همین همین حالا هم اجرا می‌شوند.
