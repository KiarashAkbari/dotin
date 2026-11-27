<!--
  The cell bodies of `02_system.ipynb`, as data. `scripts/build_system_notebook.py` reads this
  file and executes the notebook in a fresh Jupyter kernel (Restart & Run All). Prose is Persian and
  lives here because the layout guard forbids Persian in a .py source. Cell kinds: `%% md` and
  `%% code`.
-->

%% md
# 02 — سامانهٔ هشدار زودهنگام: روایت سامانهٔ نهایی

این نوتبوک چیزی را آموزش نمی‌دهد؛ فقط آنچه **یک فرمان** (`scripts/run_chain.py`) ساخته و در دیتابیس
گذاشته را می‌خواند و نشان می‌دهد: زمان هر مرحله، ویژگی‌ها، انتخاب مدل، ارزیابی در نقطهٔ عملیاتی،
جابه‌جایی داده، منبع بیرونی، هزینهٔ نشت و فهرست نهایی. هر عدد این‌جا از همان اجرا می‌آید.

- برچسب: بدترین امتیاز نظرِ سفارش ≤ ۲ (`capstone.v_order_label`).
- لحظهٔ تصمیم: `order_approved_at`. تقسیم: زمانی، روی `split.boundary`.

%% code
import json, os, subprocess, sys
from pathlib import Path

STAGE = Path.cwd()
if not (STAGE / "src" / "capstone").is_dir():
    STAGE = Path(os.environ.get("CAPSTONE_STAGE_DIR", STAGE)).resolve()
sys.path[:0] = [str(STAGE / "src"), str(STAGE.parent / "stage-1-kiarash-akbari" / "src")]

import pandas as pd
import matplotlib.pyplot as plt
from IPython.display import display, Markdown

from capstone.cli import default_config_path
from capstone.config import load_config
from capstone.features import connect, database_uri

CONFIG = load_config(default_config_path(STAGE))
SEED = CONFIG.model.random_seed  # the one seed the model layer uses; this notebook trains nothing itself
CURATED = CONFIG.features.curated_dir
if os.environ.get("CAPSTONE_NOTEBOOK_RUN_CHAIN") == "1":
    done = subprocess.run([sys.executable, str(STAGE / "scripts" / "run_chain.py"),
                           "--manifest", str(CURATED / "chain_manifest_notebook.json")],
                          cwd=STAGE, capture_output=True, text=True)
    print(done.returncode, done.stderr[-500:])
print("config:", CONFIG.source.name, "| boundary:", CONFIG.split.boundary, "| quota:", CONFIG.capacity.share)

%% md
## ۱. اجرای زنجیره و زمان هر مرحله (بودجه: ۴۵ دقیقه)

%% code
manifests = sorted(CURATED.glob("chain_manifest*.json"), key=lambda p: p.stat().st_mtime)
manifest_path = manifests[-1] if manifests else STAGE / "docs/evidence/chain-run-manifest.json"
manifest = json.loads(manifest_path.read_text())
timing = pd.DataFrame(manifest["layers"])[["name", "seconds", "status"]]
display(timing)
print(f"total {manifest['seconds_used']:.1f} s of {manifest['budget_seconds']} s budget -> {manifest['total']['status']}  ({manifest_path.name})")

%% md
## ۲. ویژگی‌ها: هر ستون، منبعش، چرا در لحظهٔ تأیید موجود است، و فرضیه‌اش

%% code
hyp = pd.read_csv(STAGE / "docs/feature_hypotheses.csv")
pd.set_option("display.max_colwidth", 90)
display(hyp)
conn = connect(database_uri(CONFIG), purpose="read the system's results")
cur = conn.cursor()
cur.execute("SELECT count(*) FROM capstone.v_feature_frame")
print("frame rows:", cur.fetchone()[0], "| features:", len(hyp) - 1, "+ 1 external")

%% md
## ۳. مدل: خط مبنا، دو خانواده، انتخاب روی اعتبارسنجیِ داخل پنجرهٔ آموزش

%% code
cur.execute("SELECT run_id, model, metrics FROM capstone.model_run ORDER BY created_at DESC LIMIT 1")
RUN_ID, MODEL, M = cur.fetchone()
display(pd.DataFrame({k: {kk: vv for kk, vv in v.items() if kk != "months"} for k, v in M["split"].items() if isinstance(v, dict)}).T)
print("run:", RUN_ID, "| chosen:", MODEL)
display(pd.DataFrame(M["selection"]))
rule = M["rule_baseline"]
display(Markdown(f"**قاعدهٔ خط مبنا** (انتخاب‌شده روی اعتبارسنجی): پیگیری سفارش‌هایی با «{rule['chosen']['rule']}» (`{rule['chosen']['column']}`)"))
display(pd.DataFrame(rule["tried"]))

%% md
## ۴. ارزیابی روی ماه‌های بعد از مرز — جدول ظرفیت (ابزار تصمیم مدیر)

%% code
ev = M["evaluation"][MODEL]
rows = []
for name, block in [("rule: " + rule["chosen"]["column"], rule["evaluation"])] + [(k, v) for k, v in M["evaluation"].items()]:
    cap = {r["share"]: r["caught_share"] for r in block["capacity"]}
    rows.append({"model": name, "recall@5%": cap[0.05], "recall@10%": cap[0.1], "recall@20%": cap[0.2],
                 "precision@10%": block["operating_point"]["precision"], "F1@10%": block["operating_point"]["f1"],
                 "AUC": block["auc"], "AP": block["average_precision"]})
