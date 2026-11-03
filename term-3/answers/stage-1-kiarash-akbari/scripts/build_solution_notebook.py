"""Builds solution.ipynb (thin notebook); execute it afterwards with Restart & Run All."""
import json
from pathlib import Path

CELLS = []


def md(text):
    CELLS.append({"cell_type": "markdown", "metadata": {}, "source": text.strip().splitlines(True)})


def code(text):
    CELLS.append(
        {
            "cell_type": "code",
            "metadata": {},
            "execution_count": None,
            "outputs": [],
            "source": text.strip().splitlines(True),
        }
    )


md("""
# تحلیل اکتشافی: زمان بستن شکایت‌های نویز ۳۱۱ نیویورک

**سؤال:** یک شکایت نویز چقدر طول می‌کشد تا بسته شود، و این زمان به نوع شکایت، محله و زمان ثبت چطور بستگی دارد؟

تمام محاسبه‌ها در `data_factory.analysis` است و این نوت‌بوک فقط آن‌ها را صدا می‌زند و نتیجه را روایت می‌کند.
هر عددی که در متن آمده از خروجی همین نوت‌بوک قابل ردیابی است.
""")

md("""
## ۰. اجرای کارخانه و شواهد مهندسی

اگر خروجی‌های `data/curated/` موجود نباشند، کارخانه با `python -m data_factory run` اجرا می‌شود (روی clone تازه شامل دانلود حدود ۱٫۰۸ میلیون رکورد است).
عددهای گزارش (تعداد خام، قرنطینه، join، بودجه‌ی درخواست) از `run_manifest.json` همین اجرا خوانده و در همین بخش چاپ می‌شوند.
""")

code("""
import subprocess
import sys
from pathlib import Path

CASES = Path("data/curated/cases.csv")
if not CASES.is_file():
    proc = subprocess.run(
        [sys.executable, "-m", "data_factory", "run"],
        capture_output=True, text=True, check=False,
    )
    print(proc.stdout[-2000:])
    print(proc.stderr[-2000:])
    proc.check_returncode()
CASES.is_file()
""")

code("""
import hashlib
import json

from data_factory.config import load_config

cfg = load_config("config/default.toml")
manifest = json.loads(Path(cfg.paths.manifest_path).read_text(encoding="utf-8"))
keys = ("code_version", "window", "restriction", "records", "join", "request_budget", "requests_this_run")
print(json.dumps({k: manifest[k] for k in keys}, indent=2, ensure_ascii=False))
""")

code("""
def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


{name: sha256(info["path"]) == info["sha256"] for name, info in manifest["outputs"].items()}
""")

code("""
quality = json.loads(Path("data/curated/data_quality_report.json").read_text(encoding="utf-8"))
print(json.dumps(quality, indent=1, ensure_ascii=False)[:2500])
""")

code("""
RERUN_CHECK = False  # یک بار True کن، اجرا کن و خروجی را ذخیره کن؛ چند دقیقه طول می‌کشد
if RERUN_CHECK:
    first = {n: i["sha256"] for n, i in manifest["outputs"].items()}
    subprocess.run([sys.executable, "-m", "data_factory", "run"], check=True, capture_output=True, text=True)
    again = json.loads(Path(cfg.paths.manifest_path).read_text(encoding="utf-8"))
    second = {n: i["sha256"] for n, i in again["outputs"].items()}
    print({"requests_second_run": again["requests_this_run"], "hashes": "SAME" if first == second else "DIFFERENT"})
""")

code("""
from pathlib import Path

from data_factory import analysis as a

SEED = a.SEED
df = a.load_cases(Path("data/curated/cases.csv"))
df.shape
""")

md("""
## ۱. توزیع مدت زمان تا بسته‌شدن

**چه چیزی را می‌سنجیم:** شکل توزیع مدت (ساعت) و اینکه کدام شاخص مرکزی معنادار است.
""")

code("""
a.duration_summary(df).round(3)
""")

code("""
_ = a.plot_duration_histogram(df)
""")

md("""
**نتیجه:** توزیع به‌شدت راست‌چوله است. میانه حدود ۱٫۰۹ ساعت است، ولی میانگین حدود ۲٫۶ ساعت است و بیشینه به ۱۹۲۹ ساعت می‌رسد.
پس میانگین را برای گزارش مرکز توزیع به کار نمی‌بریم و از **میانه** و مقیاس لگاریتمی استفاده می‌کنیم.
""")

md("""
## ۲. شناسایی و تعیین تکلیف Outlier

**روش:** حصار Tukey روی `log1p(ساعت)`، چون روی مقیاس خام، دم بلند توزیع تقریباً همه را پرت می‌کند.
""")

