# مرحلهٔ ۳ — مدل یادگیری ماشین (هزینهٔ تصمیم)

پوشهٔ تحویل مرحلهٔ ۳. کد سنگین در `src/credit/` است و نوت‌بوک فقط فراخوانی و چاپ می‌کند؛
دادهٔ خام هرگز در Git نمی‌آید و نوت‌بوک هرگز CSV نمی‌خواند.

## ساختار

```
solution.ipynb            تولیدشده با scripts/build_solution_notebook.py — فعلاً *اجراشده* است
                          (خروجی‌ها و execution_count از اجرای fixture؛ توضیح پایین)
report.md                 گزارش فارسی با جای‌نگهدار؛ سلول آخر نوت‌بوک عددگذاری و ممیزی می‌کند
requirements.txt          پین‌ها (Python >= 3.12، scikit-learn/joblib افزوده شد)
README.md                 همین فایل
src/credit/               قرارداد، منبع فایل، اعتبارسنجی، هزینه، فیچر، split، prep، مدل، ارزیابی
tests/                    ۲۷۷ آزمون آفلاین (unittest discover -s tests)
scripts/                  ژنراتور نوت‌بوک، runner اجرای نوت‌بوک، سازندهٔ fixture
docs/evidence/fixture/    شاهد یک اجرای کامل روی PostgreSQL واقعی (دادهٔ آزمایشی)
sql/                      010 اسکیما + 011 استیجینگ، و دو کوئری اندازه‌گیری در sql/queries
config/credit.example.toml  تنظیمات منبع فایل (کپی به config/credit.toml)
data/raw|curated          محل فایل Kaggle و خروجی‌های اجرا - هیچ‌کدام ردگیری نمی‌شوند
```

## وضعیت شواهد

| مورد | وضعیت |
|---|---|
| ریاضیات هزینه، آستانه، حساسیت به ماتریس | ✅ ۳۰ آزمون با عدد دستی، آفلاین `OK` |
| سازگاری منبع فایل با `fetch_all` و `RawPageStore` مرحلهٔ ۱ | ✅ روی ماشین واقعی مرحلهٔ ۱ آفلاین ثابت شد |
| قرارداد هدر/قرنطینه/دادهٔ تکراری | ✅ آفلاین `OK` |
| متن DDL و قول‌هایش (نام ستون‌ها، قیدهای نام‌دار، نبودِ قید اندازه‌گیری‌نشده) | ✅ آفلاین `OK` |
| wiring بارگذاری: config، جدول درج، قرنطینه، manifest، reset، مسیر خواندن | ✅ ۴۰ آزمونِ `test_credit_ingest.py`، آفلاین `OK` |
| بایت‌ها و نامِ فایل‌های مهاجرت قفل باشد (ledger فقط نامِ فایل را کلید می‌کند) | ✅ ۴ آزمونِ `test_credit_sql_checksums.py`؛ با یک بایتِ اضافه آزمون قرمز می‌شود (اینجا تأیید شد) |
| اجرای DDL روی PostgreSQL واقعی | ✅ PG 16.2ِ این جعبه، و PostgreSQLِ ماشینِ بازبین (`migrations=2` پس از `--reset`) — دو باگ واقعی همین‌جا پیدا شد (زیر) |
| بارگذاری یک فایل ۱۵۰٬۰۰۰ ردیفی و شمارش‌ها | ✅ fixture آزمایشی، ۱۶ صفحه، quarantine=0 — شاهدِ ماشین‌افزار، نه نتیجهٔ چالش |
| همان بارگذاری روی **فایلِ واقعیِ بازبین** | ✅ `loaded=150000`، `rejected=0`، `pages=16`، sha256 `1bd46da486a5` |
| split نشتی‌زدا، prep با ثابت‌های فقط-train، دو خانوادهٔ sklearn، باندل ذخیره‌شده | ✅ آزمون آفلاین با seed |
| آموزش مدل روی **دادهٔ واقعی**، آستانهٔ نهایی، holdout، Model Card | ✅ نوت‌بوک روی ماشینِ بازبین و روی فایلِ واقعی اجرا شد: `code_cells=24`، `errors=0`، `problems=0`، ۵۷۶٫۰ ثانیه از سقف ۲۰ دقیقه. عددهایش در `report.md`ِ *همان* درخت است، نه در این ریپو؛ پس این سطر «اجرا شد» را می‌گوید، نه «بازبینی شد» |
| اجرای `Run All` نوت‌بوک | ✅ ۲۴ سلول کد، بدون خطا، ۲۶۳٫۳ ثانیه (سقف ۲۰ دقیقه)، `problems=0` |
| بازتولیدِ شاهد بعد از `credit.db` | ✅ نوت‌بوک از اول اجرا شد و `credit_evidence.json` بیت‌به‌بیت همان ماند؛ فقط `loaded_at` جابه‌جا شد |
| مسیر فایل تازه (`credit.predict` روی باندل ذخیره‌شده، از view استیجینگ ۰۱۱) | ✅ ۵۰۰ ردیف، `rejected=0`، خروج ۰ (fixture) |
| `solution.ipynb` و `report.md` | ✅ نوشته شده؛ عددهای نتیجه در `report.md` جای‌نگهدارند |

