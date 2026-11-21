<!--
  The cell bodies of `01_exploration.ipynb`, as data. Nothing here is executed by the test
  suite; `scripts/build_exploration_notebook.py` reads this file, runs the `code` blocks in one
  namespace when asked, and writes the notebook. Prose lives here in Persian because the
  layout guard forbids Persian in a .py source -- which is the rule this file exists to obey,
  not a workaround around it: the builder is still the only thing that can produce the .ipynb.

  Cell kinds: `%% md` renders as a markdown cell and takes one of the section ids the builder
  names in SECTIONS; `%% code` is a code cell, compiled before the notebook is written; `%% note`
  is rendered only into an un-executed notebook, so the "this was not run" warning cannot
  survive into a file that was.
-->

%% note
> **این نوتبوک اجرا نشده است.** فایل را `scripts/build_exploration_notebook.py` از همین سلول‌ها
> ساخته، ولی هیچ سلولی روی هیچ دیتابیسی اجرا نشده است: خروجی‌ها تهی‌اند و `execution_count`
> برابر `null` است. تحویلِ بند ۹.۱ این نیست؛ تحویل، خروجیِ اجراست. اجرا کن:
>
> 	CAPSTONE_DATABASE_URI=… python3 scripts/build_exploration_notebook.py --execute
>
> این سلول فقط در نوتبوکِ **اجرا نشده** نوشته می‌شود؛ اگر روزی با خروجیِ جعلی پرش کنید، خودِ
> نوتبوک هنوز می‌گوید که اجرا نشده بوده.

%% md
# ۰۱ · کشف داده — exploration

این نوتبوک **تولیدشده** است: سلول‌هایش همین حالا روی همین دیتابیس اجرا شده‌اند و
خروجی‌ها چیزی‌اند که برگشتند، نه چیزی که کسی نوشت. سازندهٔ فایل:
`scripts/build_exploration_notebook.py` (بند ۹.۱ چک‌لیست).

سلسله‌مراتب عمدي است: هر سلول از `capstone.*` صدا می‌زند، چون یک نوتبوکی که
خطِ تولید ویژگی را از نو بنویسد، دومین منبع حقیقت است و اولین جایی که دو منبع
با هم نمی‌خوانند، همان‌جاست که یک ادعای درزِ اطلاعاتی متولد می‌شود.

هیچ عددی اینجا اختراع نمی‌شود. اگر جدولی خالی است، یعنی اندازه گرفته نشده.

<!--section:intent-->

%% md
## ۱ · زمینه و اتصال

دیتابیس از `CAPSTONE_DATABASE_URI` می‌آید و از هیچ مقدار پیش‌فرضی در فایل config نمی‌آید (بند ۱۰.۲).

<!--section:context-->

%% code
import os
from collections import Counter
from capstone.config import load_contract
from capstone.features import connect, database_uri
from capstone import model
from pathlib import Path

STAGE = Path.cwd() if (Path.cwd() / 'config').is_dir() else Path.cwd().parent
config = load_contract(STAGE / 'config' / 'capstone.example.toml')
uri = database_uri(config, purpose="the exploration notebook")
engine = connect(uri)
print('engine:', uri.split('://')[0])
print('label:', model.label_definition())

%% md
## ۲ · برچسب، همان‌طور که تعریف شده

سه عددِ لازم قبل از هر مدل‌سازی: چند سفارش برچسب‌دار است، چند تا بی‌برچسب، و چند تا منفی. تفکیکِ «بی‌برچسب» از «مثبت» همان چیزی است که D2 در `decisions.md` برای آن جنگیده شد؛ یک نوتبوک که آن را با صفر پر کند، همان اشتباه را در قالب روایت تکرار می‌کند.

<!--section:label-->

%% code
frame = model.read_frame(config)
total = len(frame.rows)
labelled = [row for row in frame.rows if row[model.LABEL_KEY] is not None]
negatives = [row for row in labelled if row[model.LABEL_KEY]]
print(f'rows in frame: {total}')
print(f'labelled: {len(labelled)}   unlabelled: {total - len(labelled)}')
print(f'negatives: {len(negatives)}   rate: {len(negatives) / max(len(labelled), 1):.4f}')
print(f'feature columns: {len(frame.columns)}')

%% code
worst = Counter(row['worst_score'] for row in labelled)
print('worst_score histogram (the label is a MIN over reviews, so this is its raw shape):')
for score in sorted(key for key in worst if key is not None):
    print(f'  {score}: {worst[score]}')
counts = Counter(row['review_count'] for row in labelled if row['review_count'] is not None)
multi = sum(value for key, value in counts.items() if key and key > 1)
print(f'orders with more than one review: {multi}  (D3: worst_score_wins)')

