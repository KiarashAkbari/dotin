# گزارش تغییرات: اصلاح باگ snapshot و به‌روزرسانی AGENTS.md / ROADMAP.md

> مخاطب: دستیار (agent) پروژهٔ data-factory-v2 / dotin. این گزارش فقط آنچه در همین گفتگو
> اجرا یا مشاهده شده را ثبت می‌کند. هر چه اجرا نشده «بررسی نشده» است.
>
> این نسخه، متن اصلاح‌شدهٔ گزارش دستیارِ `8843439` است؛ §۱.۱ و §۵.۲ کهنه بودند (پچ دیگر در
> `data-factory-v2` اعمال شده و push شده است) و بند P2 از حالت «پیش‌بینی درآمد» به
> «نامعلوم» برگشت (در دور بعد با لاگ p2.log تأییدشده؛ ادامهٔ این سند ببین). ارجاع‌ها به
> شمارهٔ خط و commit همان‌طور که در مخزن بررسی شد نوشته شده.

## ۱. خلاصه

1. **باگ:** بعد از `load-database --reset` هر چهار materialized view
   (`mv_q1_monthly_category_sales`, `mv_q3_delay_satisfaction`, `mv_q4_seller_dashboard`,
   `mv_q5_payment_distribution`) با `ispopulated = t` ولی `rows = 0` می‌ماندند. علت:
   مهاجرت‌های ۰۰۵ تا ۰۰۸ روی engine جدا از `lock_connection` اجرا می‌شوند و تنها
   `refresh_olist_snapshots` قبل از ساخته‌شدن viewها صدا زده می‌شد.
   **وضعیت: اصلاح در هر دو مخزن اعمال شده** — در `data-factory-v2` commit `fa812b4`
   («fix: refresh the snapshot views after the last commit of every load path»، ۲۳ خط اضافه
   بر `stage-1/src/data_factory/cli.py`). در `dotin` هم هر دو مرحله merge شده‌اند: اصلاح کد با
   PR #25 (commit `85e3f09`، commit ادغام `d526be2`) و نسخهٔ سخت‌تر تست با PR #26 (شاخهٔ
   `stage-4/test-hardening`، commit `56f0705`، commit ادغام `d8651dc` — نوک `main` همان است).
   «merge نشده / PR ساخته نشده» در نسخهٔ اول این گزارش مربوط به لحظهٔ نگارشش بود و حالا درست
   نیست؛ وضعیت دو PR با `gh pr view` و `gh api …/branches/main` خوانده شد (هر دو `MERGED`).
2. **اصلاح:** refresh پس از آخرین commit هر مسیر بارگذاری، با `commit()` خودش داخل
   `_refresh_after_commit` (خط ۱۸۹؛ refresh خط ۲۰۳ و commit خط ۲۰۴) از سه نقطه صدا زده
   می‌شود: ۲۶۳ / ۲۹۹ / ۳۲۰، بلافاصله بعد از `lock_connection.commit()` در ۲۶۲ / ۲۹۸ / ۳۱۹
   (فایل ۳۶۶ خط). این قید «خودش commit
   کند» از اجرای واقعی آمد: نسخهٔ بدون commitِ همان refresh روی PostgreSQL واقعی **مجدداً
   شکست خورد** (تراکنشِ بازِ `finally` rollback می‌شود).
3. **دو فایل راهنما (`AGENTS.md`، `ROADMAP.md`) ویرایش شدند و این بار commit‌شده‌اند:**
   `23df624` مستندات و شواهد را اضافه کرد و `a9901cb` (و چهار commit قبلش) وضعیت را
   به‌روزرسانی کرد؛ هر شش روی `origin/arena/9448ddf0-data-factory-v2` push شده‌اند. ادعای
   «هنوز در هیچ مخزنی commit نشده‌اند» مربوط به لحظهٔ نگارش نسخهٔ اول این گزارش بود و حالا
   درست نیست. ویرایش‌های این دور (بند ۴ زیر) تا commit `597ebd8` ثبت شده‌اند؛ commit‌های
   بعدی در `git log --oneline` همین شاخه‌اند.