code("""
fences = a.tukey_fences(df["duration_hours"])
fences["share_above"] = round(fences["above"] / fences["total"], 4)
fences
""")

md("""
**نتیجه:** حد بالا حدود ۱۳٫۳ ساعت است و حدود ۲٫۷٪ پرونده‌ها بالاتر از آن‌اند. حد پایین منفی می‌شود، پس پرت پایینی نداریم.

**تصمیم (فرض، نیازمند تأیید):** این پرونده‌ها **نگه داشته می‌شوند**.
دلیل: قرارداد داده پیش‌تر مقدارهای نامعتبر (مثل بسته‌شدن قبل از ثبت) را قرنطینه کرده است؛ آنچه مانده پرونده‌ی واقعی است و کندیِ واقعی را نشان می‌دهد.
حذفشان ۲٫۷٪ از پرونده‌های واقعاً کند را از تحلیل بیرون می‌برد و نتیجه را خوش‌بینانه می‌کند.
به‌جای حذف، از میانه و مقیاس لگاریتمی استفاده می‌کنیم که به دم بلند حساس نیستند.
**هزینه‌ی این انتخاب:** هر آماره‌ای که میانگین بگیرد (مثلاً میانگین مدت) تحت‌تأثیر این پرونده‌ها خواهد بود و در این نوت‌بوک گزارش نمی‌شود.
""")

md("""
## ۳. مقایسه‌ی دسته‌ای: نوع شکایت و بخش شهر

**چه چیزی را می‌سنجیم:** میانه‌ی مدت بستن در هر گروه. گروه‌های با کمتر از ۵۰۰ پرونده کنار گذاشته شده‌اند تا میانه ناپایدار نباشد.
""")

code("""
by_type = a.median_by(df, "complaint_type")
by_type.round(3)
""")

code("""
_ = a.plot_median_by(by_type, "Median hours to close by complaint type")
""")

md("""
**نتیجه:** شکایت‌های «Noise - Residential» کندترین‌اند (میانه حدود ۱٫۲۲ ساعت) و «House of Worship» سریع‌ترین (حدود ۰٫۳۷ ساعت، فقط ۴٬۷۲۶ پرونده).
تفاوت نوع‌ها قابل‌توجه است و حدود سه برابر.
""")

code("""
by_borough = a.median_by(df, "borough")
by_borough.round(3)
""")

code("""
_ = a.plot_median_by(by_borough, "Median hours to close by borough")
""")

md("""
**نتیجه:** برانکس کندترین بخش است (میانه حدود ۱٫۵۵ ساعت) و منهتن سریع‌ترین (حدود ۰٫۸۸ ساعت).
این یک مقایسه‌ی توصیفی است و علت آن (تعداد مأمور، حجم شکایت، نوع شکایت) را نشان نمی‌دهد.
""")

md("""
## ۴. روند در طول زمان

**چه چیزی را می‌سنجیم:** میانه‌ی ماهانه‌ی مدت بستن بر اساس ماه ثبت.
""")

code("""
trend = a.monthly_median(df)
_ = a.plot_monthly_trend(trend, "Median hours to close by month")
""")

code("""
trend.round(3)
""")

md("""
**نتیجه:** میانه در ژانویه ۲۰۲۵ بالاترین مقدار را دارد (حدود ۲٫۸ ساعت)، تا ژوئن ۲۰۲۵ به حدود ۰٫۷۷ ساعت می‌رسد و دوباره تا حدود ۱٫۳ ساعت در دسامبر ۲۰۲۵ و ژوئن ۲۰۲۶ بالا می‌رود.

**محدودیت (فرض):** اگر فایل فقط پرونده‌های بسته‌شده را دارد، ماه‌های آخر ممکن است کوتاه‌تر از واقع به نظر برسند، چون پرونده‌های کند هنوز بسته نشده‌اند.
این اثر در این نوت‌بوک اندازه‌گیری نشده و باید جداگانه بررسی شود.
""")

md("""
## ۵. نرخ شکایت نسبت به جمعیت منطقه

**چرا نرخ:** تعداد خام شکایت بین منطقه‌ی کوچک و پرجمعیت قابل‌مقایسه نیست؛ مخرج جمعیتی لازم است.
رکوردهای بدون منطقه یا جمعیت از این جدول کنار گذاشته می‌شوند.
""")

code("""
rates = a.area_rates(df)
top = rates.head(10).round(2)
top
""")

code("""
_ = a.plot_area_rates(rates)
""")

code("""
leader = rates.iloc[0]
{
    "zip": rates.index[0],
    "complaints": int(leader["complaints"]),
    "share_of_all_cases": round(leader["complaints"] / len(df), 4),
    "second_per_10k": round(rates.iloc[1]["per_10k"], 1),
}
""")