table = pd.DataFrame(rows).set_index("model")
display(table.round(4))
display(pd.DataFrame(ev["capacity"]).assign(denominator="labelled orders"))
display(pd.DataFrame(ev["capacity_all_approved"]).assign(denominator="all approved orders"))
print("model - rule, recall@10%, 95% bootstrap interval:", M["model_vs_rule_recall_delta_ci95"])

%% code
op = ev["operating_point"]
cm = pd.DataFrame([[op["tp"], op["fn"]], [op["fp"], op["tn"]]],
                  index=["actual negative review", "actual not negative"],
                  columns=["in the 10% list", "not in the list"])
display(Markdown("**Confusion matrix at the operating point (top 10%)**"))
display(cm)
print({k: round(op[k], 4) for k in ("precision", "recall", "f1")}, "AUC", round(ev["auc"], 4))

%% md
### پراکندگی در ماه‌های ارزیابی و معیار دوم (آخرین ماه)

%% code
months = pd.DataFrame(ev["months"])
display(months)
fig, ax = plt.subplots(figsize=(6, 3))
ax.bar(months["month"], months["recall"], color="#4c72b0")
ax.axhline(op["recall"], color="black", ls="--", label="whole window")
ax.set_ylabel("recall @10% (re-ranked in month)"); ax.legend(); ax.set_title("monthly spread")
plt.tight_layout(); plt.show()
judged = M["drift_vs_model"]
last = judged["last_full_month"]
print(f"months not judged (< {judged['min_month_labelled']} labelled orders): {judged['months_not_judged']}")
print(f"second metric -- last judged evaluation month {last['month']}: recall {last['recall']:.4f}, "
      f"AUC {last['auc']:.4f} on {last['labelled']} labelled orders; monthly range {judged['recall_range']:.4f}")

%% md
## ۵. جابه‌جایی داده (Drift): پنجرهٔ آموزش در برابر پنجرهٔ ارزیابی

%% code
drift = pd.DataFrame(M["drift"])
display(drift.head(12).round(4))
nr = pd.DataFrame(M["negative_rate_by_month"])
fig, axes = plt.subplots(1, 2, figsize=(11, 3))
axes[0].plot(nr["month"], nr["negative_rate"], marker="o")
axes[0].axvline(str(CONFIG.split.boundary)[:7], color="red", ls="--")
axes[0].set_title("negative-review rate by approval month"); axes[0].tick_params(axis="x", rotation=90)
top = drift.head(8)
axes[1].barh(top["feature"], top["psi"]); axes[1].invert_yaxis(); axes[1].set_title("PSI train -> evaluation")
plt.tight_layout(); plt.show()

%% md
## ۶. منبع بیرونی (IBGE، چگالی جمعیت ۲۰۱۰): با و بدون

%% code
ext = M["external"]
display(pd.DataFrame([{k: v for k, v in ext.items() if k not in ("negative_rate_by_density_quartile", "eval_recall_delta_ci95")}]).T)
print("eval recall@10% delta (with - without), 95% bootstrap:", ext.get("eval_recall_delta_ci95"))
q = pd.DataFrame(ext.get("negative_rate_by_density_quartile", []))
if not q.empty:
    display(q)
    fig, ax = plt.subplots(figsize=(5, 3)); ax.bar(q["quartile"], q["negative_rate"]); ax.set_title("negative rate by density quartile (train)")
    plt.tight_layout(); plt.show()

%% md
## ۷. هزینهٔ نشت: مدل تقلبی با ستون‌های وسوسه‌کننده

%% code
print((STAGE / "docs/evidence/cheating-model-gap.txt").read_text())

%% md
## ۸. خروجی عملیاتی و مسیر پیش‌بینی

%% code
followup = pd.read_csv(CURATED / "followup_list.csv")
print("follow-up list:", len(followup), "orders (top", CONFIG.capacity.share, "of the evaluation window)")
display(followup.head(10))
print((STAGE / "docs/evidence/sealed-cut-rehearsal.txt").read_text())

%% md
## ۹. دو شاهدِ دیگرِ گزارش: زمانِ کوئریِ ویژگی (سقف ۵۰۰ ms، با پلن) و حساسیتِ مرز

هر دو را اسکریپت‌هایی جدا از زنجیره نوشته‌اند (`scripts/analyze_ceiling_feature.py --tag …` و
`scripts/boundary_sensitivity.py`)؛ این‌جا فقط خوانده می‌شوند تا هر عددِ `report.md` در نوتبوک هم دیده شود.
زمانِ قاب روی این دیتابیس هم یک بار دوباره گرفته می‌شود (`features.FRAME_TIMING_SQL`، بهترینِ پنج اجرا).

%% code
import time
from capstone import features
ceiling = sorted((STAGE / "docs/evidence").glob("ceiling-feature-analysis.sandbox-*.txt"))[-1]
print(ceiling.name)
print("\n".join(line for line in ceiling.read_text().splitlines()[:8] if line.strip()))
runs = []
for _ in range(5):
    start = time.perf_counter(); cur.execute(features.FRAME_TIMING_SQL.format(frame=features.FRAME_VIEW)); cur.fetchall()
    runs.append((time.perf_counter() - start) * 1000)
print(f"this database, now: best {min(runs):.1f} ms of 5 (ceiling {CONFIG.features.feature_query_ms} ms)")
print((STAGE / "docs/evidence/boundary-sensitivity.txt").read_text())
conn.close()