4. **ویرایش‌های این دور:** بند وضعیت §۱۴ `AGENTS.md` و بند ۱ §۹ `ROADMAP.md` به
   «تأییدشده و اصلاح‌شده در کد (`fa812b4`)؛ بسته‌شدن روی `olist_db` باز» عوض شد؛ بند ۴ فاز ۰
   `ROADMAP.md` هم اکنون می‌گوید لاگ خام محلی است و اعداد در گزارش؛ `P2` در سه جا «نامعلوم» شد
   (در دور بعد با لاگ p2.log تأییدشده؛ ادامهٔ این سند ببین) و پس از رسیدن `p1.log`/`p2.log` به
   «تأییدشده با لاگ (گزارش دانشجو؛ لاگ خام محلی)» به‌روز شد؛
   `psycopg2-binary` به‌عنوان **انحراف محیط** علامت خورد؛ تست `stage-1` به ۸ تست رسید و
   دو لاگ قرمز/سبزِ موجود در `stage-4/docs/evidence/` با کد امروز بازتولید شدند.

## ۲. شواهد اجرا (مشاهده‌شده)

- پایگاه آزمایشی: `olist_phase0_test` — **نه** `olist_db`. `--reset` کل schema را می‌اندازد،
  پس اجرای آن روی `olist_db` انجام نشده و نمی‌شود؛ هر ادعایی که این دو را یکی بگیرد غلط است.
- اجرای دوم با `--reset --skip-factory-sink` کامل شد؛ جدول‌ها بارگذاری شدند
  (۷۱، ۹۹٬۴۴۱، ۳٬۰۹۵، ۳۲٬۹۵۱، ۷۳۸٬۳۳۲، ۹۹٬۴۴۱، ۱۱۲٬۶۵۰، ۱۰۳٬۸۸۶، ۹۹٬۲۲۴ ردیف) ولی هر چهار
  view صفر ردیف داشتند. این پیش‌بینی **P1** را تأیید کرد.
- بعد از پچ، همان دو اجرای پشت‌سرهم هر چهار view را پر نگه داشتند:
  `1282 / 4 / 3053 / 27`.
- تست‌ها روی PostgreSQL 16 دانشجو: `Ran 7 tests ... OK` (۲٫۹۹ ثانیه) با `TEST_DATABASE_URL`
  روی `olist_phase0_test`، بدون skip. امروز همان فایل ۸ تست است (بند ۳.۲) و در سندباکس
  `Ran 8 tests OK` شد.
- **اجرای بدون `--reset` (P2): تأییدشده با لاگ** (گزارش دانشجو؛ لاگ خام محلی). `p2.log` آن را
  روی `olist_phase0_test` با `sql_migrations=0`، `sql_snapshots=0` و هر چهار view پر
  (`1282 / 4 / 3053 / 27`) نشان می‌دهد؛ sha256 دو لاگ در `phase0-baseline.txt` بند A.2 و
  `PHASE0_REPORT.md` §۳ ثبت شده، خود فایل‌ها در مخزن نیستند. روایت مقابل — همان اجرا تمام
  نشد (`DELETE FROM olist.customers` بیش از ۲:۴۱ دقیقه، بدون `wait_event`؛ reported, not
  measured) — به‌عنوان گزارش معارضِ سازگارنشده نگه داشته می‌شود، نه رد نه تأیید؛ علت کندی هم
  فقط فرضیه است (احتمالاً `ON DELETE CASCADE` روی فرزندان). فرضیهٔ scan کامل با
  `EXPLAIN` سنجیده و رد شده؛ علت کندی همچنان بی‌اثبات است. اینکه این اجرا همان
  stateِ اجرای `--reset` بوده، ادعا نمی‌شود.
- زمان `2m47s` برای دو اجرا به‌همراه کوئریِ شمارش، عددِ گزارش‌شدهٔ خود دانشجو است؛ تجزیهٔ
  زمان هر یک در مخزن نیست و در سندباکس بازسنجیده نشده (بررسی نشده).

## ۳. تغییرات کد و تست

| فایل | تغییر |
|---|---|
| `stage-1/src/data_factory/cli.py` | +۲۳ خط در `fa812b4`؛ `_refresh_after_commit` بعد از آخرین commit هر سه مسیر خروج |
| `stage-1/tests/test_load_database_snapshots.py` | فایل تست (این دور ۴۸۹ خط، ۸ تست) |
| `term-3/answers/stage-1-kiarash-akbari/…` در `dotin` | `85e3f09`: همان پچ + بلوک `load-credit` که **مربوط به این مخزن نیست** و کپی نشد |

