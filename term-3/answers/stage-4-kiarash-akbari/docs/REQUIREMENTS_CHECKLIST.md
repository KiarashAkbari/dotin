# چک‌لیست الزامات چالش نهایی (مرحلهٔ ۴)

این فایل «آنچه واقعاً در درخت هست» را از «آنچه صورت‌مسئله خواسته» جدا می‌کند. هر بند: شناسه، ارجاع به
صورت‌مسئله، وضعیت، و شاهدی که در همین پوشه پیدا می‌شود. همهٔ مسیرها نسبت به
`term-3/answers/stage-4-kiarash-akbari/` اند. هیچ عددی این‌جا نوشته نشده که در یک فایل شاهد
(`docs/evidence/…`) یا خروجیِ ذخیره‌شدهٔ یک نوتبوک نباشد؛ هر عدد کنار فایلش آمده.

**وضعیت این نسخه:** بازنویسیِ کامل روی شاخهٔ `fix/stage-4-finish` (۲۰۲۶-۱۱-۲۷). نسخهٔ قبلی این فایل
وضعیتِ فاز ۰ تا ۴ را نگه داشته بود («`src/` وجود ندارد» و مانند آن) و دیگر با درخت نمی‌خواند. شاهدهای
اجرایی از سندباکس‌اند: PostgreSQL 16.4، ۲ vCPU، Olistِ کاملِ عمومی، IBGEِ زنده. اگر `git log -1` چیزی
جدیدتر از این دور می‌گوید، بندهای «انجام‌شده» باید دوباره با فایل‌شان سنجیده شوند.

سه وضعیت مجاز:

| واژه | یعنی |
|---|---|
| انجام‌شده | در همین درخت هست و با خواندن فایل یا اجرای تست تأیید شده |
| باز | الزام است و چیزی در درخت برآورده‌اش نمی‌کند |
| نامشخص | بخشی‌اش هست، یا نتیجه به اجرایی گره خورده که فقط روی ماشین دانشجو/ارزیاب معنا دارد |

مخفف ارجاع‌ها: `00` = راهنمای تحویل (`../stage-1-kiarash-akbari/files-given/00_Delivery_Guide.txt`)،
`04` = صورت‌مسئلهٔ چالش نهایی، `02`/`03` = چالش‌های مرحلهٔ ۲ و ۳. `04 §۱`…`04 §۷` هفت زیربخشِ «آنچه باید
تحویل داده شود»، `§معماری` = «معماری الزامی سامانه»، `§پذیرش` = چک‌لیست ۱۰ سطریِ «معیار پذیرش»،
`§تحویل` = «تحویل این چالش»، `§منابع` = «منابع داده»، `§تعریف` = «تعریف دقیق مسئله».
شناسه‌ها در کد با رقم ASCII (`4.1`) و این‌جا با رقم فارسی (`۴.۱`) نوشته می‌شوند؛ یکی‌اند
(`src/capstone/chain.py`، `src/capstone/doctor.py`، `src/capstone/config.py` به همین شناسه‌ها ارجاع می‌دهند).

## ۱. پیش‌شرط‌ها و وزن‌ها

- **۱.۱** · نامشخص — `04 §پذیرش` سطر ۱ + `00 §۸`: پروژه روی ماشین ارزیاب اجرا شود.
  شاهد: در سندباکس از دیتابیسِ خالی ۷ از ۷ گام `ok` (`docs/evidence/chain-run-manifest.json`،
  `docs/evidence/chain-run-log.jsonl`). روی ماشین دانشجو بعد از patchهای 0048 به بعد هنوز اجرا نشده؛
  دستورش در `README.md` بخش «راه‌اندازی».
- **۱.۲** · انجام‌شده — `04 §وزن ارزیابی`: وزن‌ها (نشت ۲۵٪، یکپارچگی ۲۰٪، مهندسی ۲۰٪، مدل ۱۵٪، گزارش ۱۵٪،
  غیرفنی ۵٪). شاهد: `ROADMAP.md` §۰.