## کوئری‌های اندازه‌گیری

```bash
psql "$DATABASE_URL" -f sql/queries/credit_sentinel_spikes.sql
psql "$DATABASE_URL" -f sql/queries/credit_missingness_parity.sql
```

خروجی این دو، شاهد بخش «اعداد sentinel» و «گم‌شدگی» در گزارش است؛ عددی را از حافظه ننویسید.

## اجرای ingest (مرحلهٔ ۳ روی بستر مرحلهٔ ۱ و ۲)

همهٔ فرمان‌ها از داخل پوشهٔ `stage-3` اجرا می‌شوند. هیچ‌کدام CSV را
مستقیم به تحلیل وارد نمی‌کنند: فایل از همان حلقهٔ صفحه‌بندی، قرارداد هدر، قرنطینه و
manifest مرحلهٔ ۱ رد می‌شود و بعد در `credit.application` نوشته می‌شود.

```bash
# یک venv برای هر سه مرحله کافی است؛ اگر یکی از قبل کار می‌کند، همان را بدهید
for c in "${VIRTUAL_ENV:+$VIRTUAL_ENV/bin/python}" ../.venv/bin/python \
         ../stage-1/.venv/bin/python .venv/bin/python; do
  [ -x "$c" ] && { P="$c"; break; }
done
echo "P=$P"                       # اگر خالی ماند، venv را فعال کنید؛ یا مسیرش را
                                  # مستقیم بدهید: P=../.venv/bin/python
export PYTHONPATH=src             # برای `python -m credit.load` لازم است: این مرحله
                                  # pyproject ندارد، پس `credit` فقط با مسیر import می‌شود
                                  # (نوت‌بوک و تست‌ها خودشان src را اضافه می‌کنند).
                                  # افزودن ../stage-1/src لازم نیست: `data-factory` با
                                  # pip install -e ثبت شده و import خالصش کار می‌کند
$P -m pip install -r requirements.txt          # اگر هنوز نصب نکرده‌اید (آینه: README ریشه)
$P -m pip check                                # سازگاریِ نسخه‌ها؛ باید بی‌صدا باشد
$P -c "from data_factory import config; print(config.__file__)"   # از کدام درخت بیاید مهم است
env -u PYTHONPATH $P -c "import data_factory; print(data_factory.__file__)"
# ^ بدون PYTHONPATH هم باید مسیر همین مخزن را بدهد؛ اگر `-e` را جایی دیگر ثبت کرده
#   باشید، `pip install -e .` از داخل stage-1 همان را بازنویسی می‌کند

cp data/raw/.gitkeep /dev/null 2>/dev/null; ls data/raw/cs-training.csv   # فایل را بگذارید
cp config/credit.example.toml config/credit.toml

# ۱) بدون دیتابیس: شمارش، اعتبارسنجی، قرنطینه
$P -m credit.load --dry-run --credit-config config/credit.toml

# ۲) مهاجرت‌های sql/ یعنی 010 و 011 (قفل advisory + ledger را خودش می‌گیرد)
#    اگر این دیتابیس هرگز این فایل‌ها را ندیده، قدمِ ۲ بی‌مهاجرت رد می‌شود؛ اگر
#    چیدمانِ قدیمیِ همین پروژه یا نسخه‌ای ویرایش‌شده‌شان را دیده، همین‌جا
#    «SQL migration checksum mismatch» می‌گیرید که درمانش قدمِ ۳ است، نه ویرایشِ فایل.
$P -m credit.load --migrations-only --credit-config config/credit.toml

# ۳) بارگذاری کامل
$P -m credit.load --reset --credit-config config/credit.toml

# ۴) تکرارپذیری: دو بار اجرای کامل؛ خطِ فرمان و خطِ digest باید بیت‌به‌بیت یکی باشند
load_and_digest() {
  $P -m credit.load --reset --credit-config config/credit.toml | tail -1
  $P -c "import json;d=json.load(open('data/curated/credit_run_manifest.json'));print(json.dumps(d['database']['digest'],sort_keys=True))"
}
load_and_digest > /tmp/load-a.txt
load_and_digest > /tmp/load-b.txt
diff /tmp/load-a.txt /tmp/load-b.txt && echo "parity ok"
# digest عمداً جدا از manifest چاپ می‌شود: started_at/finished_at در manifest هستند و
# بین دو اجرا فرق می‌کنند؛ چیزی که باید یکسان بماند aggregateهای همین ۱۲ کلیدند

# ۵) آزمون‌ها: ۲۷۷ آفلاین. واحدِ اجرا unittest است، نه pytest (مرحلهٔ ۱ عمداً
#    pytest را از requirements حذف کرده). برخلاف مرحلهٔ ۲ اینجا هیچ آزمونِ
#    DB-محورِ skip‌شونده‌ای وجود ندارد و TEST_DATABASE_URL چیزی را باز نمی‌کند؛
#    شواهدِ سمتِ دیتابیس همان قدم‌های ۲ تا ۴ بالایی‌اند، نه این بلوک:
#    این بلوک را بعد از `pip install -r requirements.txt` بگیرید؛ قبل از آن ~۲۱ آزمون
#    فقط با ModuleNotFoundError (numpy/pandas/sqlalchemy) قرمز می‌شود و آن خطای
#    کد نیست، ترتیبِ اجراست (روی ماشینِ بازبین دقیقاً همین افتاد).
$P -m unittest discover -s tests

# ۶) پیش‌بینی روی فایل تازه (بعد از اینکه notebook باندل را ساخت)
$P -m credit.predict --file data/raw/cs-validation.csv \
    --bundle data/curated/model --credit-config config/credit.toml

# ۷) نوت‌بوک با همان پایتون، تا هسته پین‌ها را داشته باشد
$P -m notebook solution.ipynb

# ۸) اگر فایل Kaggle دسترس نبود، دادهٔ آزمایشی با همان شکلِ فایل واقعی
$P scripts/make_fixture.py --rows 150000     # سرستونِ اول را خالی می‌گذارد، sha256 را چاپ می‌کند
```

