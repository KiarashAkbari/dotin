"""Exploratory analysis helpers; the notebook only imports and narrates them."""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.figure import Figure

SEED = 42
SECONDS_PER_HOUR = 3600
RATE_BASE = 10_000
CORRELATION_SAMPLE = 200_000
MIN_GROUP_SIZE = 500
TOP_N = 15
QUANTILES = (0.5, 0.75, 0.9, 0.95, 0.99)
FENCE_FACTOR = 1.5
HISTOGRAM_BINS = 80
CASE_COLUMNS = (
    "created_at",
    "duration_seconds",
    "is_ambiguous",
    "complaint_type",
    "descriptor",
    "borough",
    "modzcta",
    "population",
)
CATEGORY_COLUMNS = ("complaint_type", "descriptor", "borough", "modzcta")
CORRELATION_COLUMNS = ("duration_hours", "population", "hour", "weekday")


def load_cases(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(
        path,
        usecols=CASE_COLUMNS,
        dtype={column: "category" for column in CATEGORY_COLUMNS},
        parse_dates=["created_at"],
    )
    frame["duration_hours"] = frame["duration_seconds"] / SECONDS_PER_HOUR
    frame["month"] = frame["created_at"].dt.to_period("M").astype(str)
    frame["hour"] = frame["created_at"].dt.hour
    frame["weekday"] = frame["created_at"].dt.dayofweek
    return frame


def duration_summary(frame: pd.DataFrame) -> pd.Series:
    return frame["duration_hours"].describe(percentiles=list(QUANTILES))


def tukey_fences(values: pd.Series) -> dict[str, float | int]:
    """Tukey fences on log1p(hours); durations are strongly right-skewed."""
    logged = np.log1p(values.dropna())
    q1, q3 = logged.quantile([0.25, 0.75])
    spread = q3 - q1
    low, high = q1 - FENCE_FACTOR * spread, q3 + FENCE_FACTOR * spread
    return {
        "low_hours": float(np.expm1(low)),
        "high_hours": float(np.expm1(high)),
        "below": int((logged < low).sum()),
        "above": int((logged > high).sum()),
        "total": int(logged.size),
    }


def median_by(
    frame: pd.DataFrame, column: str, min_size: int = MIN_GROUP_SIZE
) -> pd.DataFrame:
    grouped = frame.groupby(column, observed=True)["duration_hours"].agg(["median", "count"])
    return grouped[grouped["count"] >= min_size].sort_values("median", ascending=False)


def monthly_median(frame: pd.DataFrame) -> pd.Series:
    return frame.groupby("month")["duration_hours"].median()


def monthly_median_by(frame: pd.DataFrame, column: str) -> pd.DataFrame:
    return (
        frame.groupby(["month", column], observed=True)["duration_hours"]
        .median()
        .unstack(column)
    )


def area_rates(frame: pd.DataFrame) -> pd.DataFrame:
    known = frame.dropna(subset=["modzcta", "population"])
    grouped = known.groupby("modzcta", observed=True).agg(
        complaints=("duration_hours", "size"), population=("population", "first")
    )
    grouped["per_10k"] = grouped["complaints"] / grouped["population"] * RATE_BASE
    return grouped.sort_values("per_10k", ascending=False, kind="stable")


def hour_weekday_counts(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.groupby(["weekday", "hour"]).size().unstack(fill_value=0)


def spearman(frame: pd.DataFrame) -> pd.DataFrame:
    size = min(len(frame), CORRELATION_SAMPLE)
    sample = frame.sample(size, random_state=SEED)
    return sample[list(CORRELATION_COLUMNS)].corr(method="spearman")


def plot_duration_histogram(frame: pd.DataFrame) -> Figure:
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(np.log10(frame["duration_seconds"].dropna() + 1), bins=HISTOGRAM_BINS)
    ax.set_xlabel("log10(seconds to close + 1)")
    ax.set_ylabel("cases")
    ax.set_title("Time to close a case")
    return fig


def plot_median_by(table: pd.DataFrame, title: str) -> Figure:
    fig, ax = plt.subplots(figsize=(8, max(3, 0.35 * len(table))))
    table["median"].sort_values().plot.barh(ax=ax)
    ax.set_xlabel("median hours to close")
    ax.set_title(title)
    return fig


def plot_monthly_trend(trend: pd.Series | pd.DataFrame, title: str) -> Figure:
    fig, ax = plt.subplots(figsize=(9, 4))
    trend.plot(ax=ax)
    ax.set_xlabel("month of creation")
    ax.set_ylabel("median hours to close")
    ax.set_title(title)
    ax.tick_params(axis="x", rotation=60)
    return fig


def plot_area_rates(rates: pd.DataFrame, top_n: int = TOP_N) -> Figure:
    top = rates.head(top_n)
    fig, ax = plt.subplots(figsize=(8, 5))
    top["per_10k"].sort_values().plot.barh(ax=ax)
    ax.set_xlabel("complaints per 10,000 residents")
    ax.set_title(f"Top {top_n} areas by complaint rate")
    return fig


def plot_hour_weekday(counts: pd.DataFrame) -> Figure:
    fig, ax = plt.subplots(figsize=(9, 4))
    image = ax.imshow(counts.to_numpy(), aspect="auto")
    ax.set_xlabel("hour of day")
    ax.set_ylabel("weekday (0 = Monday)")
    ax.set_title("Complaints by weekday and hour")
    fig.colorbar(image, ax=ax)
    return fig