- **۱.۳** · انجام‌شده (سندباکس) — `04 §معماری` بند ۵: کل زنجیره ≤ ۴۵ دقیقه با زمانِ هر حلقه.
  شاهد: `docs/evidence/chain-run-manifest.json` — ۱۲۷٫۴ ثانیه، `over_budget: false`، زمانِ جدای هر گام؛
  سقف از `config/capstone.default.toml` (`budget.chain_minutes`).
- **۱.۴** · نامشخص — `04 §معماری` بند ۲: کوئری ویژگی < ۵۰۰ ms با پلن. سندباکس برآورده می‌کند
  (`docs/evidence/ceiling-feature-analysis.sandbox-2026-11-27.txt`: بهترینِ ۱۵ تکرار ۳۵۷٫۲ ms، ۱۵ از ۱۵ زیر سقف،
  پلن کامل در همان فایل). روی ماشین دانشجو بعد از 0048 دوباره گرفته نشده — `README.md` بخش «زمانِ کوئریِ ویژگی».
  جزئیات: بند ۵.۳.
- **۱.۵** · انجام‌شده — `00 §۶`: ابزار بیرون از دامنه فقط با توضیح. شاهد: `requirements.txt` (همه `==`)؛
  ابزارها pandas/scikit-learn/psycopg/matplotlib/scipy و package مرحلهٔ ۱؛ بخش AI usage در `report.md`.
- **۱.۶** · انجام‌شده — `00 §۳`: هیچ عددی ساخته نشود. شاهد: `report.md`، `manager_summary.md` و
  `README.md` فقط از فایل‌های `docs/evidence/` نقل می‌کنند؛ `tests/test_evidence_provenance.py` نمی‌گذارد شاهدی بی‌صدا
  بازنویسی شود. چیزی که اندازه گرفته نشده «اندازه گرفته نشده» نوشته شده (بخش آخر همین فایل).

## ۲. قواعد `00` روی این مرحله

- **۲.۱** · انجام‌شده — `00 §۲.۱` اجرای کامل در kernel تازه. شاهد: `scripts/build_exploration_notebook.py
  --execute --kernel` و `scripts/build_system_notebook.py` هر دو نوتبوک را در kernel تازه اجرا و ذخیره می‌کنند.
- **۲.۲** · انجام‌شده — `00 §۲.۲` شماره‌های پیوسته ۱..N. شاهد: `tests/test_delivered_notebooks.py`.
- **۲.۳** · انجام‌شده — `00 §۲.۳` خروجی‌ها ذخیره. شاهد: همان تست (هیچ سلول کدِ بی‌خروجی، هیچ traceback).
- **۲.۴** · انجام‌شده — `00 §۲.۴` بدون مسیر مطلق. شاهد: همان تست (الگوی `/home`، `/Users`، `/tmp` …) و
  مسیرهای نسبی در `config/capstone.default.toml`.
- **۲.۵** · انجام‌شده — `00 §۲.۵` یک seed در ابتدا. شاهد: `SEED = config.model.random_seed` در اولین سلول کدِ
  هر دو نوتبوک؛ پین در `tests/test_delivered_notebooks.py`.
- **۲.۶** · انجام‌شده — `00 §۲.۶` منطق سنگین در `.py`. شاهد: `src/capstone/`؛ نوتبوک‌ها فقط `capstone` را صدا
  می‌زنند و `tests/test_exploration_notebook.py` (`test_the_code_cells_use_capstone_rather_than_a_parallel_query`)
  هر `SELECT` خامی را در سلول رد می‌کند؛ شمارش D1/D2 به `capstone.model.decision_counts` منتقل شد.