md("""
**نتیجه:** منطقه‌ی 10466 با ۱۳۴٬۸۰۳ شکایت برای ۷۲٬۴۷۹ نفر، نرخ حدود ۱۸٬۵۹۹ در ده‌هزار نفر دارد؛ یعنی بیش از یک شکایت به‌ازای هر نفر، و حدود ۱۲٫۴٪ کل پرونده‌ها.
نفر دوم (11366) حدود ۷٬۷۶۱ است، پس 10466 حدود ۲٫۴ برابر منطقه‌ی بعدی است.

**فرض (اندازه‌گیری‌نشده):** این مقدار احتمالاً از شاکی‌های تکراری یا یک آدرس پرتکرار می‌آید. این فرض را نمی‌توان با ستون‌های موجود تأیید کرد و باید با ستون آدرس داده‌ی خام بررسی شود.
این را یک یافته‌ی کیفیت داده می‌دانیم، نه یک نتیجه‌ی رفتاری درباره‌ی ساکنان آن منطقه.

مناطق با جمعیت بسیار کم (مثل 10004 با ۳٬۰۲۸ نفر) هم نرخ ناپایدار دارند، چون مخرج کوچک است.
""")

md("""
## ۶. الگوی ساعت و روز هفته

**چه چیزی را می‌سنجیم:** تعداد شکایت‌ها در هر ساعت و روز هفته.
""")

code("""
counts = a.hour_weekday_counts(df)
_ = a.plot_hour_weekday(counts)
""")

code("""
hourly = counts.sum()
night_hours = [21, 22, 23, 0, 1]
{
    "peak_hour": int(hourly.idxmax()),
    "peak_count": int(hourly.max()),
    "low_hour": int(hourly.idxmin()),
    "low_count": int(hourly.min()),
    "night_share_21_to_01": round(hourly[night_hours].sum() / hourly.sum(), 4),
}
""")

md("""
**نتیجه:** اوج شکایت‌ها ساعت ۲۲ است و کمترین مقدار حدود ساعت ۶ تا ۷. ساعت‌های ۲۱ تا ۱ بامداد حدود ۴۳٫۸٪ کل شکایت‌ها را دارند.
""")

md("""
## ۷. رابطه‌ی متغیرهای عددی

**روش:** همبستگی Spearman (رتبه‌ای)، چون مدت بستن چوله است. برای سرعت روی نمونه‌ی ۲۰۰٬۰۰۰ تایی با seed ثابت محاسبه می‌شود.
""")

code("""
a.spearman(df).round(3)
""")

md("""
**نتیجه:** همه‌ی ضریب‌ها ضعیف‌اند و بزرگ‌ترین قدر مطلق ۰٫۰۶۱ است (مدت و جمعیت). ساعت ثبت، روز هفته و جمعیت منطقه به‌تنهایی مدت بستن را توضیح نمی‌دهند.
""")

md("""
## ۸. یافته‌های غیربدیهی و محدودیت‌ها

**یافته‌ها**

1. یک منطقه (10466) به‌تنهایی حدود ۱۲٫۴٪ کل پرونده‌ها را دارد و نرخش حدود ۲٫۴ برابر منطقه‌ی دوم است؛ این یک نکته‌ی کیفیت داده است که تحلیل‌های بعدی باید حساب کنند.
2. کندی مدت بستن به نوع و محل شکایت بسته است (برانکس و شکایت‌های مسکونی کندترند) نه به ساعت یا روز هفته؛ ضریب‌های Spearman نزدیک صفرند.
3. شکایت‌ها شبانه متمرکزند (حدود ۴۳٫۸٪ بین ساعت ۲۱ و ۱) ولی مدت بستن تقریباً به ساعت ثبت وابسته نیست.

**محدودیت‌ها**

- میانه فقط رفتار مرکزی را نشان می‌دهد و علت را توضیح نمی‌دهد.
- اثر احتمالی پرونده‌های بازِ ماه‌های آخر روی روند اندازه‌گیری نشده است.
- علت تمرکز شکایت در 10466 فرض است و تأیید نشده.
- همبستگی Spearman فقط وابستگی یکنوا را می‌سنجد.
""")

for index, cell in enumerate(CELLS):
    cell["id"] = f"cell-{index:02d}"

notebook = {
    "cells": CELLS,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.12"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

out = Path(__file__).resolve().parent.parent / "solution.ipynb"
out.write_text(json.dumps(notebook, indent=1, ensure_ascii=False), encoding="utf-8")
print("wrote", out)