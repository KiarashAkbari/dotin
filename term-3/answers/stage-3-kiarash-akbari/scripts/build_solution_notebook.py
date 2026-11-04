"""Build ``solution.ipynb`` from a reviewed list of cells.

Why a generator instead of hand-editing a notebook: the rules for this stage are
awkward to satisfy by hand and easy to check in code - markdown and code in a fixed
order, no heavy logic in a cell, no CSV read, one seed declared once, Persian prose, and
every ``module.attribute`` a cell calls having to exist in the package. :func:`validate`
enforces those, and the file regenerates byte-identically, which is a reproducibility
claim the repository can test rather than one a reader has to trust.

Run from the stage-3 directory::

    python scripts/build_solution_notebook.py            # write solution.ipynb
    python scripts/build_solution_notebook.py --check    # fail if the file differs

The notebook itself is executed where the data is: it reads the views stage 3's loader
filled, so no run of this generator touches a database or the Kaggle file.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import json
import re
from pathlib import Path

NBFORMAT, NBFORMAT_MINOR = 4, 5
KERNEL = "python3"
#: the one seed, declared in the first code cell and nowhere else
SEED = 20261103
#: bounds the *logic* in a cell, counted as non-blank lines including imports; anything
#: longer belongs in src/credit where it can be tested
MAX_CODE_LINES = 16
CELL_ID_PREFIX = "cell"
PERSIAN = re.compile(r"[\u0600-\u06FF]")
#: every ``module.attr`` a cell uses is resolved against the real package
API_PATTERN = re.compile(
    r"\b(columns|cost|evaluate|features|measure|missingness|models|prep|read|report|split|predict|credit_cfg)\.([a-z_][a-z0-9_]*)"
)
MODULE_OF = {
    "columns": "columns",
    "cost": "cost",
    "evaluate": "evaluate",
    "features": "features",
    "measure": "measure",
    "missingness": "missingness",
    "models": "models",
    "prep": "prep",
    "read": "read",
    "report": "report",
    "split": "split",
    "predict": "predict",
}
CREDIT_MODULES = (
    "columns",
    "cost",
    "evaluate",
    "features",
    "measure",
    "missingness",
    "models",
    "prep",
    "read",
    "report",
    "split",
    "predict",
    "config",
    "load",
)


def md(source: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": _lines(source)}


def code(source: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": _lines(source.strip() + "\n"),
    }


def _lines(text: str) -> list[str]:
    parts = text.splitlines(keepends=True)
    return parts if parts else ["\n"]


CELLS: list[tuple[str, str]] = [
    (
        "markdown",
        (
            "# مرحلهٔ ۳ — امتیازدهی اعتباری: هزینهٔ تصمیم، نه دقت\n\n"
            "این نوت‌بوک فقط از دیتابیس می‌خواند (SQLAlchemy، بدون ``read_csv``). فایل خام از همان "
            "خط لولهٔ مرحلهٔ ۱ وارد ``credit.application`` شده و کدهای sentinel در "
            "``credit.v_model_input`` به NULL تبدیل شده‌اند؛ یعنی همان آماده‌سازی روی فایل تازهٔ "
            "ماه بعد اعمال می‌شود.\n\n"
            "هر عددی که در ادامه چاپ می‌شود در ``data/curated/credit_evidence.json`` هم نوشته می‌شود "
            "و ``report.md`` مجاز است فقط از همان اعداد نقل کند (سلول آخر این را چک می‌کند).\n\n"
            "**زمان:** اگر به سقف ۲۰ دقیقه نزدیک شدیم، ``RUN_LIGHT = True`` فولدها را از ۵ به ۳ کم "
            "می‌کند؛ این یک تخمین است، نه عدد اندازه‌گیری‌شده.\n\n"
            "**خروجی‌های این فایل:** از اجرای کامل ``Run All`` روی PostgreSQL ۱۶ و یک دادهٔ fixture "
            "آزمایشی (۱۵۰٬۰۰۰ ردیف تولید‌شده، نه فایل Kaggle) نوشته شده‌اند تا نشان دهند ماشین‌افزار "
            "از سر تا پا کار می‌کند؛ این‌ها نتیجهٔ چالش نیستند. برای اعداد واقعی، همین نوت‌بوک را روی "
            "دیتابیس خودتان با فایل اصلی Run All کنید - خروجی‌های تازه جای همین‌ها را می‌گیرند.\n\n"
            "| # | بخش | معیار پذیرش |\n|---|---|---|\n"
            "| ۱ | ممیزی نشتی زمان-تصمیم | اولین بخش |\n"
            "| ۲ | ساختار، sentinel، گم‌شدگی | سیاست با عدد، ≥۲ سیاست imputation |\n"
            "| ۳ | split و prep | مرز قابل بازرسی |\n"
            "| ۴ | فیچرهای مهندسی‌شده | ≥۴ فیچر، با/بدون اندازه‌گیری شد |\n"
            "| ۵ | بیس‌لاین و دو خانواده | ≥۲۵٪ کاهش هزینه |\n"
            "| ۶ | آستانه از منحنی هزینه | هرگز ۰٫۵، حساسیت ۱۲:۱→۴:۱ |\n"
            "| ۷ | ارزیابی کامل + holdout یک‌بار | ماتریس اختلاط، spread، فاصلهٔ CV |\n"
            "| ۸ | Model Card، importance، fairness | سطر به سطر از عدد |\n"
            "| ۹ | آرتیفکت‌ها | باندل + اسکریپت پیش‌بینی |\n"
        ),
    ),
    (
        "code",
        f"""import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "src"))