- **۲.۷** · انجام‌شده — `00 §۲.۷` هر بخش با Markdownِ راوی. شاهد: `docs/exploration_notebook_cells.md`،
  `docs/system_notebook_cells.md`؛ پین در `tests/test_delivered_notebooks.py`.
- **۲.۸** · انجام‌شده — `00 §۲.۸` سقف زمان. شاهد: بند ۱.۳.
- **۲.۹** · انجام‌شده — `00 §۲.۹` pinِ دقیق. شاهد: `requirements.txt` (تنها سطرِ بی‌`==`، `-e ../stage-1-kiarash-akbari`
  است که package مرحلهٔ ۱ را از همین مخزن نصب می‌کند).
- **۲.۱۰** · انجام‌شده — `00 §۲` مستندات فارسی، کد و commit انگلیسی. شاهد: `tests/test_stage4_layout.py`
  (`test_no_persian_in_the_python_sources`، `test_docstrings_are_english_sentences`).
- **۲.۱۱** · انجام‌شده — `00 §۳` پنج بخشِ گزارش. شاهد: `report.md` §۱ فهم مسئله، §۲ تصمیم‌ها (≥ ۱۰)،
  §۷ شکست‌ها، §۸ ریسک‌ها، §۹ «دو روز دیگر».
- **۲.۱۲** · باز — `00 §۳` + `04 §تحویل`: ۸۰۰ تا ۱۵۰۰ کلمه. `report.md` هنوز از این بازه بلندتر است.
- **۲.۱۳** · انجام‌شده — `00 §۳/§۴` هر عدد ردیابی‌پذیر. شاهد: هر عدد گزارش کنار فایل شاهدش آمده؛ D1/D2 در خروجیِ
  `01_exploration.ipynb`، نتیجه‌ها در `02_system.ipynb` از `docs/evidence/model-run.json`.
- **۲.۱۴** · انجام‌شده — `00 §۴` repo با history، بدون دادهٔ خام. شاهد: `.gitignore` (`data/raw/*`، `*.joblib`، …)
  و `tests/test_stage4_layout.py`.
- **۲.۱۵** · انجام‌شده — `00 §۵` راه‌حل از دستیار گرفته نشود. شاهد: `ROADMAP.md` §۱؛ انتخاب منبع با دانشجو (۱۲.۱).
- **۲.۱۶** · انجام‌شده — `00 §۷` بخش AI usage. شاهد: `report.md` بخش «AI usage»، تاریخچه در `docs/CHANGE_REPORT.md`.

## ۳. تعریف مسئله (`04 §تعریف`)

- **۳.۱** · انجام‌شده — واحد = سفارش، برچسب = امتیاز ۱ یا ۲. شاهد: `sql/003_feature_views.sql`
  (`capstone.v_order_label`)، `capstone.model.label_definition`، `decisions.md` §۱–۳.
- **۳.۲** · انجام‌شده — فقط اطلاعات تا `order_approved_at`. شاهد: `sql/002_time_guard.sql`، ممیزی بخش ۴.
- **۳.۳** · انجام‌شده — D1 (بدون `order_approved_at`). شاهد: `decisions.md` §۱، `no_approved_at` در
  `config/capstone.default.toml`؛ شمارش روی دیتابیس در خروجیِ `01_exploration.ipynb` (۱۶۰ سفارش، ۱۴۱ لغوشده) و
  `docs/evidence/phase1-decision-counts.txt`.
- **۳.۴** · انجام‌شده — D2 (بدون نظر). شاهد: `decisions.md` §۲، `no_review` در config، `capacity_all_approved` در
  `docs/evidence/model-run.json`؛ شمارش در `01_exploration.ipynb` (۷۶۸).
- **۳.۵** · انجام‌شده — D3 (چند نظر): `multi_review = "worst_score_wins"`. شاهد: `decisions.md` §۳،
  `docs/evidence/decision-impact.txt`.