1. باگ جانبی تست که حین کار پیدا و رفع شد: مسیر مهاجرت‌های مرحلهٔ ۲ ثابت `stage-2/sql`
   فرض می‌شد؛ در `dotin` پوشه `stage-2-kiarash-akbari` است. نسخهٔ `dotin` glob
   `stage-2*/sql` را با `skipTest` (اگر هیچ) انتخاب کرد. **در این مخزن همان glob اعمال شد،
   ولی با `fail` به‌جای `skip`**: صفر یا دو تطابق پیام خطای روشن با شمارش می‌گیرد
   (`expected exactly one stage-2*/sql directory … found N`)، چون یک fixture ناتوان نباید
   به‌شکل «چیزی برای آزمودن نیست» از بین برود. `found[0]` به‌سلیقه‌ی نویسنده انتخاب نمی‌شود.
2. تست هشتم (`test_the_stage2_sql_directory_requirement_fails_rather_than_skips`) همان قاعده
   را بدون پایگاه می‌آزماید: با صفر تطابق و با دو تطابق باید `fail` شود، و روی این درخت
   دقیقاً یک تطابق پیدا می‌شود.
3. **قرمز/سبز با کد امروز (سندباکس، PostgreSQL 16.2 با `pgserver`):** قبل از پچ
   `Ran 8 … FAILED (failures=5)` و هر چهار MV `ispopulated=t, rows=0`؛ بعد از پچ
   `Ran 8 … OK` و هر چهار MV `rows=1`. لاگ‌ها:
   `stage-4/docs/evidence/phase0-red-run.txt` و `phase0-green-run.txt` (این دور بازتولید
   شدند، پس شماره‌هایشان با ۸ تست می‌خواند).
4. مجموعهٔ کامل `stage-1` در سندباکس آفلاین: `Ran 262` با تنها خطای `test_analysis` و دو
   skip از کلاس پایگاه‌دار؛ روی PostgreSQL واقعی همان مجموعه `FAILED (failures=5, errors=1)`
   در حالت قرمز و `errors=1` در حالت سبز. **این خطا محیطی است و به پچ ربط ندارد** — ثابت شد
   با اجرای کپیِ تمیزِ HEAD (`git archive a9901cb`) روی همان interpreter:
   `Ran 261 … FAILED (errors=1)` بدون پچ در برابر `Ran 262 … FAILED (errors=1)` با پچ. علت
   دقیق: `data_factory/analysis.py` هم `pandas` را import می‌کند هم `matplotlib`؛ با نصب آن
   دو، همان دو درخت `Ran 264 … OK (skipped=2)` و `Ran 265 … OK (skipped=2)` شدند — یعنی
   ویرایش تست دقیقاً یک تست اضافه می‌کند و هیچ تست دیگری را نمی‌شکند.
5. `ruff check` روی فایل تست ویرایش‌شده پاس است. دو `F401` در `cli.py`
   (`MIGRATION_LOCK_KEY`, `sql_migration_files`) از قبل در HEAD بودند و دست‌نخورده ماندند؛
   حذفشان بی‌ربط به این پچ است.
6. `ML-CHALLENGE/` ردیابی‌نشده است و وارد commit نشد.

## ۴. تغییرات فایل‌های Markdown

**ROADMAP.md**

1. فاز ۰ بند ۳: اجرای `load-database --reset` روی پایگاه آزمایشی یک‌بارمصرف
   (`olist_phase0_test`)، نه `olist_db` اصلی، چون `--reset` همهٔ داده‌ها را پاک می‌کند.
2. فاز ۰ بند ۴: `phase0-baseline.txt` **نوشته شده** است و لاگ خام محلی می‌ماند؛ اعداد در
   گزارش‌اند. صریح گفته می‌شود اندازه‌گیری روی `olist_phase0_test` بوده و عدد `1:16.24` در
   هیچ لاگی به پایگاه مشخصی گره نخورده، پس دربارهٔ آن «بررسی نشده» می‌مانیم.
3. یادداشت‌های سازگاری بند ۱: وضعیت = «تأییدشده و اصلاح‌شده در کد (`fa812b4`)؛
   بسته‌شدن روی `olist_db` باز؛ P2 نامعلوم» (در دور بعد با لاگ p2.log تأییدشده؛ ادامهٔ این
   سند ببین).