import pandas as pd

from credit import columns, cost, db, evaluate, features, measure, missingness, models, prep, read, report, split, tables
from credit.config import load_credit_config
from credit.predict import score_file
from data_factory.manifest import code_version

SEED = {SEED}
RUN_LIGHT = False
engine = db.engine_for(Path("../stage-1/config/default.toml"))
credit_cfg = load_credit_config(Path("config/credit.toml"))
print(f"seed={{SEED}} light={{RUN_LIGHT}} schema={{credit_cfg.schema}} code={{code_version()}}")""",
    ),
    (
        "markdown",
        (
            "## ۰. داده‌ای که می‌خوانیم، از کجا آمده\n\n"
            "پیش از هر تحلیل، ردیف‌های نوت‌بوک باید همان ردیف‌هایی باشند که loader تأیید کرده. "
            "``credit.load_run`` نام فایل، sha256، تعداد بارگذاری‌شده و تعداد رد شده را نگه می‌دارد."
        ),
    ),
    (
        "code",
        """provenance = measure.load_run_provenance(engine, schema=credit_cfg.schema)
rows_view = read.row_count(engine, "input", schema=credit_cfg.schema)
rows_raw = read.row_count(engine, "raw", schema=credit_cfg.schema)
print("| field | value |\\n|---|---|")
for key, value in provenance.items():
    print(f"| `{key}` | {value} |")
print(f"| rows_in_view | {rows_view} |\\n| rows_in_table | {rows_raw} |")
assert rows_view == rows_raw, "the view must not add or drop rows"
assert int(provenance["rows_loaded"]) == rows_view, "load_run and the table disagree"
print(f"provenance ok: {rows_view} rows, sha256 {str(provenance['sha256'])[:12]}")""",
    ),
    (
        "markdown",
        (
            "## ۱. ممیزی نشتی در زمان تصمیم — اولین بخش گزارش\n\n"
            "دو پرسش جدا: آیا فیچر در لحظهٔ تصمیم در دسترس است (نه فقط در دادهٔ تاریخی)، و آیا عددی "
            "که برای ساختش لازم است از ردیف‌های ارزیابی آمده؟ ستون آخر جدول، مرزی است که هر فیچر را "
            "کنترل می‌کند؛ ثابت‌های برازش‌شده هم جداگانه در بخش prep لیست می‌شوند."
        ),
    ),
    (
        "code",
        """audit = features.audit_table()
tables.md_table(print(audit))
unavailable = audit.loc[audit["available before the decision"] == "NO", "feature"].tolist()
print(f"\\nnot available at decision time: {unavailable or 'none'}")
assert not unavailable, "a feature unavailable at decision time cannot be used"
needs_evidence = audit.loc[audit["needs evidence"] != "-", ["feature", "needs evidence"]]
print(f"\\n{tables.md_table(needs_evidence) if len(needs_evidence) else 'no feature claims evidence it does not carry'}")""",
    ),
    (
        "markdown",
        (
            "## ۲. مقادیر غیرعادی: کد یا عدد؟\n\n"
            "همین شمارش مستقل روی جدول خام، در SQL و در پایتون گرفته می‌شود؛ برابریشان یعنی سیاست sentinel "
            "که در view نوشته شده، همان چیزی است که کد هم اعمال می‌کند - و این همان شرط «prep بدون تغییر "
            "روی فایل تازه اعمال می‌شود» است. شمارش از view گرفته نمی‌شود، چون view دقیقاً همان کدها را "
            "NULL می‌کند و صفرِ دیدن از view مدرک نیست. جفت‌هایی که کاوش شدند و چیزی پیدا نشد هم سطر "
            "می‌مانند: صفرِ اندازه‌گیری‌شده مدرک است، سطرِ نبودن نه."
        ),
    ),
    (
        "code",
        """spikes_sql = measure.run_query(engine, "sentinel_spikes", sql_dir=Path("sql"))