- **۳.۶** · انجام‌شده — تقسیم زمانی با مرز موجه. شاهد: `split.boundary = 2018-05-31` در config، `decisions.md` §۴،
  `split` در `docs/evidence/model-run.json`؛ حساسیتِ مرز (۲۰۱۸-۰۶-۳۰ و ۲۰۱۸-۰۷-۳۱) در
  `docs/evidence/boundary-sensitivity.txt` و `decisions.md` §۴۰.
- **۳.۷** · انجام‌شده — معیار اصلی recall@10٪ (رتبه‌بندی). شاهد: `capstone.capacity.rank_then_select`،
  `docs/evidence/capacity-table.json`، `tests/test_capstone_capacity.py`.
- **۳.۸** · انجام‌شده — معیار دوم روی آخرین ماه و توضیح افت. شاهد: `drift_vs_model.last_full_month` در
  `docs/evidence/model-run.json` (۲۰۱۸-۰۸، recall ۰٫۲۰۶۰؛ ماه‌های زیر ۵۰۰ سطرِ برچسب‌دار داوری نمی‌شوند)،
  `report.md` §۳ و §۶، `02_system.ipynb`.
- **۳.۹** · انجام‌شده — شمارش واقعیِ تکلیف‌ها. شاهد: `docs/evidence/phase1-decision-counts.txt`، `01_exploration.ipynb`.

## ۴. ممیزی زمانی ستون‌ها (۲۵٪)

- **۴.۱** · انجام‌شده — `04 §۱` جدول کامل. شاهد: `docs/evidence/column_audit.csv` (۵۳ ستون، هر ۹ جدول)،
  `docs/evidence/column-audit-run.txt`، `python -m capstone audit --coverage --require-complete`.
- **۴.۲** · انجام‌شده — هیچ ستونی بیرون از ممیزی در مدل. شاهد: `capstone.audit.require_allowed`،
  `capstone.require_allowed` در `sql/002_time_guard.sql`، `tests/test_leak_gate.py`، `tests/test_capstone_sql_guards.py`
  (ایستا + زنده با `pg_depend` وقتی `CAPSTONE_DATABASE_URI` هست)؛ گام `load-audit` در زنجیره.
- **۴.۳** · نامشخص — شاهدِ اجراشدنی برای هر حکم. دو شاهد از سه (زمانِ پر شدن، همبستگی با `order_status`) روی هر ۵۳
  ستون: `scripts/audit_columns.py`، `docs/evidence/column-audit-run.txt`. سومی (دیکشنری داده) در مخزن نیست.
- **۴.۴** · انجام‌شده — ≥ ۳ ستونِ وسوسه‌کننده نام‌برده. شاهد: `report.md` §۴، `decisions.md` §۱۰.
- **۴.۵** · انجام‌شده — مدل تقلبی و فاصله. شاهد: `docs/evidence/cheating-model-gap.txt` (فاصلهٔ recall@10٪: +۰٫۱۸۶۵)،
  `scripts/cheating_model_gap.py` (بیرون از زنجیره).
- **۴.۶** · انجام‌شده — تست ضدنشت از فاز ۲. شاهد: `tests/test_leak_gate.py`، `docs/evidence/leak-red-run.txt`،
  `docs/evidence/leak-green-run.txt`.
- **۴.۷** · انجام‌شده — دامِ `olist.v_delivered_orders` مرحلهٔ ۲ علامت خورده. شاهد: `sql/README.md` (فهرست دام‌ها).

## ۵. ویژگی‌ها (`04 §۲`)

- **۵.۱** · انجام‌شده — ≥ ۱۰ ویژگی از چند جدول در View. شاهد: `capstone.v_feature_frame` در
  `sql/003_feature_views.sql` (۲۹ ویژگی، ۶ جدولِ نام‌بردهٔ صورت‌مسئله)، `docs/feature_hypotheses.csv`.