> `python` خام را به کار نبرید: روی دبیان/اوبونتو اغلب این نام وجود ندارد و
> `pip install` بیرون از venv با `externally-managed-environment` (PEP 668) رد می‌شود.
> اگر `stage-2/.venv` را ساخته‌اید، همان هم کار می‌کند — فقط `$P` را به
> `.venv/bin/python` عوض کنید؛ `bootstrap_test_db.sh` در عوض `../stage-1/.venv/bin/python`
> را ترجیح می‌دهد.

### چهار قولی که این لایهٔ بارگذاری باید نگه دارد

1. **مهاجرت‌ها همان فایل‌های `sql/` هستند، نه یک کلید در config.** `apply_migrations`
   هر `*.sql`ِ آن دایرکتوری را به ترتیبِ نام به `apply_sql_migrations` می‌دهد، پس `011`
   دیگر منتظر نمی‌ماند کسی یادش بماند نامش را در config اضافه کند. `sql/queries/`
   زیرپوشه است، یعنی SQLِ اندازه‌گیری هرگز مهاجرت حساب نمی‌شود. کلیدِ قدیمیِ `migration`
   حالا **رد** می‌شود نه نادیده‌گرفته‌شدن؛ بی‌صدا بی‌اثر گذاشتنِ یک تنظیم، بدترین پاسخ است.
2. **reset باید ردیف ledger را هم پاک کند.** `reset_factory_schema` در مرحلهٔ ۲ این کار
   را نمی‌کند (فقط `DROP SCHEMA factory CASCADE`)؛ اگر دستی اسکیما را بیندازید، اجرای
   بعدی مهاجرتِ گمشده را «اعمال‌شده» می‌بیند و رد می‌کند. `credit.load.reset_credit`
   به‌ازای هر فایلِ این مرحله یک `DELETE` می‌زند (نه کلِ جدول، که تاریخچهٔ مرحلهٔ ۲ است)
   و آزمون متنش (`test_reset_drops_the_schema_and_every_ledger_row_this_stage_owns`)
   همان تعدادِ بیانیه‌ها و همان bind parameter را قفل می‌کند.
