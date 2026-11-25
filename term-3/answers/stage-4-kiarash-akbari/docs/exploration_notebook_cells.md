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

import capstone
from capstone.cli import default_config_path

# the stage folder from the package itself, not from the working directory (review B22), and the
# decided config the chain runs with, not the undecided example
STAGE = Path(capstone.__file__).resolve().parents[2]
config = load_contract(default_config_path(STAGE))
SEED = config.model.random_seed  # one seed, defined once; nothing below draws at random
uri = database_uri(config, purpose="the exploration notebook")
engine = connect(uri)
print('engine:', uri.split('://')[0])
print('label:', model.label_definition())

%% md
## ۲ · برچسب، همان‌طور که تعریف شده

سه عددِ لازم قبل از هر مدل‌سازی: چند سفارش برچسب‌دار است، چند تا بی‌برچسب، و چند تا منفی. تفکیکِ «بی‌برچسب» از «مثبت» همان چیزی است که D2 در `decisions.md` برای آن جنگیده شد؛ یک نوتبوک که آن را با صفر پر کند، همان اشتباه را در قالب روایت تکرار می‌کند.

<!--section:label-->

%% code
frame = model.read_frame(config, external='auto')
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
## ۶ · نمودارها: برچسب در طول زمان و چند ویژگیِ کلیدی

سه نمودار، همه روی سفارش‌های برچسب‌دار: نرخ نظر منفی در هر ماهِ تأیید (خط قرمز = مرز تقسیم)، و توزیعِ دو ویژگی که فرضیهٔ اصلی پشتشان است (مدتِ وعدهٔ تحویل و سهم کرایه) جدا برای منفی و غیرمنفی. جدولِ داده از همان `capstone.system.read_frame` می‌آید که لایهٔ مدل می‌خواند.

%% code
import matplotlib.pyplot as plt
import pandas as pd
from capstone import system

df = system.read_frame(engine)
lab = df[df['is_negative'].notna()]
rate = lab.groupby('decision_month')['is_negative'].agg(['mean', 'size'])
rate = rate[rate['size'] >= 100]
fig, axes = plt.subplots(1, 3, figsize=(15, 3.5))
axes[0].plot(rate.index, rate['mean'], marker='o')
axes[0].axvline(str(config.split.boundary)[:7], color='red', ls='--')
axes[0].set_title('negative-review rate by approval month (months with >=100 orders)')
axes[0].tick_params(axis='x', rotation=90)
for ax, column, clip in ((axes[1], 'promise_days', 60), (axes[2], 'freight_share', 0.6)):
    for flag, name in ((0.0, 'not negative'), (1.0, 'negative')):
        values = lab.loc[lab['is_negative'] == flag, column].dropna().clip(upper=clip)
        ax.hist(values, bins=30, density=True, alpha=0.5, label=name)
    ax.set_title(column); ax.legend()
plt.tight_layout()
plt.show()
print(rate.round(4).to_string())

%% md
## ۷ · آزمون فرضیه‌ها (فقط روی پنجرهٔ آموزش)

برای هر ویژگیِ عددی یک آزمون Mann-Whitney (منفی در برابر غیرمنفی) و اندازهٔ اثر به‌صورت AUC تک‌ستونی (۰٫۵ = بی‌اثر)؛ برای ستون‌های متنی آزمون کای‌دو روی جدول توافقی. همه روی سفارش‌های **تا مرز** (`split.boundary`) تا ماه‌های ارزیابی دیده نشوند. فرضیه‌ای که p-value بزرگ یا اثرِ نزدیک ۰٫۵ دارد «رد شده» علامت می‌خورد و در گزارش هم همین‌طور می‌آید.

%% code
from scipy.stats import mannwhitneyu, chi2_contingency

split = system.time_split(df, config.split.boundary, config.model.validation_months)  # the model's own split
train = df[split.train & df['is_negative'].notna()]
numeric, categorical = system.feature_columns(df, external=False)
if system.EXTERNAL_COLUMN in df.columns:
    numeric = numeric + [system.EXTERNAL_COLUMN]