- **۵.۲** · انجام‌شده — تجمیع‌های تاریخی فقط گذشته را می‌بینند، در خودِ SQL. شاهد: `RANGE … PRECEDING` در
  `sql/003_feature_views.sql`، توابع as-of در `sql/004_as_of_functions.sql` (از جمله تاریخچهٔ فروشنده)،
  `tests/test_capstone_features.py`.
- **۵.۳** · نامشخص — زمان‌گیریِ تکرارپذیر + `EXPLAIN (ANALYZE)`، < ۵۰۰ ms. ابزار: `scripts/analyze_ceiling_feature.py
  --tag` (۱۵ تکرار، قبل/بعد از `ANALYZE`، پلن). شاهدِ سندباکس:
  `docs/evidence/ceiling-feature-analysis.sandbox-2026-11-27.txt` (۳۵۷٫۲ ms). آستانه‌های این ابزار:
  `decisions.md` §۳۹، `docs/evidence/ceiling-threshold-derivation.txt`. اندازهٔ ماشین دانشجو بعد از 0048 گرفته نشده.
- **۵.۴** · انجام‌شده — یک جملهٔ فرضیه برای هر ویژگی. شاهد: `report.md` §۵، `docs/feature_hypotheses.csv`.
- **۵.۵** · انجام‌شده — همهٔ DDL نسخه‌دار. شاهد: `sql/001`…`006`، `docs/evidence/sql-checksums.txt`.

## ۶. مدل و تصمیم ظرفیت‌محور (`04 §۳`)

- **۶.۱** · انجام‌شده — خط مبنای قاعده‌ای با عدد. شاهد: `rule_baseline` در `docs/evidence/model-run.json`
  («بیشترین قلم در سفارش»)، `report.md` §۳.
- **۶.۲** · انجام‌شده — دو خانوادهٔ مدل. شاهد: logreg ×۲ و HistGradientBoosting ×۲ در `selection` همان فایل؛
  `capstone.model.require_two_families`.
- **۶.۳** · انجام‌شده — سهمیهٔ ۱۰٪، نه آستانهٔ ۰٫۵. شاهد: `capstone.capacity`، `data/curated/followup_list.csv`
  (خروجی زنجیره؛ در Git نمی‌آید).
- **۶.۴** · انجام‌شده — جدول ۵/۱۰/۲۰٪ با کارِ اضافه و فاصله تا مدل تقلبی. شاهد: `docs/evidence/capacity-table.json`،
  `docs/evidence/cheating-model-gap.txt`، `report.md` §۳.
- **۶.۵** · انجام‌شده — CM مطلق + P/R/F1/AUC در نقطهٔ عملیاتی. شاهد: `evaluation.<model>.operating_point` در
  `docs/evidence/model-run.json`، `report.md` §۳، `02_system.ipynb`.
- **۶.۶** · انجام‌شده — پراکندگی ماهانه. شاهد: `drift_vs_model.monthly_recall` در همان فایل، `02_system.ipynb`.

## ۷. جابه‌جایی داده (`04 §۴`)

- **۷.۱** · انجام‌شده — مقایسهٔ توزیع‌ها. شاهد: PSI در `drift` و `drift_vs_model.top_shifted_features`
  (`docs/evidence/model-run.json`).
- **۷.۲** · انجام‌شده — آیا افت با جابه‌جایی توضیح داده می‌شود. شاهد: `report.md` §۶ (سازگاری، بدون ادعای علّی).
- **۷.۳** · انجام‌شده — پاراگرافِ «شش ماه بعد». شاهد: `report.md` §۶.

## ۸. مسیر پیش‌بینی و برش بسته (`04 §۵`)

- **۸.۱** · انجام‌شده — نقطهٔ ورود با همان اعتبارسنجی و آماده‌سازی. شاهد: `python -m capstone predict --input …`
  (`src/capstone/predict.py`)، loader مرحلهٔ ۱، همان Viewها؛ `tests/test_capstone_predict.py`.