3. **ledger فقط نامِ فایل را کلید می‌کند، پس نامِ فایل‌های این مرحله تا ابد قفل است.**
   `public.data_factory_migrations` یک جدولِ مشترک برای همهٔ مرحله‌هاست و ستونِ کلیدش
   امروز تصادمی نیست: فایل‌های مرحلهٔ ۲ از `001` تا `009` هستند و مرحلهٔ ۱ هیچ
   فایلِ مهاجرتی در درختش ندارد. ولی هر درختِ دیگری که روزی فایلی هم‌نام
   `010_credit_schema.sql` اعمال کند، ردیفش همین‌جا می‌نشیند. `reset_factory_schema`
   دستِ خودِ ledger را نمی‌زند (و `reset_migration_history` هیچ‌جا صدا نمی‌شود)، پس تنها
   راهِ تمیزکردنِ ردیف‌های این مرحله همان `credit.load --reset` است - که دقیقاً دو ردیفِ
   همین دو فایل را پاک می‌کند و به تاریخچهٔ مرحلهٔ ۲ کاری ندارد. قفلِ بایت‌ها را
   `tests/test_credit_sql_checksums.py` نگه می‌دارد: اصلاحِ یک مهاجرتِ اعمال‌شده در جا
   ممنوع، فایلِ جدید (`012_…`) مجاز.
4. **`load-database` مرحلهٔ ۲ دست‌نخورده می‌ماند.** ترتیب `reset → base → load → sink →
   snapshots` در `tests/test_sql_migrations.py` با لیست دقیق فراخوانی‌ها قفل شده؛ برای
   همین بارگذاری اعتبار یک ** فرمان مستقل** است نه یک فلگ روی آن دستور.

### وضعیت بستر (ماشینِ تحویل؛ ماشینِ بازبین روی `7816890` و `62fcbd6`)

- مرحلهٔ ۱: `Ran 257 OK` — مرحلهٔ ۲: `Ran 46 OK (skipped=25)` — مرحلهٔ ۳ (این پوشه):
  `Ran 277 OK`. کنترل منفی (ماتریس هزینه ۱:۱، حذف shuffle از split، حذف
  `infer_objects` از مسیر خواندن): `FAILED (failures=12)`، یعنی آزمون‌ها بایت می‌کنند.
  هر سه عدد از یک extract تمیزِ `git archive HEAD` آمده‌اند، نه از پوشهٔ کاری.
- ماشینِ بازبین (`7816890`، Python 3.12.3) قفلِ exact-pin را از
  `mirror-pypi.runflare.com` نصب کرد، `pip check` گفت `No broken requirements
  found.` و `python -m unittest discover -s tests` داد `Ran 259 tests / OK` — یعنی
  «محیطِ pinned روی ≥۳٫۱۲ نصب و سبز می‌شود» دیگر ادعای یک‌طرفه نیست. آزمونِ بستنِ
  قفلِ وابستگی آنجا **واقعاً** اجرا شد (نه skip)، چون هر ۲۸ نامِ قفل به‌علاوهٔ
  `data-factory` با `-e` نصب بودند؛ در جعبهٔ من که `data-factory` ثبت‌نشده بود skip
  می‌کرد.
- بارگذاریِ فایلِ واقعی روی همان ماشین هم بسته شد: بعد از `62fcbd6`،
  `credit.load --reset` داد
  `file=cs-training.csv sha256=1bd46da486a5 loaded=150000 rejected=0 pages=16
  migrations=2`، و `python -m unittest discover -s tests` داد `Ran 266 tests in
  2.381s / OK` **بدونِ هیچ skip**. یعنی DDL روی PostgreSQLِ او اجرا شده، و قراردادِ
  هدر/صفحه‌بندی/قرنطینه روی ۱۵۰٬۰۰۰ ردیفِ واقعیِ Kaggle سرِ جای خودند؛ اینکه `rejected=0`
  است هم خطا نیست: کدهای ۹۶/۹۸ و سلول‌های خالی رد نمی‌شوند، کمبودشان اندازه‌گیری می‌شود.
- همان اجرا یک خطای واقعی هم داد: `--migrations-only` روی دیتابیسِ او با
  `SQL migration checksum mismatch` ایستاد. ردیفِ ledgerِ او برای
  `010_credit_schema.sql` می‌گفت `2cf265af…`؛ هیچ نسخه‌ای که این ریپو از آن فایل
  داشته (`aec9009c…` پیش از اصلاحِ FILTER در `3d232eb`، `28926a70…` پس از آن) به
  آن هاش نمی‌رسد — پس آن ردیف از نسخه‌ای **دیگر** از همان فایل آمده، نه از بایت‌های
  ما. خودِ حارس درست کار کرد؛ کاری که ما نکردیم این بود که `010` را پس از اعمال
  ویرایش کردیم (`ac34967`) و هیچ مسیرِ پیشروی نگذاشتیم. از آن به بعد خطا
  توضیحِ درمان را هم چاپ می‌کند، و `tests/test_credit_sql_checksums.py` نگهش
  می‌دارد. بازتولید و درمان روی PostgreSQL 16.2 همین‌جا تأیید شد: با ردیفِ
  دست‌کاری‌شده همان DataError (با همان متنِ اخطار)، و `--reset` داد
  `migrations=2` و `loaded=150000`.
  آنچه هنوز اجرا **نشده**: سلول‌های مدل/آستانه/holdout/عدالتِ نوت‌بوک روی فایلِ
  واقعی؛ مسیرِ بارگذاری ولی روی همان فایل سنجیده شد (بالا).