rows = []
for column in numeric:
    neg = train.loc[train['is_negative'] == 1, column].dropna()
    pos = train.loc[train['is_negative'] == 0, column].dropna()
    if neg.nunique() < 2 and pos.nunique() < 2:
        continue
    stat, p = mannwhitneyu(neg, pos, alternative='two-sided')
    rows.append({'feature': column, 'test': 'Mann-Whitney', 'effect_auc': stat / (len(neg) * len(pos)), 'p_value': p})
for column in categorical:
    table = pd.crosstab(train[column].fillna('(blank)'), train['is_negative'])
    table = table[table.sum(axis=1) >= 50]
    chi2, p, dof, _ = chi2_contingency(table)
    rows.append({'feature': column, 'test': f'chi2 dof={dof}', 'effect_auc': None, 'p_value': p})
tests = pd.DataFrame(rows)
tests['verdict'] = [
    'supported' if p < 0.001 and (auc is None or pd.isna(auc) or abs(auc - 0.5) >= 0.02) else 'not supported'
    for p, auc in zip(tests['p_value'], tests['effect_auc'])
]
tests['strength'] = (tests['effect_auc'] - 0.5).abs()
print(f'training window: {len(train)} labelled orders, {int(train["is_negative"].sum())} negatives')
print(tests.sort_values(['verdict', 'strength'], ascending=[False, False]).drop(columns='strength').round(5).to_string(index=False))

%% md
## ۸ · فرضیه‌ها و شکست‌ها

چیزی که این نوتبوک باید نشان دهد فقط توزیع‌ها نیستند؛ فرضیه‌هایی که مردند هم بخشی از تحویل‌اند (بند ۹.۱: «کشف، EDA، ممیزی زمانی، فرضیه‌ها، شکست‌ها»). سلول آخر عمداً یک چیزِ محاسبه‌شدهٔ بی‌ادعا می‌نویسد تا ادعای بزرگ، عددِ کوچکِ خودش را داشته باشد.

مسیرهایی که امتحان شدند و کنار رفتند (شرح کامل در `report.md` بخش ۷ و `decisions.md`):

- **چگالی جمعیت به‌عنوان سیگنال بیرونی:** در جدول بالا اثرش نزدیک ۰٫۵ است و «not supported» خورده؛ در مدل هم بازهٔ اطمینانِ اثرش صفر را در بر دارد (`docs/evidence/model-ablation-run.txt`). فرضیه رد شد و مدل تحویلی بدون آن است.
- **سرشماری ۲۰۲۲** اول به‌جای ۲۰۱۰ آمده بود: عددی که در لحظهٔ تأیید سفارش‌های ۲۰۱۶–۲۰۱۸ وجود نداشت، یعنی نشت از راه منبع بیرونی.
- **فرضیهٔ «درآمد»** برای منبع بیرونی: جدول IBGE اصلاً متغیر درآمد نداشت.
- **چند فرضیهٔ ویژگی** (مثلاً `shipping_limit_days`، `n_products`، `product_weight_g`) روی آموزش اثری نزدیک به صفر دارند؛ در مدل مانده‌اند چون مدل درختی خودش وزنشان را کم می‌کند، ولی در گزارش «حمایت‌نشده» خوانده می‌شوند.

سلول آخر نرخ منفی هر ماه را می‌دهد؛ ماه‌هایی با کمتر از ۱۰۰ سفارش برچسب‌دار در دامنه حساب نمی‌شوند (۲۰۱۶-۰۹، ۲۰۱۶-۱۲ و ۲۰۱۸-۰۹ یک سفارش دارند).

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
rates = [negative / rows for rows, negative in by_month.values() if rows >= 100]
if rates:
    print(f'spread across months with >= 100 labelled orders: {min(rates):.4f} .. {max(rates):.4f}  '
          f'(a range of {max(rates) - min(rates):.4f})')
    print('a wide range is the hypothesis that the feature window drifts; the report')
    print('section that quotes it has to carry the number, not this cell.')

engine.close()