- **۸.۲** · انجام‌شده — schema غلط → خطای روشن با نام فایل و ستون. شاهد: `tests/test_capstone_predict.py`.
- **۸.۳** · انجام‌شده (تمرین) — اجرای برش بسته. شاهد: `docs/evidence/sealed-cut-rehearsal.txt` (ماه ۲۰۱۸-۰۸:
  امتیازِ predict با امتیازِ گام مدل برای هر ۶٬۶۲۰ سفارش یکی است و recall@10٪ همان ۰٫۲۰۶۰)؛
  `scripts/sealed_cut_rehearsal.py`. هر snapshotِ `capstone.predict.SNAPSHOTS` دقیقاً یک بار تازه می‌شود؛ نگهبانش در
  `tests/test_capstone_sql_guards.py` است.
- **۸.۴** · انجام‌شده — زمانِ هر حلقه در manifest. شاهد: `docs/evidence/chain-run-manifest.json`.
- **۸.۵** · انجام‌شده (سندباکس) — آزمون روی محیط خالی. شاهد: همان manifest روی دیتابیسِ تازه‌ساخته؛
  `tests/test_end_to_end.py` روی دادهٔ ساختگی.

## ۹. تحویل‌های متنی و نوتبوک‌ها

- **۹.۱** · انجام‌شده — `01_exploration.ipynb`. اجراشده در kernel تازه، شماره‌های ۱..۹؛ سازنده:
  `scripts/build_exploration_notebook.py`؛ تست: `tests/test_exploration_notebook.py`، `tests/test_delivered_notebooks.py`.
- **۹.۲** · انجام‌شده — `02_system.ipynb`. سازنده: `scripts/build_system_notebook.py`؛ تست: `tests/test_delivered_notebooks.py`.
- **۹.۳** · انجام‌شده — `report.md` با ≥ ۱۰ تصمیم و بخش جدای نشت (§۴).
- **۹.۴** · انجام‌شده — `decisions.md` ≥ ۸ تصمیم با زمینه/گزینه‌ها/تصمیم/عوارض. شاهد: `decisions.md` (§۱ تا §۴۰)؛
  ساختار هر بلوک در `tests/test_stage4_layout.py` پین شده.
- **۹.۵** · انجام‌شده — `README.md` برای غریبه. شاهد: `README.md` (پیش‌نیاز، فایل URI با `umask 077`، زنجیره،
  پیش‌بینی، تست‌ها با و بدون DB، زمان‌گیری، ساخت نوتبوک‌ها)؛ `tests/test_stage4_layout.py`.
- **۹.۶** · انجام‌شده — جمع‌بندی یک‌صفحه‌ای غیرفنی. شاهد: `manager_summary.md`.

## ۱۰. کیفیت مهندسی و تاریخچه

- **۱۰.۱** · انجام‌شده — ≥ ۱۵ تست جدید. شاهد: `tests/` (بیش از ۵۰۰ تستِ این مرحله؛ شمارِ هر دور در گزارش تحویل).
- **۱۰.۲** · انجام‌شده — تست سرتاسری روی دادهٔ ساختگی تا پیش‌بینی. شاهد: `tests/test_end_to_end.py`،
  `tests/synthetic_olist.py`؛ به دیتابیسِ دورریختنی با `CAPSTONE_SMOKE_DATABASE_URI` و `CAPSTONE_SMOKE_ALLOW_DESTROY=1` نیاز دارد.
- **۱۰.۳** · انجام‌شده — تست ضدنشتی که واقعاً قرمز می‌شود. شاهد: `docs/evidence/leak-red-run.txt`،
  `docs/evidence/sql-guards-red-run.txt`.
- **۱۰.۴** · نامشخص — نشت الفبای بیگانه. فقط نیمهٔ Python پوشش دارد (`test_no_persian_in_the_python_sources`)؛
  تستی روی `*.md`/`*.ipynb` برای الفبای سوم نیست.