- اولین تماس با فایلِ واقعی اما انجام شد، روی ماشینِ تحویل‌گیرنده و با `cs-training.csv`
  خودِ Kaggle:
  `file=cs-training.csv rows=150000 valid=150000 quarantined=0 ratio=0.000000 reasons={}`.
  قراردادِ هدر، صفحه‌بندیِ حافظه‌مند، طبقه‌بندیِ ردیف‌ها و قرنطینه روی ۱۵۰٬۰۰۰ ردیفِ واقعی
  همان رفتاری را دادند که آفلاین ادعا می‌شد. `quarantined=0` هم انتظارِ همین است: کدهای
  ۹۶/۹۸ و سلول‌های خالی رد نمی‌شوند، کمبودشان اندازه‌گیری می‌شود. دیتابیس هنوز لمس نشده.
- قرارداد هدر **دو بار** از روی متن عوض شد و بارِ دوم را هم خودِ فایل اصلاح کرد: اول
  `Give.Me.Some.Credits` (عنوانِ مسابقه) نوشته بودم، بعد فرض کردم Kaggle با `Id` شروع
  می‌شود؛ سرستونِ واقعیِ `cs-training.csv` ولی سلولِ اولش **خالی** است، چون آن ستون
  ایندکسِ ردیفِ بی‌نام است. حالا `ID_COLUMN` همان `Id` می‌ماند (کلیدِ داخلیِ ردیف‌ها) و
  `ID_ALIASES` سه املای `Id`، `""` و `Give.Me.Some.Credits` را فقط در جایگاهِ صفر می‌پذیرد؛
  خالی‌بودنِ هر جای دیگر یعنی drift. این خطا فقط با پیامِ درستِ همان آزمون حل شد:
  `SchemaDriftError` مقدارِ expected/actual را در context چاپ می‌کند، پس به‌جای حدسِ
  سوم، سرستونِ واقعیِ فایلِ شما را دیدم.
- `--dry-run` هیچ آزمونِ CLI نداشت و همین که یک بلوک کپی‌شونده در گفتگو خطِ
  `cp config/credit.example.toml config/credit.toml` را جا انداخت، خطا را فقط به شکل
  `Config file not found: config/credit.toml context={}` برگرداند — بی‌اخبارکننده.
  حالا پیامِ نبودِ فایل، خودِ فایلِ مثال را نام می‌برد (فقط اگر واقعاً کنارش باشد) و
  دو آزمونِ `DryRunCliTests` همان فرمان را از اول تا آخر اجرا می‌کنند: شمارشِ درستِ
  فایل، و اینکه dry-run manifest نمی‌نویسد. `context={}` خالی هم چاپ نمی‌شود.
- درست پیش از اولین اجرای دیتابیس، سیم‌کشیِ خودم یک باگِ بی‌صدا داشت: config نامِ *یک*
  فایل مهاجرت را نگه می‌داشت (`migration = "010_…"`) و `apply_migrations` همان یکی را
  فیلتر می‌کرد، پس `011_credit_staging.sql` را هیچ مسیری اعمال نمی‌کرد و view استیجینگی
  که `credit.predict` روی فایلِ تازه از آن می‌خواند، روی هیچ دیتابیسی ساخته نمی‌شد.
  ۲۳۸ آزمونِ سبزِ آن روز این را نگرفته بودند، چون هیچ‌کدام نمی‌پرسید «آیا هر فایلِ
  `sql/` اعمال می‌شود». حالا لیست، همان دایرکتوری است و سه نگهبانِ جدید دارد؛ با کنترلِ
  منفی (برگرداندنِ قانونِ تک‌فایلی) پنج آزمون قرمز می‌شوند: `Ran 15 … FAILED (failures=4,
  errors=1)`. اگر `config/credit.toml` را از مثالِ قبلی کپی کرده‌اید یک بار دیگر
  کپی‌اش کنید: خطِ `migration` حذف شده و فایلِ قدیمی با پیامِ صریح رد می‌شود.