raw = read.load_frame(engine, "raw", schema=credit_cfg.schema)
spikes_py = features.sentinel_spike_counts(raw)
tables.md_table(print(spikes_sql))
print(f"\\nsql rows={len(spikes_sql)} python rows={len(spikes_py)}")
tables.md_table(print(spikes_py))
""",
    ),
    (
        "code",
        """# two independent counts of the same thing must agree row for row, not just in length
merged = spikes_sql.merge(
    spikes_py[["column", "code", "rows"]],
    left_on=["column_name", "code"],
    right_on=["column", "code"],
    how="outer",
    indicator=True,
)
assert (merged["_merge"] == "both").all(), "the two sides probed different (column, code) pairs"
counts = merged["rows_with_code"].astype("int64")
assert (counts == merged["rows"].astype("int64")).all(), "SQL and pandas counted different rows"
print(f"\\npolicy: {sorted(columns.SENTINEL_CODES)} -> NULL in the view, kept verbatim in the table")
zeros = int((merged['rows'] == 0).sum())
print(f"probed pairs={len(merged)}, measured zeros={zeros} (a measured zero is evidence; an absent row is not)")""",
    ),
    (
        "code",
        """frame = read.load_frame(engine, "input", schema=credit_cfg.schema)
frame = features.build_features(frame)
print(f"rows={len(frame)} columns={len(frame.columns)} target_rate={frame[columns.VIEW_TARGET_COLUMN].mean():.6f}")
print("\\ndtypes:")
print(tables.md_table(frame.dtypes.to_frame("dtype").reset_index(names="column")))
counts = ["num_real_estate_loans", "num_dependents", "num_late_90"]
print("\\ninteger-valued and numeric-looking, kept as counts: " + ", ".join(counts))
print("why not one-hot: each is ordered and zero is a real value; binning would only cost degrees of freedom")""",
    ),
    (
        "markdown",
        (
            "## ۳. گم‌شدگی: آیا عدد دارد و سیاست را عوض می‌کند؟\n\n"
            "پاسخ این بخش باید سیاست را عوض کند، وگرنه فقط توصیف است. دو سیاست imputation در "
            "``prep.PLANS`` با هم مقایسه می‌شوند و معیارشان هزینهٔ کراس‌والید است، نه شکل توزیع."
        ),
    ),
    (
        "code",
        """parity = measure.run_query(engine, "missingness_parity", sql_dir=Path("sql"))
tables.md_table(print(parity))
profile = missingness.missingness_profile(frame, tuple(columns.MODEL_FEATURE_COLUMNS))
print(f"\\n{tables.md_table(profile)}")
no_income = frame["income_missing"] == 1
a, b = frame[no_income], frame[~no_income]
test = missingness.two_proportion_test(int(a[columns.VIEW_TARGET_COLUMN].sum()), len(a), int(b[columns.VIEW_TARGET_COLUMN].sum()), len(b))
print(f"\\nincome reported vs not: {json.dumps(test, default=str)}")""",
    ),
    (
        "code",
        """splits = split.stratified_split(frame, seed=SEED)
tables.md_table(print(split.boundary_audit(splits)))
feature_names = tuple(spec.name for spec in features.FEATURES)
# prep owns the missingness flags, so the list handed to fit() is what the frame
# already holds: raw model columns plus the engineered ones, never a flag
fit_columns = tuple(columns.MODEL_FEATURE_COLUMNS) + feature_names
fitted = {name: prep.fit(splits.train, plan=plan, columns=fit_columns) for name, plan in prep.PLANS.items()}
train_y = prep.target(splits.train)
for name, stats in fitted.items():
    assert stats.fitted_rows == len(splits.train), f"{name} saw rows outside train"
print(f"\\nplans compared: {sorted(fitted)}")""",
    ),
    (
        "code",
        """chosen = fitted["median+indicator"]
tables.md_table(print(prep.audit(chosen)))
prepared_val = prep.apply(splits.validation, chosen)
left = prep.remaining_missing(prepared_val, list(chosen.feature_names()))
print(f"\\nNaN left after preparation: {int(left['nan_after_prep'].sum())}")
assert int(left["nan_after_prep"].sum()) == 0, "preparation must leave no NaN for the linear model"
print("every number above came from the train split; apply() has no access to any other frame")""",
    ),
    (
        "markdown",
        (
            "## ۴. فیچرهای مهندسی‌شده: هر کدام با یک فرضیه، و اندازه‌گیری با/بدون\n\n"
            "فیچری که هزینه را کم نمی‌کند حذف می‌شود و در گزارش ذکر می‌شود؛ نگه داشتنش «بی‌ضرر» نیست، "
            "چون یک خط ممیزی نشتی و یک ستون مرور اضافه می‌کند."
        ),
    ),
    (
        "code",
        """k = 3 if RUN_LIGHT else 5