- **۱۰.۵** · انجام‌شده — تست‌های مراحل ۱ تا ۳ سبز. شاهد: `docs/evidence/stage1-suite-his-run.txt` و اجرای این دور
  (گزارش تحویل).
- **۱۰.۶** · انجام‌شده — ≥ ۲۵ commit پراکنده. شاهد: `git log -- term-3/answers/stage-4-kiarash-akbari`
  (بیش از ۹۰ commit، ۲۰۲۶-۱۱-۱۶ تا ۲۰۲۶-۱۱-۲۷).
- **۱۰.۷** · انجام‌شده — پیام commit انگلیسی، بدون دادهٔ خام.
- **۱۰.۸** · انجام‌شده — اسکلت واقعی `src/`، `tests/`، `sql/`، `scripts/`.

## ۱۱. یکپارچگی زنجیره (`04 §معماری`)

- **۱۱.۱** · انجام‌شده (Olist) / نامشخص (IBGE) — لایهٔ دریافت = package مرحلهٔ ۱. شاهد: `src/capstone/ingest.py`
  (verify و load با loader مرحلهٔ ۱، قرنطینه و manifest)؛ IBGE از `src/capstone/external.py` می‌آید نه loader
  مرحلهٔ ۱ — دلیلش `decisions.md` §۱۲.
- **۱۱.۲** · انجام‌شده — مدل فقط از Database می‌خواند. شاهد: `capstone.system.read_frame`، `capstone.model.read_frame`.
- **۱۱.۳** · انجام‌شده — یک فرمان. شاهد: `scripts/run_chain.py` (= `python -m capstone chain`)، `tests/test_run_chain.py`.
- **۱۱.۴** · انجام‌شده — MVهای مرحلهٔ ۲ بعد از `--reset` پر می‌مانند. شاهد: گام `load-database` و
  `docs/PHASE0_REPORT.md` §۱ ردیف ۷.
- **۱۱.۵** · انجام‌شده — View در برابر snapshot با عدد. شاهد: `decisions.md` §۶ و §۳۸،
  `docs/evidence/frame-join-once-sandbox.txt`.

## ۱۲. منبع بیرونی (`04 §منابع` بند ۲)

- **۱۲.۱** · انجام‌شده — انتخاب منبع با دانشجو: IBGE (جدول 1301، متغیر 616، سال ۲۰۱۰). شاهد: `decisions.md` §۸،
  `external` در `config/capstone.default.toml`.
- **۱۲.۲** · انجام‌شده — رایگان و بی‌کلید. شاهد: `docs/evidence/ibge-access-his-attestation.txt`.
- **۱۲.۳** · انجام‌شده — پیوند با مکان سفارش + فرضیه. شاهد: `sql/005_external_signal.sql`، `sql/006_external_bridge.sql`،
  `report.md` §۷.
- **۱۲.۴** · انجام‌شده — schema جدا. شاهد: `capstone.external_municipio` در `sql/005_external_signal.sql`.
- **۱۲.۵** · انجام‌شده — نتیجهٔ منفی گزارش شده. شاهد: `docs/evidence/model-ablation-run.txt` (بازهٔ bootstrapِ delta
  برای مدل انتخاب‌شده صفر را در بر دارد)، `report.md` §۷.
- **۱۲.۶** · انجام‌شده — تست‌های مرحلهٔ ۱ نمی‌شکنند. شاهد: اجرای مجموعهٔ مرحلهٔ ۱ در این دور (گزارش تحویل).

## ۱۳. جزئیات `02` و `03` که این مرحله رویشان تکیه دارد

- **۱۳.۱** · انجام‌شده — روش اندازه‌گیری زمان (چند اجرا، پراکندگی، محیط). شاهد: `scripts/analyze_ceiling_feature.py`،
  فایل‌های `docs/evidence/ceiling-feature-analysis.*.txt`.