- اجرای واقعی (نه dry-run) سرِ خودِ نوت‌بوک سه باگ دیگر را بیرون ریخت، هیچ‌کدام با
  آفلاین دیدنی نبودند: ۱) `DataFrame.to_markdown` به `tabulate` نیاز دارد که در قفل
  نیست — با `credit.tables.md_table` عوض شد و یک آزمون حالا هر `to_markdown/to_excel/
  to_parquet/read_html` را در کد تحویلی منع می‌کند؛ ۲) کوئریِ شمارش sentinel از view
  می‌خواند، همان‌جا که سیاستِ خودمان کدها را NULL کرده — صفرِ حاصل از view مدرک نیست،
  پس شمارش به جدول خام رفت و جفت‌هایِ کاوش‌شده‌ی بی‌مورد هم با صفر ماندن سطر می‌مانند
  (برابری دو اندازه‌گیری حالا سطر‌به‌سطر چک می‌شود نه با `len`)؛ ۳) `PrepStats.feature_names()`
  ماتریس را از ده ستون خامِ فایل می‌ساخت نه از ستون‌هایی که `fit` گرفته بود — یعنی مدل روی
  ۱۰ فیچر تمرین می‌کرد و ۱۶ تا چاپ می‌شد، و آبلیشن یک ماتریس را دو بار می‌سنجید و «هیچ» را
  حذف می‌کرد. اصلاحش دو آزمون جدید هم گرفت. ضمناً همان اجرا نشان داد بلوکِ «حذف فیچرهای
  بی‌بها» *بعد از* سلولِ آموزش مدل نشسته بود (NameError در دقیقهٔ هفتم)؛ برای همین
  `scripts/build_solution_notebook.py` حالا scopes را هم چک می‌کند: هیچ سلولی نمی‌تواند
  نامی را بخواند که سلولِ قبلی نساخته باشد، و یک آزمونِ کنترل‌منفی ثابت می‌کند این چک
  می‌تواند قرمز شود. عددها: `pages=16`، `rows_loaded=150000`، `rejected=0`، `migrations=2`
  و در اجرای دوم `0`، `elapsed=259.1s`، `problems=0`.
- `ruff format --check` روی این پوشه تمیز است (۳۰ فایل، همه فرمت‌شده)؛ روی `stage-1`
  در همان commit، ۵۹ فایل از ۶۹ فایل فرمت‌نشده‌اند و در `stage-2` ۴ از ۱۲ —
  convention معرفی‌شده در خودشان اجرا نشده، و این بدهی مرحلهٔ ۲ است نه مرحلهٔ ۳.
- متن هر چالش حالا داخل پوشهٔ خودش است (`files-given/` در هر مرحله) و ردگیری
  می‌شود؛ برنچ `ml-challenge-01` همان فایل‌ها را در `term-3/files-given` داشت.

## اجرای نوت‌بوک (Run All، اما قابل بازرسی)

«Restart & Run All» یک ادعا دربارهٔ یک فایل است، پس برنامه هم می‌تواند آن را چک کند.
`scripts/run_notebook.py` همان کاری را می‌کند که داور می‌کند و بعد فایل را می‌سنجد:

```bash
# اجرا از داخل stage-3، با همان پایتونِ runbook (هستهٔ kernel از همین venv برمی‌خیزد)
# اگر `data_factory` را با `-e ../stage-1` نصب نکرده باشید، به PYTHONPATH اضافه‌اش کنید
read -s OLIST_DB_PASSWORD && export OLIST_DB_PASSWORD    # همان چیزی که `credit.load` هم
#    می‌خواهد. kernelِ نوت‌بوک محیطِ همان شل را به ارث می‌برد، پس این خط باید *پیش از*
#    Run All زده شود؛ اگر Jupyter را از میانبرِ دسکتاپ باز کنید، آن شل هیچ‌وقت export
#    نداشته و اتصال رد می‌شود. یک ~/.pgpass برای همین user هم کار می‌کند، و هیچ رمزی در
#    فایل‌های این ریپو نوشته نمی‌شود.
PYTHONPATH=src $P scripts/run_notebook.py --out /tmp/solution.executed.ipynb
PYTHONPATH=src $P scripts/run_notebook.py --check --notebook /tmp/solution.executed.ipynb
```

> خودِ این ابزار به `nbclient` و `nbformat` نیاز دارد که **عمداً** در `requirements.txt`
> نیستند: نه بسته، نه نوت‌بوک، نه آزمون‌ها چیزی از آن‌ها import می‌کند (import داخل تابع
> است و یک آزمون همین را نگهبانی می‌کند)، پس محیطی که فقط از قفل ساخته شده هم می‌تواند
> نوت‌بوک را با Restart & Run All در UI اجرا کند. اگر خواستید بازرسی را از خط فرمان بگیرید:
> `$P -m pip install nbclient nbformat` (هستهٔ `python3` را هم همان `ipykernel` می‌دهد که
> Jupyter نصب می‌کند).