%% md
## ۳ · توزیع ویژگی‌ها و «گم‌شده‌ها»

§۷ `decisions.md` صفر‌کردن را ممنوع کرده، پس ستونِ دارای NULL باید در همین نوتبوک دیده شود، نه در یک متدِ بی‌صدا. شمارشِ NULLها تنها چیزی است که بعداً در فاز ۸ (drift) مقایسه می‌شود، پس اینجا هم همین تابع می‌خواند.

<!--section:missing-->

%% code
missing: Counter = Counter()
for row in frame.rows:
    for name in frame.columns:
        if row.get(name) is None:
            missing[name] += 1
print('columns with at least one NULL, worst first:')
for name, count in missing.most_common():
    print(f'  {name:<34} {count:>7} of {total}  ({count / max(total, 1):.2%})')
if not missing:
    print('  none -- every feature cell in this frame is non-NULL')

%% md
## ۴ · ممیزی زمانی

سوالِ بند ۹.۱ چک‌لیست این است: آیا هر ستونی که وارد مدل می‌شود در لحظهٔ `order_approved_at` وجود داشته؟ پاسخ در خودِ داده نیست، در `capstone.column_audit` و گاردهای `sql/002` است؛ نوتبوک فقط گزارش می‌دهد گارد چه می‌گوید.
این سلول عمداً یک `SELECT` نوشتگی نیست: از همان `audit` می‌خواند که CLI هم می‌خواند، تا نوتبوک و `capstone audit` هیچ‌وقت دو جواب نداشته باشند.

<!--section:time-audit-->

%% code
from capstone import audit

# the same reader the CLI uses -- one source, so the notebook and `capstone audit`
# cannot end up disagreeing about what was verified
verdicts = audit.load_audit(STAGE / 'docs' / 'evidence' / 'column_audit.csv')
summary = audit.summarise(verdicts)
print('column audit, as the CLI reads it:')
for key, value in summary.items():
    print(f'  {key:<12} {value}')
problems = audit.audit_problems(verdicts)
print(f'problems: {len(problems)}')
for line in problems[:10]:
    print('  -', line)
if not problems:
    print('  none, so require_allowed can vouch for every frame column')

%% md
## ۵ · سیگنال بیرونی (۶۱۴)

ستون‌های `external_*` از `sql/005_external_signal.sql` می‌آیند. پوشششان یک عددِ تحویلی است: «سیگنال به چند درصد سفارش‌ها رسید» بدون آن، جملهٔ «کمک کرد» بی‌معناست.

<!--section:external-->

%% code
external = frame.external_columns()
print(f'external columns in the frame: {external or "none"}')
if external:
    matched = sum(1 for row in frame.rows if row.get('external_matched'))
    print(f'rows with external_matched: {matched} of {total} ({matched / max(total, 1):.2%})')
    for name in external:
        filled = sum(1 for row in frame.rows if row.get(name) is not None)
        print(f'  {name}: {filled} non-null')
else:
    print('the signal has not been joined into this frame yet -- run sql/005 and re-run')

%% md
## ۶ · فرضیه‌ها و شکست‌ها

چیزی که این نوتبوک باید نشان دهد فقط توزیع‌ها نیستند؛ فرضیه‌هایی که مردند هم بخشی از تحویل‌اند (بند ۹.۱: «کشف، EDA، ممیزی زمانی، فرضیه‌ها، شکست‌ها»). سلول آخر عمداً یک چیزِ محاسبه‌شدهٔ بی‌ادعا می‌نویسد تا ادعای بزرگ، عددِ کوچکِ خودش را داشته باشد.

<!--section:failures-->

%% code
# per-month negative rate, which is the distribution phase 8 compares train against
# eval. No model is fitted here on purpose: a drift claim that needs a fitted scorer
# is not a drift claim, and model.month_spread() is the place scores belong.
by_month: dict[str, list[int]] = {}
for row in labelled:
    month = row.get(model.MONTH_KEY) or 'unknown'
    hit = by_month.setdefault(month, [0, 0])
    hit[0] += 1
    hit[1] += 1 if row[model.LABEL_KEY] else 0
print(f'months with labelled rows: {len(by_month)}')
for month in sorted(by_month):
    rows, negative = by_month[month]
    print(f'  {month}  rows {rows:>6}  negatives {negative:>6}  rate {negative / max(rows, 1):.4f}')
rates = [negative / max(rows, 1) for rows, negative in by_month.values()]
if rates:
    print(f'spread across months: {min(rates):.4f} .. {max(rates):.4f}  '
          f'(a range of {max(rates) - min(rates):.4f})')
    print('a wide range is the hypothesis that the feature window drifts; the report')
    print('section that quotes it has to carry the number, not this cell.')