- **۱۳.۲** · انجام‌شده — پلن قبل/بعد. شاهد: همان فایل‌ها (قبل و بعد از `ANALYZE`) و
  `docs/evidence/ceiling-feature-analysis.join-once-before.txt` / `…-after.txt`.
- **۱۳.۳** · انجام‌شده — هزینهٔ پنهان index (مرحلهٔ ۲). شاهد: `../stage-2-kiarash-akbari/report.md`.
- **۱۳.۴** · انجام‌شده — کوئری‌ای که index نجاتش نمی‌دهد (مرحلهٔ ۲). شاهد: همان.
- **۱۳.۵** · انجام‌شده — Function داخل `SELECT`. شاهد: `sql/004_as_of_functions.sql`.
- **۱۳.۶** · انجام‌شده — SQLAlchemy/psycopg امن (پارامتری، بدون connection بازمانده). شاهد: `src/capstone/features.py`،
  `src/capstone/errors.py` (رمز در هیچ پیام خطایی نیست).
- **۱۳.۷** · نامشخص — Procedure پارامتردار و idempotent. این مرحله Procedure ندارد؛ کارِ معادل با Function
  (`capstone.refresh_external_municipio`، `capstone.upsert_external_municipio` در `sql/005_external_signal.sql`) انجام می‌شود.
- **۱۳.۸** · انجام‌شده — مرز نشت‌ناپذیر آموزش/ارزیابی. شاهد: `capstone.system.time_split`، `tests/test_capstone_system.py`.
- **۱۳.۹** · نامشخص — معیار هزینه‌محور (`03 §۶`). این مرحله تصمیم را با سهمیهٔ ظرفیت می‌گیرد (`04 §۳`)، نه ماتریس هزینه؛
  عدد «هزینه به ازای ۱۰۰۰» در این مرحله گزارش نشده.
- **۱۳.۱۰** · انجام‌شده — ارزیابی چندتکه با پراکندگی. شاهد: پراکندگی ماهانه و بازهٔ bootstrap
  (`model_vs_rule_recall_delta_ci95`) در `docs/evidence/model-run.json`.
- **۱۳.۱۱** · باز — Model Card و انصاف بین زیرگروه‌ها (مثلاً ایالت). در این مرحله ساخته نشده.
- **۱۳.۱۲** · انجام‌شده — همان کد اعتبارسنجی در مسیر پیش‌بینی. شاهد: `src/capstone/predict.py` (loader مرحلهٔ ۱ و همان Viewها).

## ۱۴. آنچه فقط دانشجو تعیین می‌کند

- **۱۴.۱** · نامشخص — موعد تحویل واقعی.
- **۱۴.۲** · نامشخص — آیا سقف ۴۵ دقیقه دانلودِ داده را هم می‌خورد. اجرای سندباکس دانلود را نمی‌شمارد (CSVها از پیش روی دیسک بودند).
- **۱۴.۳** · انجام‌شده — D1/D2/D3 و مرز. شاهد: `config/capstone.default.toml`، `decisions.md` §۱–۴.
- **۱۴.۴** · انجام‌شده — منبع API و فرضیه. شاهد: بند ۱۲.۱.

## آنچه هنوز باید روی ماشین دانشجو انجام شود

1. چرخاندن رمز دیتابیسی که پیش‌تر در یک خروجی تست دیده شد، پیش از هر اجرا.
2. اجرای `scripts/run_chain.py` روی دیتابیسِ تازه و ذخیرهٔ manifest.
3. `scripts/analyze_ceiling_feature.py --tag <نام>` روی دیتابیسِ بی‌کار — بند ۱.۴ و ۵.۳ را می‌بندد یا رد می‌کند.
4. ساختن دوبارهٔ هر دو نوتبوک و اجرای تست‌ها با و بی‌دیتابیس، و smoke/سرتاسری روی دیتابیسِ دورریختنی.