خط پایانیِ هر دو شکل‌اش یک الگوی ثابت دارد (عددهایش از اجرای همین جعبه است؛ روی ماشینِ
شما `elapsed` فرق می‌کند و همان انتظار است — بقیه نه):

```
executed /tmp/solution.executed.ipynb code_cells=24 errors=0 elapsed=259.1s budget=20min problems=0
checked /tmp/solution.executed.ipynb code_cells=24 errors=0 problems=0
```

دو چیز که در این مسیر دیده می‌شوند و باید درست خوانده شوند:

- `[IPKernelApp] WARNING | Kernel is running over TCP without encryption` را ipykernel 7 روی
  localhost چاپ می‌کند و خطا نیست؛ روی ماشینِ بازبین و روی این جعبه هم یکی بود، و هسته فقط
  روی `127.0.0.1` گوش می‌دهد.
- اگر رمزِ دیتابیس در محیط نباشد، حالا سلولِ ۱ با `ConfigError` می‌ایستد و همان‌جا خطِ
  `read -s … && export …` را می‌گوید. دلیلش `credit.db.engine_for` است: پیش از تحویلِ engine
  یک `SELECT 1` می‌زند تا «اتصال برقرار است» و «تحلیل شروع شده» دو چیزِ یکی باشند. متنِ
  راننده پس از پاک‌سازیِ هر شکلِ `password=` و `://user:pass@` چاپ می‌شود، پس رمز حتی در
  traceback هم نمی‌افتد - یک آزمون دقیقاً همین را می‌سنجد، نه اینکه فرضش کنیم. دو *درمانِ*
  متفاوت هم به‌همین دلیل است: اگر حرفِ سرور رمز باشد، خطِ `read -s` پیشنهاد می‌شود؛ اگر
  چیزی جز رمز باشد (`Connection refused`، یا `role "x" does not exist`)، `fix` سراغِ
  `database.url` می‌رود و صراحتاً می‌گوید هیچ statement‌ای اجرا نشده. به کسی که رمز را
  export کرده، «رمزت کم است» نمی‌گوییم، و به کسی که سرورش جواب داده، «سرور پایین است» هم
  نمی‌گوییم.

**بعد از `Run All` چه چیزی در درخت عوض می‌شود.** سلولِ آخر `report.md` را از evidence پر
می‌کند، پس `git status` یک `M stage-3/report.md` می‌دهد و این نتیجهٔ طراحی است، نه آلودگی.
`run_notebook.py` هم نوت‌بوکِ ردگیری‌شده را دست نمی‌زند (خروجی به `--out` می‌رود)؛ اگر اما
نوت‌بوک را در Jupyter باز کنید و ذخیره کنید، `solution.ipynb` محلی‌تان عوض می‌شود و `git pull`
بعدی به‌خاطر همین می‌ایستد — قبل از pull یا commitش کنید یا `git checkout -- stage-3/solution.ipynb`.
آزمون‌ها بعد از پرشدنِ report هم سبز می‌مانند: `test_the_shipped_report_has_only_well_formed_slots`
دو حالت دارد — در قالب، هیچ عددِ نتیجه‌ای مجاز نیست؛ در گزارشِ پرشده هر عدد باید در
`data/curated/credit_evidence.json`ِ همان اجرا پیدا شود، و نبودنِ آن فایل **خطا** است نه skip. اگر `data/curated/` خالی باشد — که در یک cloneِ تازه
همین‌طور است، چون آن فایل را فقط اجرایِ نوت‌بوک می‌سازد — آزمون همان payload را از
`docs/evidence/real/` می‌خواند؛ پس فایل باید در یکی از این دو باشد، و نبودنش در هیچ‌کدام
خطاست.
اینکه حالتِ دوم بایت می‌کند با یک عددِ ساختگیِ ۴۷۱۱٫۲۳ در متنِ پرشده تأیید شد.

**اگر `report.md` دوباره خالی شده ولی اجرا در تاریخچه هست** (مثلاً `git checkout --` قبل از
pull آن را برگردانده)، لازم نیست ده دقیقه صبر کنید؛ همان کاری را بخواهید که سلولِ آخر می‌کند:

```bash
PYTHONPATH=src $P -c "
import json
from pathlib import Path
from credit import report
status = report.apply_to_file(
    Path('report.md'),
    json.loads(Path('data/curated/credit_evidence.json').read_text(encoding='utf-8')),
)
print(status)
"
```