folds = split.stratified_folds(splits.train, seed=SEED, k=k)
sweep = []
for name, stats in fitted.items():
    X = prep.apply(splits.train, stats)[list(stats.feature_names())]
    sweep.append(models.cross_validate_cost("logistic", X, train_y, folds, plan=name))
print(f"k={k} folds, plan sweep on the fast family:")
tables.md_table(print(models.compare(sweep)))
best_plan = min(sweep, key=lambda r: r.mean_cost).plan
print(f"\\nbest plan by mean cost: {best_plan}")""",
    ),
    (
        "code",
        """final_stats = fitted[best_plan]
names = list(final_stats.feature_names())
X_train = prep.apply(splits.train, final_stats)[names]
families = []
for family in models.FAMILIES:
    families.append(models.cross_validate_cost(family, X_train, train_y, folds, plan=best_plan))
table = models.compare(sweep + families)
tables.md_table(print(table))
best = models.pick_best(sweep + families)
print(f"\\nselected {best.family} / {best.plan}: mean {best.mean_cost:.2f} std {best.std_cost:.2f} worst {best.worst_cost:.2f}")""",
    ),
    (
        "code",
        """ablation = []
for spec in features.FEATURES:
    kept = [name for name in names if name != spec.name]
    result = models.cross_validate_cost(best.family, prep.apply(splits.train, final_stats)[kept], train_y, folds, plan=f"without {spec.name}")
    ablation.append({"feature": spec.name, "mean_cost": round(result.mean_cost, 3), "delta": round(result.mean_cost - best.mean_cost, 3)})
ablation_frame = pd.DataFrame(ablation).sort_values("delta", ascending=False)
full_cost = next(r.mean_cost for r in families if r.family == best.family)
print(f"full model (all {len(names)} features): cost {full_cost:.3f}\\n")
tables.md_table(print(ablation_frame))""",
    ),
    (
        "markdown",
        (
            "**تصمیم:** فیچری که حذفش هزینهٔ میانگین را بالا نمی‌برد از ماتریس نهایی بیرون می‌ماند. آبلیشن تک‌به‌تک است، پس مجموعهٔ حذف‌ها جدا اندازه‌گیری می‌شود؛ مدل نهایی، آستانه و CVِ گزارش‌شده همه روی همین ماتریس محاسبه می‌شوند - وگرنه «حذف شد» فقط یک جمله در خروجی است."
        ),
    ),
    (
        "code",
        """ablation_rows = int((ablation_frame["delta"] <= 0).sum())
dropped = ablation_frame.loc[ablation_frame["delta"] <= 0, "feature"].tolist()
print(f"\\nfeatures that did not pay and are dropped from the final matrix: {dropped or 'none'}")
kept_names = [name for name in names if name not in dropped]
kept_inputs = tuple(name for name in fit_columns if name not in dropped)
X_kept = prep.apply(splits.train, final_stats)[kept_names]
kept_cv = models.cross_validate_cost(
    best.family, X_kept, train_y, folds, plan=f"{best_plan} without the {len(dropped)}"
)
print(
    f"cv on the kept matrix ({len(kept_names)} columns): mean {kept_cv.mean_cost:.2f}"
    f" +/- {kept_cv.std_cost:.2f}; full matrix was {best.mean_cost:.2f}"
)""",
    ),
    (
        "markdown",
        (
            "## ۵. بیس‌لاین بدون یادگیری، و هزینهٔ زمین\n\n"
            "قبل از هر ادعایی دربارهٔ مدل، کف زمین: «هیچ‌کس را علامت نزن» و «همه را علامت بزن» و یک "
            "قانون سادهٔ بدون مدل. معیار پذیرش ۲۵٪ کاهش نسبت به اولی است."
        ),
    ),
    (
        "code",
        """val_y = prep.target(splits.validation)
baselines = evaluate.baseline_costs(val_y)
rule_flags = (splits.validation["utilization_over_limit"] > 0).astype("int64")
rule = cost.rule_baseline(val_y, rule_flags, name="utilization-over-limit")
entries = {**{name: {"cost_per_1000": value, "counts": {}} for name, value in baselines.items()}, rule["name"]: rule}
for name, entry in entries.items():
    print(f"| baseline {name} | cost {entry['cost_per_1000']:.2f} per 1,000 |")