**AGENTS.md** (یادداشت پایانی بخش ۱۴)

1. همان وضعیت؛ در نسخهٔ اول «`dotin` merge نشده و ساختن PR با دانشجوست» نوشته شد و در این
   دور به «هر دو PR merge شده‌اند» اصلاح شد (#25 با `d526be2` برای کد، #26 با `d8651dc` برای
   نسخهٔ سخت‌تر تست).
2. قید اینکه `--reset` فقط روی پایگاه آزمایشی اجرا می‌شود، و اینکه `Ran 7` شمارهٔ تست‌های
   امروز نیست.

**PHASE0_REPORT.md / evidence**

1. §۳: پ۱ تأییدشده، **پ۲ نامعلوم** (در دور بعد با لاگ p2.log تأییدشده؛ ادامهٔ این سند ببین)
   (دو نقلِ متضاد، بدون لاگ)، پ۳ بی‌موضوع؛ جملهٔ
   «پ۱ و پ۲ دقیقاً همان‌طور که پیش‌بینی شده بود درآمدند» حذف شد.
2. §۱ ردیف ۷ و §۸ بند ۴: ادعای تساوی شمارش P1/P2 حذف شد؛ `psycopg2-binary 2.9.13` «انحراف
   محیط» نامیده شد (قفل‌های مخزن `psycopg==3.3.6` را پین می‌کنند؛ تست هیچ درایوری را تحمیل
   نمی‌کند و درایور از خود `TEST_DATABASE_URL` می‌آید).
3. `stage-4/docs/evidence/README.md`: دستور بازتولید با نام پایگاه درست
   (`olist_phase0_test`)، توضیح «لاگ خام محلی است»، و فهرست بازِ فاز ۰ که دیگر به جملهٔ
   «بستن این پرونده با اجرای خود دانشجو» ارجاع نمی‌دهد — آن جمله در ROADMAP نمانده بود.
4. `phase0-baseline.txt`: بند A.2 (P2) با علامت `UNVERIFIED / نامعلوم` (در دور بعد با لاگ
   p2.log تأییدشده؛ ادامهٔ این سند ببین)؛ جملهٔ
   «prediction P2 held» بیرون رفت؛ بند A و بند C صریح می‌کنند هیچ عددی روی `olist_db`
   سنجیده نشده و درخواستهٔ gate هم اجرای روی `olist_db` نیست.

اندازه‌ها در نسخهٔ اول این گزارش: AGENTS.md ۷۳٬۱۹۳ بایت، ROADMAP.md ۲۸٬۱۴۰ بایت. ویرایش‌ها
با جایگزینی دقیق انجام شد و هر الگو دقیقاً یک‌بار پیدا شد. اندازهٔ AGENTS.md پس از ویرایش
این دور ۷۳٬۶۳۶ بایت است.

**commit این دور**

1. ثبت‌های این گفتگو: `de056e7 → 597ebd8` (۶ commit، هر کدام در `git log --oneline`).
2. این فهرست فقط ثبت‌های پیش از هر ورود را نام می‌برد؛ هر ورودِ جدید خودش یک commit است و
   در `git log --oneline` همین شاخه دیده می‌شود.

## ۵. بررسی نشده / باز

1. merge شدن `stage-4/phase-0-snapshot-fix` در `dotin`: **انجام شده** — PR #25 با commit ادغام
   `d526be2` merge شد (۲۰۲۶-۱۱-۱۴ ساعت ۱۱:۵۹ UTC) و PR #26 که همان «glob → fail» را می‌آورد
   (شاخهٔ `stage-4/test-hardening`، commit `56f0705`) با commit ادغام `d8651dc` (۱۴:۳۹ UTC).
   وضعیت دو PR از `gh pr view` خوانده شد: `state=MERGED` برای هر دو، و PR #26 دقیقاً یک فایل
   دارد (+۵۹/−۵، همان عدد patch این مخزن).
2. اعمال همان اصلاح کد در `data-factory-v2`: **انجام شده** (`fa812b4`، push شده تا
   `23df624`). پچ «glob → fail» هم به `dotin` رفت و merge شد — PR #26، شاخهٔ
   `stage-4/test-hardening`، commit `56f0705` (فقط همان فایل تست، +۵۹/−۵) با commit ادغام
   `d8651dc`؛ پس هیچ‌کپی `skipTest` نمانده و دو مخزن یک رفتار دارند: `require_stage2_sql_dir`
   با fail.