`unresolved` باید `0` باشد. و این **بازچسباندنِ عدد است، نه بازتولیدش**: عددها از همان اجرایِ
قبلی می‌آیند، پس اگر کدِ تحلیل از آن وقت عوض شده باشد، این اعداد مالِ کدِ جدید نیستند و تنها
راهش اجرایِ دوباره است. برای اینکه بدانید «عوض‌شدنِ کد» چه بهایی دارد: بین `5fc6b90` و
`b9248bb` فقط ساختِ engine و مستندات تغییر کرد و روی دادهٔ آزمایشی، diffِ بایتیِ
`credit_evidence.json` فقط `loaded_at` را نشان داد. این روی دادهٔ آزمایشی سنجیده شده، نه روی
دادهٔ شما. شواهدِ اجرایِ واقعی را در `docs/evidence/real/` بگذارید؛ همان پوشه می‌نویسد چه
چیزهایی هرگز نباید به ریپو اضافه شوند. و از آن لحظه یک آزمون هم سرِ جایش می‌آید: `ArchivedEvidenceConsistencyTests` آن جفت را با `report.md` می‌سنجد (اسلاتی نمانده باشد، عددها در evidence پیدا شوند، و manifest با evidence دربارهٔ یک فایل حرف بزنند). اگر پوشه خالی باشد آزمون skip می‌شود و همان را بلند می‌گوید — پس «سبز بودنِ مجموعه» با «شواهد گذاشته شدن» یکی نیست.

بازرسی‌ها: هر سلول کد اجرا شده باشد، شماره‌گذاری ۱..N پیوسته باشد، هیچ خروجی `error`
نباشد، و هیچ مسیر مطلق در متن سلول‌ها نماند. `--budget-minutes` هم سقف ۲۰ دقیقه را
خودش می‌سنجد، نه اینکه ما بگوییم زیرش است.

`solution.ipynb` تحویل‌شده **اجراشده** است: خروجی‌ها از یک اجرای کامل روی PostgreSQL
16.2 با دادهٔ fixture آزمایشی‌اند (نه فایل Kaggle) و همان‌طور که `docs/evidence/fixture/`
می‌گوید. برای دادهٔ واقعی، Run All را خودتان اجرا کنید؛ خروجی‌های تازه جای همان‌ها را
می‌گیرند. ژنراتور همچنان منبعِ متنِ سلول‌هاست و `--check` دو حالت را می‌پذیرد: فایل
تمیز باید بیت‌به‌بیت خروجی ژنراتور باشد، فایل اجراشده باید در *متن سلول‌ها* کلمه‌به‌کلمه
برابر باشد و جز آن (خروجی، شمارهٔ اجرا) چیزی از خودش اضافه نکرده باشد.

## تولید دوبارهٔ نوت‌بوک

```bash
../stage-1/.venv/bin/python scripts/build_solution_notebook.py            # نوشتن solution.ipynb
../stage-1/.venv/bin/python scripts/build_solution_notebook.py --check    # اگر فایل با ژنراتور فرق داشت، خطا
```

نوت‌بوک یک اثرِ تولیدشده است: ترتیب سلول‌ها، نبودِ `read_csv`، تک‌seed بودن، فارسی‌بودن
متن‌ها و **وجود هر `module.attr` که سلولی صدا می‌زند** همگی در
`tests/test_credit_notebook.py` چک می‌شوند (چک آخر با خواندن AST ماژول‌ها کار می‌کند،
پس به دیتابیس یا نصب مرحلهٔ ۱ نیاز ندارد).

## آرتیفکت‌هایی که اجرا می‌سازد

| فایل | چه هست |
|---|---|
| `data/curated/model/model.joblib` | estimator برازش‌شده روی train+validation |
| `data/curated/model/prep_stats.json` | ثابت‌های برازش‌شدهٔ prep (ترتیب کلیدها = ترتیب ستون‌ها) |
| `data/curated/model/model_card.json` | آستانه، نام فیچرها، ماتریس هزینه، نسخهٔ کد |
| `data/curated/credit_evidence.json` | هر عددی که `report.md` مجاز به نقل آن است |
| `data/curated/credit_run_manifest.json` | manifest بارگذاری: sha256، صفحه‌ها، قرنطینه، digest |
| `solution.executed.ipynb` (مقصد `--out`) | همان نوت‌بوک با خروجی‌ها؛ چیزی که `--check` رویش اجرا می‌شود |
| `docs/evidence/fixture/` | شاهدِ بایگانی‌شدهٔ یک اجرای کامل: evidence، manifest، `run.txt`، `report-slots.txt` |