floor = baselines["flag-nobody"]
print(f"\\nfloor to beat: {floor:.2f}; 25% reduction means < {floor * 0.75:.2f}")""",
    ),
    (
        "markdown",
        (
            "## ۶. آستانه: از منحنی هزینه روی validation\n\n"
            "هرگز ۰٫۵ - آن عدد یک ادعا دربارهٔ برابر بودن هزینهٔ دو خطاست و این ماتریس برابر نیست. "
            "رویهٔ تساوی: آستانهٔ پایین‌تر. حساسیت به ماتریس: اگر false negative چهار واحد به جای دوازده "
            "باشد، آستانه بالا می‌رود و هزینهٔ انتخابی عوض می‌شود - مدل همان می‌ماند، فقط قیمت خطا تغییر "
            "می‌کند."
        ),
    ),
    (
        "code",
        """model = models.FAMILIES[best.family].make(seed=SEED)
model.fit(X_kept, train_y)
val = prep.apply(splits.validation, final_stats)
val_proba = model.predict_proba(val[kept_names])[:, 1]
choice = cost.select_threshold(val_y, val_proba, matrix=cost.CHALLENGE_MATRIX)
print(json.dumps(choice.as_dict(), indent=2, default=str))
curve = cost.cost_curve(val_y, val_proba, matrix=cost.CHALLENGE_MATRIX)
at_half = cost.binary_counts(val_y, (val_proba >= 0.5).astype("int64"))
print(f"\\ncost at the default 0.5: {cost.cost_per_1000(at_half, cost.CHALLENGE_MATRIX, n=len(val_y)):.2f}")
print(f"cost at the chosen {choice.threshold:.3f}: {choice.cost_per_1000:.2f}")""",
    ),
    (
        "code",
        """import matplotlib.pyplot as plt

fig, ax = plt.subplots(figsize=(7, 3))
ax.plot(curve["threshold"], curve["cost_per_1000"], label=cost.CHALLENGE_MATRIX.name)
ax.axvline(choice.threshold, ls="--", label=f"chosen {choice.threshold:.3f}")
ax.axvline(0.5, ls=":", color="tab:red", label="default 0.5")
ax.set_xlabel("threshold")
ax.set_ylabel("expected cost per 1,000")
ax.legend(loc="upper right")
ax.set_title("cost curve, validation split")
plt.tight_layout()
plt.show()""",
    ),
    (
        "code",
        """sensitivity = cost.matrix_sensitivity(val_y, val_proba, [cost.CHALLENGE_MATRIX, cost.FLATTER_MATRIX])
tables.md_table(print(pd.DataFrame(sensitivity)))""",
    ),
    (
        "markdown",
        (
            "## ۷. ارزیابی کامل؛ holdout یک‌بار\n\n"
            "مدل نهایی روی train+validation برازش می‌شود (آستانه از validation آمده) و آنگاه holdout "
            "باز می‌شود - یک بار، بدون بازگشت برای تنظیم دوباره."
        ),
    ),
    (
        "code",
        """combined = pd.concat([splits.train, splits.validation], ignore_index=True)
combined_stats = prep.fit(combined, plan=prep.PLANS[best_plan], columns=kept_inputs)
combined_X = prep.apply(combined, combined_stats)[kept_names]
bundle = models.build_bundle(best.family, combined_X, prep.target(combined), prep_stats=combined_stats, threshold=choice.threshold, matrix=cost.CHALLENGE_MATRIX, seed=SEED, code_version=code_version())
holdout_prepared = prep.apply(splits.holdout, combined_stats)
holdout_proba = bundle.score(holdout_prepared)
holdout_y = prep.target(splits.holdout)
holdout = evaluate.holdout_report(holdout_proba, holdout_y, threshold=bundle.threshold, family=bundle.family, cv=(kept_cv.mean_cost, kept_cv.std_cost))
print(json.dumps(holdout.as_dict(), indent=2, default=str))""",
    ),
    (
        "code",
        """print(f"confusion matrix at t={bundle.threshold:.4f} on {holdout.rows} held-out rows:")
print(f"| | predicted 1 | predicted 0 |\\n|---|---|---|\\n"
      f"| actual 1 | {holdout.counts['tp']} | {holdout.counts['fn']} |\\n"
      f"| actual 0 | {holdout.counts['fp']} | {holdout.counts['tn']} |")
print(f"\\n{evaluate.metrics_table(holdout)}")
print(f"\\ncv mean {holdout.cv_mean_cost:.2f} +/- {holdout.cv_std_cost:.2f} vs holdout {holdout.cost_per_1000:.2f}")
print(f"reduction vs flag-nobody: {holdout.reduction_vs_flagging_nobody_pct:.2f}%")""",
    ),
    (
        "markdown",
        (
            "## ۸. اهمیت فیچرها، Model Card، و fairness\n\n"
            "اهمیت با permutation روی همان هزینهٔ انتخابی سنجیده می‌شود (نه log-loss)، روی validation - "
            "تا holdout فقط یک‌بار مصرف شود."
        ),
    ),
    (
        "code",
        """importance = models.importances(model, val[kept_names], val_y, threshold=choice.threshold, seed=SEED, n_repeats=5)