3. نتیجهٔ P2: **تأییدشده با لاگ** (گزارش دانشجو؛ لاگ خام محلی). باز: خودِ فایل‌های
   `p1.log`/`p2.log` در مخزن نیستند و علت کندی `DELETE` همچنان **نامعلوم** است.
4. اجرای تست‌ها و `--reset` روی `olist_db`: عمداً انجام نمی‌شود. بازتولیدِ این دور روی
   `olist_phase0_test` و PostgreSQL سندباکس بود؛ `Ran 7/8 OK` روی `olist_db` **بررسی نشده**.
5. زمان‌ها (`1:16.24`، `2m47s`): فقط نقل اعداد دانشجو؛ در سندباکس این دور بازسنجیده نشد —
   venv جایگزین (`/tmp/v5`) درایور پایگاه و `pandas` ندارد، پس شمارش قابل‌مقایسه با آن
   محیط‌ها تولید نمی‌شود.
6. مسیر sink (`009` + `load_stage1_sink`) در هیچ اجرای ثبت‌شده‌ای آزموده نشده
   (`--skip-factory-sink`)؛ سبز-بودن آن مسیر **بررسی نشده**.
7. وجود فایل خام (`*.csv`, `*.zip`) در تاریخچهٔ شاخهٔ arena: بررسی شد و نبود (`git log
   --oneline origin/arena/… -- '*.csv' '*.zip'` خالی).
8. مقایسهٔ دو نسخهٔ AGENTS.md آپلودشده (۴۶٬۵۹۲ و ۷۲٬۷۰۲ بایت)؛ ویرایش روی نسخهٔ بزرگ‌تر
   انجام شد. ارجاع دیگر به `olist_db`/`--reset` خارج از خطوط ویرایش‌شده ممکن است باشد؛ فقط
   با grep الگو نگاه شده.

## ۶. قواعد این گفتگو (دستور به agent)

1. هیچ push یا merge بدون تأیید صریح دانشجو و نمایش مقصد/diff.
2. `--reset` فقط روی پایگاه آزمایشی، هرگز روی `olist_db`.
3. هیچ ادعایی دربارهٔ P2 یا merge تا آمدن خروجی واقعی — برای P2 این قاعده با لاگ‌ها ادا
   شد و حکم به «تأییدشده با لاگ (گزارش دانشجو؛ لاگ خام محلی)» عوض شد؛ روایت معارض ساقط
   نشده، فقط سازگارنشده مانده است.
4. قبل از اعمال فایل‌های md، diff نشان داده شود.
5. `psycopg2-binary` انحراف محیط است، نه وابستگی پروژه؛ در مستندات این‌طور نوشته می‌شود.

## AI usage log

- تحلیل باگ، نوشتن تست و patch، اصلاح مسیر تست، ویرایش فایل‌های md و نگارش این گزارش با کمک
  دستیار؛ تصمیم‌ها، اجرا روی پایگاه دانشجو، commit و push با دانشجو (push این دور در انتظار
  نمایش diff و تأیید است).

## پیوست الف — پچ جدا برای کپی `dotin` (اعمال شد: PR #26، merge در `d8651dc`)

فایل مقصد در `dotin`: `term-3/answers/stage-1-kiarash-akbari/tests/test_load_database_snapshots.py`
(آنجا ۴۳۲ خط است؛ نسخهٔ این مخزن ۴۸۹ خط). هدف: همان glob `stage-2*/sql`، ولی با **fail**
به‌جای `skipTest` — صفر تطابق نباید خودش را «چیزی برای آزمودن نیست» جا بزند.

`parents[2]` در `dotin` هم درست است، چون ریشهٔ پاسخ‌ها `term-3/answers/` هم
`stage-1-kiarash-akbari/` را دارد هم `stage-2-kiarash-akbari/`؛ یعنی همان glob دقیقاً یک
تطابق می‌دهد و لازم نیست شمارهٔ `parents` عوض شود.

۱) این سه‌تایی را بالای ثابت `SNAPSHOT_MIGRATION` بگذار و بلوک globِ همراه با `skipTest` را
   که الان در `setUp` هست حذف کن:

```python
ANSWERS_ROOT = Path(__file__).resolve().parents[2]
# `__name__`, not a literal: the module is `test_load_database_snapshots` under
# `discover -s tests` and `tests.test_load_database_snapshots` when named directly, and
# a hard-coded target silently patches nothing in one of the two.
MODULE = __name__


def stage2_sql_dir():
    """Locate the stage-2 `sql` directory, which sits beside stage-1 here and beside
    the answers in `dotin`, under two different names.

    Exactly one match is required. Zero means the layout or this helper is broken, and
    several mean the choice would be alphabetical luck -- both are worth a failure with
    the searched root and the sorted matches in the message, and neither is worth a
    skip, because a skip is how a real check quietly stops checking.
    """
    return sorted(path for path in ANSWERS_ROOT.glob("stage-2*/sql") if path.is_dir())


def require_stage2_sql_dir(test):
    found = stage2_sql_dir()
    if len(found) != 1:
        test.fail(
            f"expected exactly one stage-2*/sql directory under {ANSWERS_ROOT}, found "
            f"{len(found)}: {found or 'none'}; the fixtures need the real 001-009 scripts"
        )
    return found[0]
```

۲) این کلاس کمکی را کنار `FakeEngine` بگذار؛ تست بند ۴ بدون پایگاه از آن استفاده می‌کند و
   `fail`ش همان کاری را می‌کند که `TestCase.fail` می‌کند (raise)، تا helper نتواند به
   `found[0]` برسد:

```python
class FakeTest:
    """A test-case stand-in: `fail` raises and keeps the message, as TestCase does."""

    def fail(self, message=""):
        raise AssertionError(message)
```

۳) در `EmptyDatabaseSnapshotTests.setUp` فقط خط انتخاب منبع می‌آید — بقیهٔ `setUp`
   دست‌نخورده:

```python
        source = require_stage2_sql_dir(self)
```

۴) این تست را داخل `SnapshotRefreshOrderTests` اضافه کن (بی‌پایگاه است، پس همیشه اجرا
   می‌شود):

```python
    def test_the_stage2_sql_directory_requirement_fails_rather_than_skips(self):
        # No database needed: a broken layout is a failure with the counts in it,
        # in both directions, because "0 matches" and "2 matches" mean different things.
        with (
            mock.patch(f"{MODULE}.stage2_sql_dir", list),
            self.assertRaises(AssertionError) as zero,
        ):
            require_stage2_sql_dir(FakeTest())
        self.assertIn("found 0:", str(zero.exception))
        matches = [ANSWERS_ROOT / "stage-2/sql", ANSWERS_ROOT / "stage-2-x/sql"]
        with (
            mock.patch(f"{MODULE}.stage2_sql_dir", lambda: matches),
            self.assertRaises(AssertionError) as two,
        ):
            require_stage2_sql_dir(FakeTest())
        self.assertIn("found 2:", str(two.exception))
        self.assertIn(str(ANSWERS_ROOT), str(two.exception))
        # outside every patch: this checkout must satisfy its own requirement
        one = require_stage2_sql_dir(self)
        self.assertEqual([one], stage2_sql_dir())
        self.assertTrue(one.is_dir(), "the real stage-2 sql directory should be a directory")
```

۵) `MODULE = __name__` در بند ۱ عمدی است: در `dotin` هم این فایل را با
   `unittest discover -s tests` اجرا می‌کنند هم با `-m unittest tests.test_…`، و یک target
   سخت‌کدشده در یکی از دو حالت بی‌صدا هیچ‌چیز را patch نمی‌کند. `from pathlib import Path` و
   `from unittest import mock` از قبل در فایل هست و نباید دوباره اضافه شود.

بعد از اعمال، انتظار: `Ran 8 tests OK` روی PostgreSQL واقعی و `OK (skipped=2)` آفلاین، با
`ruff check` پاس. اعمال شد (PR #26، commit ادغام `d8651dc`). آزمون ماژول روی
`olist_phase0_test` در `dotin`: `Ran 8 tests OK`، بدون skip (۲٫۷۳۲ ثانیه) (گزارش دانشجو؛
لاگ در مخزن نیست). اجرای آفلاین sandbox: `Ran 8 OK` با دو skip.
اجرای کل `stage-1` روی `dotin/main` (`d8651dc`): `Ran 265 tests OK` (۵٫۳۰۸ ثانیه)، روی
`olist_phase0_test` با درایور `psycopg` (گزارش دانشجو؛ لاگ در مخزن نیست).