tables.md_table(print(importance.head(5)))
holdout_flags = (holdout_proba >= bundle.threshold).astype("int64")
bands = evaluate.define_bands(splits.holdout)
fair = evaluate.subgroup_table(splits.holdout.assign(age_band=bands), holdout_y, holdout_flags, holdout_proba)
print(f"\\n{tables.md_table(fair)}")
band_rows = json.loads(fair.to_json(orient="records"))
worst_band = max(band_rows, key=lambda row: row["cost_per_1000"])
best_band = min(band_rows, key=lambda row: row["cost_per_1000"])
print(
    f"\\nworst band {worst_band['age_band']}: {worst_band['cost_per_1000']:.1f} per 1,000 on "
    f"{worst_band['rows']} rows; best band {best_band['age_band']}: {best_band['cost_per_1000']:.1f}"
)
concentration = evaluate.failure_concentration(holdout_proba, holdout_y, threshold=bundle.threshold)
print(f"\\n{json.dumps(concentration, indent=2)}")""",
    ),
    (
        "code",
        """for line in evaluate.where_it_fails(concentration, fair):
    print(line)
card = evaluate.model_card(
    holdout,
    dataset_line=f"{provenance['file_name']} sha256 {str(provenance['sha256'])[:12]}",
    preparation_line=f"{best_plan}, row-wise half in {credit_cfg.schema}.v_model_input",
    feature_lines=[f"`{name}`" for name in bundle.feature_names],
    trained_rows=len(combined),
    code_version=code_version(),
    intended_use="triage of applications into a human review queue",
    excluded_uses=["adverse pricing decided by the model alone", "populations the file does not represent"],
)
print(card)""",
    ),
    (
        "markdown",
        (
            "## ۹. آرتیفکت‌ها\n\n"
            "باندل شامل estimator، ثابت‌های prep و آستانه است؛ اسکریپت پیش‌بینی همین‌ها را مصرف می‌کند، "
            "پس مسیر آموزش و مسیر اسکور یک کد تکراری ندارند."
        ),
    ),
    (
        "code",
        """written = bundle.save(Path("data/curated/model"))
print("\\n".join(f"{key}: {path}" for key, path in written.items()))
fresh = Path("data/raw/cs-validation.csv")
if fresh.is_file():
    scored = score_file(engine, credit_cfg, fresh, bundle)
    print(f"\\n{scored.summary()}")
    tables.md_table(print(scored.scored.head(5)))
else:
    print(f"\\nno fresh file to score; run: python -m credit.predict --file <csv> --bundle data/curated/model")""",
    ),
    (
        "markdown",
        (
            "## ۱۰. evidence\n\n"
            "یک فایل، هر عددی که گزارش مجاز به نقل آن است. ``sort_keys`` اینجا امن است چون مجموعه‌ای از "
            "یافته‌هاست (برخلاف ثابت‌های prep که ترتیبشان همان ترتیب ستون‌های فیچر است)."
        ),
    ),
    (
        "code",
        """evidence = measure.collect(engine, sql_dir=Path("sql"), schema=credit_cfg.schema, extra={
    "splits": splits.sizes,
    "threshold": {"value": choice.threshold, "grid_size": choice.grid_size, "ties": choice.ties, "cost_per_1000": choice.cost_per_1000},
    "plans": {r.plan: {"mean_cost_per_1000": r.mean_cost} for r in sweep},
    "cv": {r.family: r.as_dict() for r in families},
    "ablation": {"full_cost": full_cost, "dropped": len(dropped), "rows": len(ablation_frame)},
    "prep": {"fitted_rows": combined_stats.fitted_rows, "features": len(kept_names), "input_columns": len(kept_inputs)},
    "holdout": holdout.as_dict(),
    "fairness": {"bands": len(band_rows), "max_band": worst_band, "min_band": best_band},
    "sensitivity": {str(row["matrix"]).replace(":", "_"): row for row in sensitivity},
    "importance": {"count": len(importance)},
})
path = measure.write_json(Path("data/curated/credit_evidence.json"), evidence)
print(report.render_numbers_table(evidence, preferred=["threshold.value", "holdout.cost_per_1000", "splits.train", "prep.fitted_rows"]))
print(f"\\nwrote {path}")""",
    ),
    (
        "code",
        """status = report.apply_to_file(Path("report.md"), evidence)
print(f"report.md slots filled: {json.dumps(status)}")
print(report.decisions_table(evidence))""",
    ),
    (
        "markdown",
        (
            "## آنچه این نوت‌بوک ادعا نمی‌کند\n\n"
            "- یک فایل، یک بازار، یک دورهٔ زمانی؛ تعمیم به جمعیت دیگر ادعا نیست.\n"
            "- اثر سیاست sentinel بر هزینهٔ واقعیِ بلندمدت (نه بر این ماتریس) اندازه‌گیری نشده.\n"
            "- اگر ``RUN_LIGHT`` روشن بوده، spread با ۳ فولد گزارش شده و با ۵ فولد قابل مقایسه نیست.\n"
            "- فیچرهای حذف‌شده در جدول ablation آمده‌اند؛ حذف‌شدن یعنی «در این داده هزینه را کم نکرد»، "
            "نه «بی‌معناست».\n"
        ),
    ),
]


def build_notebook() -> dict:
    cells: list[dict] = []
    for position, (kind, source) in enumerate(CELLS, start=1):
        cell = md(source) if kind == "markdown" else code(source)
        cell["id"] = f"{CELL_ID_PREFIX}-{position:03d}"
        cells.append(cell)
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": KERNEL,
            },
            "language_info": {"name": "python"},
        },
        "nbformat": NBFORMAT,
        "nbformat_minor": NBFORMAT_MINOR,
    }


def serialize(notebook: dict) -> str:
    """Deterministic text: the same CELLS produce the same bytes."""
    return json.dumps(notebook, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def _cell_sources(notebook: dict) -> list[tuple[str, str]]:
    """(kind, text) per cell - what the generator owns and a run must not change."""
    return [(cell["cell_type"], "".join(cell["source"])) for cell in notebook["cells"]]


def _was_run(notebook: dict) -> bool:
    """Whether a Run All left its outputs here (half-run is reported by validate)."""
    counts = [
        cell.get("execution_count")
        for cell in notebook.get("cells", [])
        if cell.get("cell_type") == "code"
    ]
    return bool(counts) and all(count is not None for count in counts)


def code_cells(notebook: dict) -> list[str]:
    return [
        "".join(cell["source"])
        for cell in notebook["cells"]
        if cell["cell_type"] == "code"
    ]


def markdown_cells(notebook: dict) -> list[str]:
    return [
        "".join(cell["source"])
        for cell in notebook["cells"]
        if cell["cell_type"] == "markdown"
    ]


def validate(notebook: dict, *, src_root: Path | None = None) -> list[str]:
    """The rules this notebook has to keep, checked instead of reviewed.

    Parsing, line count, the no-CSV rule, one seed, Persian prose, the order cells
    have to run in, and an API check: every ``module.attribute`` a cell names must
    exist in the package, which is what lets a generator-only change be trusted
    without executing the notebook.
    """
    problems: list[str] = []
    sources = code_cells(notebook)
    if not sources:
        return ["no code cells"]
    for position, source in enumerate(sources, start=1):
        try:
            ast.parse(source)
        except SyntaxError as exc:
            problems.append(f"code cell {position} does not parse: {exc}")
            continue
        # imports are excluded: the budget is about logic, and a setup cell has to
        # import the package it is about to call
        lines = [
            line
            for line in source.splitlines()
            if line.strip() and not line.lstrip().startswith(("import ", "from "))
        ]
        if len(lines) > MAX_CODE_LINES:
            problems.append(
                f"code cell {position} has {len(lines)} lines (> {MAX_CODE_LINES})"
            )
        if "read_csv" in source or "read_parquet" in source:
            problems.append(f"code cell {position} reads a file directly")
        sinks = (
            "print",
            "display",
            "plt.show",
            "savefig",
            "to_csv",
            "write_json",
            "bundle.save",
        )
        if "=" in source and not any(sink in source for sink in sinks):
            problems.append(f"code cell {position} computes without printing")
    counts = [
        cell.get("execution_count")
        for cell in notebook["cells"]
        if cell.get("cell_type") == "code"
    ]
    numbered = [count for count in counts if count is not None]
    if numbered and len(numbered) != len(counts):
        # one cell executed and the rest stale carries numbers from a source that no
        # longer exists, which is worse than an empty output nobody reads
        problems.append(
            f"code cells are half-run: {len(numbered)} of {len(counts)} carry an execution count"
        )
    if numbered and sorted(numbered) != list(range(1, len(counts) + 1)):
        problems.append("the execution counts are not the contiguous range 1..N")
    if f"SEED = {SEED}" not in sources[0]:
        problems.append("the first code cell must declare the single seed")
    if sum("SEED = " in source for source in sources) != 1:
        problems.append("SEED is assigned in more than one cell")
    for position, source in enumerate(markdown_cells(notebook), start=1):
        if not PERSIAN.search(source):
            problems.append(f"markdown cell {position} has no Persian text")
    problems.extend(_check_scopes(sources))
    problems.extend(
        _check_apis(notebook, src_root or Path(__file__).resolve().parents[1] / "src")
    )
    return problems


def _bound_names(tree: ast.AST) -> set[str]:
    """The names a cell hands to the cells after it."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(
                (alias.asname or alias.name.split(".")[0]) for alias in node.names
            )
        elif isinstance(
            node,
            (ast.ExceptHandler, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            if node.name:  # `except OSError:` binds nothing
                names.add(node.name)
        elif isinstance(node, ast.Lambda):
            # a lambda's parameter is bound in the cell's own namespace, unlike a def's
            names.update(arg.arg for arg in node.args.args)
        elif isinstance(node, (ast.comprehension, ast.NamedExpr)):
            names.update(
                sub.id for sub in ast.walk(node.target) if isinstance(sub, ast.Name)
            )
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            names.update(
                sub.id
                for sub in ast.walk(node.optional_vars)
                if isinstance(sub, ast.Name)
            )
    return names


def _check_scopes(sources: list[str]) -> list[str]:
    """No cell may read a name an earlier cell has not already made.

    A notebook is one namespace advanced cell by cell, so a name defined two cells
    down is not a style problem but a NameError at the end of a multi-minute run -
    and the ordering mistake that a per-cell syntax check cannot see.
    """
    problems: list[str] = []
    known = set(dir(builtins))
    for position, source in enumerate(sources, start=1):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue  # the parse check above already reported it
        bound_here = _bound_names(tree)
        loaded = {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        missing = sorted(loaded - known - bound_here)
        if missing:
            problems.append(
                f"code cell {position} reads names no earlier cell made: {', '.join(missing)}"
            )
        known |= bound_here
    return problems


def _outside_strings(source: str) -> str:
    """The cell's code with its string literals blanked, for identifier scanning."""
    blanked = re.sub(r'""".*?"""', " ", source, flags=re.DOTALL)
    return re.sub(r"('[^']*'|\"[^\"]*\")", " ", blanked)


def _module_names(module: Path) -> set[str]:
    """Top-level names a module defines, read from its source."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.Assign):
            found.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            found.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(
                (alias.asname or alias.name).split(".")[0] for alias in node.names
            )
    return found


def _check_apis(notebook: dict, src_root: Path) -> list[str]:
    """Every ``module.attr`` a cell names must be defined in that module.

    Parsed from source rather than imported, so the check runs on a machine that has
    no PostgreSQL, no stage-1 install and no Kaggle file - which is every machine that
    regenerates the notebook. An attribute that comes from a re-export (``credit.read``
    importing ``pandas``) is a false positive risk, so unresolved names are reported as
    warnings, and a missing source directory is a problem rather than a silence.
    """
    credit_dir = src_root / "credit"
    if not credit_dir.is_dir():
        return [f"api check found no package to read: {credit_dir}"]
    names = {
        module.stem: _module_names(module) for module in sorted(credit_dir.glob("*.py"))
    }
    problems: list[str] = []
    for position, source in enumerate(code_cells(notebook), start=1):
        code_only = _outside_strings(source)
        for owner, attribute in sorted(set(API_PATTERN.findall(code_only))):
            if owner == "credit_cfg":
                continue  # a config object's own fields, not a module attribute
            defined = names.get(owner)
            if defined is None:
                problems.append(f"code cell {position}: no credit module named {owner}")
            elif attribute not in defined:
                problems.append(
                    f"code cell {position}: {owner}.{attribute} is not defined in {owner}.py"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/build_solution_notebook.py")
    parser.add_argument("--out", default="solution.ipynb")
    parser.add_argument(
        "--check", action="store_true", help="fail if the file differs from the spec"
    )
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)

    notebook = build_notebook()
    problems = validate(notebook)
    text = serialize(notebook)
    target = Path(args.out)
    if problems:
        print("notebook shape problems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    if args.validate_only:
        print(
            f"ok: {len(notebook['cells'])} cells, {len(code_cells(notebook))} code cells"
        )
        return 0
    if args.check:
        if not target.is_file():
            print(f"missing {target}")
            return 1
        delivered = json.loads(target.read_text(encoding="utf-8"))
        if _cell_sources(delivered) != _cell_sources(notebook):
            print(f"{target} is stale; run python scripts/build_solution_notebook.py")
            return 1
        delivered_problems = validate(delivered)
        if delivered_problems:
            print(f"notebook shape problems in {target}:")
            for problem in delivered_problems:
                print(f"  - {problem}")
            return 1
        if not _was_run(delivered) and target.read_text(encoding="utf-8") != text:
            # an unexecuted notebook has nothing but the generator's bytes to justify it
            print(
                f"{target} differs from the generator outside the cells; regenerate it"
            )
            return 1
        state = (
            "executed: outputs saved, cells numbered"
            if _was_run(delivered)
            else "pristine"
        )
        print(f"ok: {target} matches the generator ({state})")
        return 0
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target} ({len(notebook['cells'])} cells)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
