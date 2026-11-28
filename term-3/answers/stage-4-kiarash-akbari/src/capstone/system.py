"""The model layer that runs: time split, column policy, rule baseline, two families, evaluation.

Everything the brief grades as a result is computed here, on the frame the database builds, and
nothing here reads a CSV:

* **Time split** (``04`` section "definition"): rows whose ``order_approved_at`` falls on or before
  ``split.boundary`` are the training window, the rest is the evaluation window. Model *selection*
  happens inside the training window only: its last ``model.validation_months`` months are the
  validation slice, every candidate is fitted on the months before it and ranked there. The winner
  is refitted on the whole training window and scored once on the evaluation window. The evaluation
  months are never used to choose anything (review B1).
* **Column policy** (decisions.md section 15, review B2): numbers get the training-window median
  plus a ``missing`` indicator, so "no prior order" stays a signal instead of becoming zero; the
  three text columns are one-hot encoded with categories seen at least ``MIN_CATEGORY_ROWS`` times
  in the training window, the rest folded into one "infrequent" bucket, and a blank cell is its own
  category. Every statistic is fitted on training rows only.
* **Rule baseline** (review B17): one ops-readable rule, "follow up the orders with the largest X",
  where X is picked from a short list of single columns on the *validation* slice.
* **Quota, not threshold**: every metric is taken at the top ``capacity.share`` of a ranking.

The labelled population is D2's decision: an order without any review has no label, so it is not
in any denominator. The operational check (rank *all* approved orders, unlabelled ones included,
and count what the top 10 % catches) is reported beside it (review B16).
"""

from __future__ import annotations

import io
import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Sequence

from .capacity import capacity_table
from .config import CapstoneConfig
from .errors import InputError, NotReadyError
from .model import auc as rank_auc
from .model import metrics_at_share

#: the text columns of v_feature_frame (sql/003); everything else is a number or a flag
CATEGORICAL = ("customer_state", "seller_state", "product_category_name_english")
#: columns that identify or time a row, or carry the label -- never a feature
NOT_FEATURES = frozenset(
    {"order_id", "customer_id", "order_approved_at", "decision_month", "is_negative", "has_review"}
)
EXTERNAL_COLUMN = "external_density_2010"
EXTERNAL_VIEW = "capstone.v_external_feature"
#: a category must cover this many training rows to get its own column
MIN_CATEGORY_ROWS = 200

#: the rule baseline's candidates: (column, direction, sentence for the ops team). direction +1
#: means "larger is riskier". Picked on the validation slice, never on the evaluation months.
RULE_CANDIDATES = (
    ("promise_days", 1, "the longest promised delivery window"),
    ("freight_share", 1, "the highest share of freight in the bill"),
    ("n_items", 1, "the most items in one order"),
    ("n_sellers", 1, "the most sellers in one order"),
    ("customer_seller_span_sq", 1, "the longest distance between customer and seller"),
    ("price_sum", 1, "the most expensive orders"),
    ("shipping_limit_days", 1, "the latest seller hand-over deadline"),
)

MODEL_TABLE = "capstone.model_run"
SCORE_TABLE = "capstone.order_score"
ARTIFACT_NAME = "model.joblib"
METRICS_NAME = "model_metrics.json"

SCORE_DDL = f"""
CREATE TABLE IF NOT EXISTS {MODEL_TABLE} (
    run_id      TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    boundary    DATE NOT NULL,
    model       TEXT NOT NULL,
    metrics     JSONB NOT NULL
);
CREATE TABLE IF NOT EXISTS {SCORE_TABLE} (
    run_id            TEXT NOT NULL REFERENCES {MODEL_TABLE}(run_id) ON DELETE CASCADE,
    order_id          TEXT NOT NULL,
    order_approved_at TIMESTAMP NOT NULL,
    window_name       TEXT NOT NULL CHECK (window_name IN ('train', 'eval')),
    score             DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (run_id, order_id)
);
"""


# --------------------------------------------------------------------------------------------
# reading the frame
# --------------------------------------------------------------------------------------------


def frame_sql(*, external: bool, where: str = "") -> str:
    """One statement: the audited frame, its label, and (optionally) the external column."""
    select = [
        "f.*",
        "l.is_negative",
        "l.has_review",
        "to_char(f.order_approved_at, 'YYYY-MM') AS decision_month",
    ]
    joins = ["JOIN capstone.v_order_label AS l USING (order_id)"]
    if external:
        select.append(f"x.{EXTERNAL_COLUMN}")
        joins.append(f"LEFT JOIN {EXTERNAL_VIEW} AS x USING (order_id)")
    sql = "SELECT " + ", ".join(select) + " FROM capstone.v_feature_frame AS f " + " ".join(joins)
    if where:
        sql += " WHERE " + where
    return sql + " ORDER BY f.order_approved_at, f.order_id"


def has_external_view(cursor: Any) -> bool:
    cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (EXTERNAL_VIEW,))
    return bool(cursor.fetchone()[0])


def copy_frame(cursor: Any, sql: str):
    """``COPY (sql) TO STDOUT`` into pandas: one text buffer instead of 100k Python tuples."""
    import pandas as pd

    buffer = io.BytesIO()
    with cursor.copy(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER true)") as copy:
        for chunk in copy:
            buffer.write(chunk)
    buffer.seek(0)
    frame = pd.read_csv(buffer, low_memory=False)
    return tidy(frame)


def tidy(frame):
    """Types the CSV round trip cannot carry: booleans, the timestamp, the label."""
    import pandas as pd

    for column in frame.columns:
        series = frame[column]
        if series.dtype == object or pd.api.types.is_string_dtype(series):
            values = set(series.dropna().unique().tolist())
            if values and values <= {"t", "f"}:
                frame[column] = series.map({"t": 1.0, "f": 0.0}).astype(float)
            elif column in CATEGORICAL:
                # object, not pandas' string dtype: the encoder's blank imputer wants np.nan cells
                frame[column] = series.astype(object).where(series.notna(), None)
    for column in CATEGORICAL:
        if column in frame:
            series = frame[column]
            frame[column] = series.astype(object).where(series.notna(), None)
    frame["order_approved_at"] = pd.to_datetime(frame["order_approved_at"])
    if "is_negative" in frame:
        frame["is_negative"] = frame["is_negative"].astype("float")  # NaN = no review (D2)
    if EXTERNAL_COLUMN in frame:
        frame[EXTERNAL_COLUMN] = pd.to_numeric(frame[EXTERNAL_COLUMN], errors="coerce")
    return frame


def read_frame(
    connection: Any, *, external: bool | None = None, where: str = "", allow_empty: bool = False
):
    """The frame for the model step. ``external=None`` means "join it when the view exists"."""
    cursor = connection.cursor()
    if external is None or external:
        present = has_external_view(cursor)
        if external and not present:
            raise NotReadyError(
                f"{EXTERNAL_VIEW} is missing: apply sql/006 (the features step does) first"
            )
        external = present
    frame = copy_frame(cursor, frame_sql(external=bool(external), where=where))
    if frame.empty and not allow_empty:
        raise NotReadyError(
            "capstone.v_feature_frame returned no rows: run the ingest and features steps first"
        )
    return frame


# --------------------------------------------------------------------------------------------
# columns and pipelines
# --------------------------------------------------------------------------------------------


def feature_columns(frame, *, external: bool) -> tuple[list[str], list[str]]:
    """(numeric, categorical) features, in frame order; the external column only on request."""
    categorical = [name for name in CATEGORICAL if name in frame.columns]
    numeric = [
        name
        for name in frame.columns
        if name not in NOT_FEATURES and name not in categorical and name != EXTERNAL_COLUMN
    ]
    if external:
        if EXTERNAL_COLUMN not in frame.columns:
            raise InputError(f"{EXTERNAL_COLUMN} is not in the frame; join {EXTERNAL_VIEW} first")
        numeric.append(EXTERNAL_COLUMN)
    return numeric, categorical


def make_estimator(name: str, seed: int):
    """``family.variant`` from the config's roster -> an unfitted scikit-learn estimator."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression

    family, _, variant = name.partition(".")
    if family == "logreg":
        strength = {"c0_1": 0.1, "c1": 1.0, "c10": 10.0}.get(variant)
        if strength is None:
            raise InputError(f"unknown logreg variant {variant!r}; known: c0_1, c1, c10")
        return LogisticRegression(C=strength, max_iter=2000)
    if family == "hgb":
        depth = {"d3": 3, "d6": 6}.get(variant)
        if depth is None:
            raise InputError(f"unknown hgb variant {variant!r}; known: d3, d6")
        return HistGradientBoostingClassifier(
            max_depth=depth, learning_rate=0.05, max_iter=300, l2_regularization=1.0,
            random_state=seed,
        )
    raise InputError(f"unknown model family {family!r}; the roster knows logreg and hgb")


def make_pipeline(name: str, numeric: Sequence[str], categorical: Sequence[str], seed: int):
    """The column policy of decisions.md section 15, fitted inside the pipeline on training rows."""
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, StandardScaler

    numeric = list(numeric)
    parts = []
    plain = [column for column in numeric if column != EXTERNAL_COLUMN]
    if plain:
        parts.append(
            (
                "numeric",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="median", add_indicator=True)),
                        ("scale", StandardScaler()),
                    ]
                ),
                plain,
            )
        )
    if EXTERNAL_COLUMN in numeric:
        # density spans 0.1 to 13,000 people/km2: the log is what makes it one scale
        parts.append(
            (
                "external",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="median", add_indicator=True)),
                        ("log", FunctionTransformer(_log1p, feature_names_out="one-to-one")),
                        ("scale", StandardScaler()),
                    ]
                ),
                [EXTERNAL_COLUMN],
            )
        )
    if categorical:
        parts.append(
            (
                "text",
                Pipeline(
                    [
                        ("blank", SimpleImputer(strategy="constant", fill_value="(blank)")),
                        (
                            "onehot",
                            OneHotEncoder(
                                handle_unknown="infrequent_if_exist",
                                min_frequency=MIN_CATEGORY_ROWS,
                                sparse_output=False,
                            ),
                        ),
                    ]
                ),
                list(categorical),
            )
        )
    return Pipeline(
        [("columns", ColumnTransformer(parts)), ("model", make_estimator(name, seed))]
    )


def _log1p(values):
    import numpy as np

    return np.log1p(np.clip(values, 0, None))


def fit_score(name: str, fit_rows, score_rows, numeric, categorical, seed: int):
    """Fit on ``fit_rows`` (labelled only) and return (pipeline, scores of ``score_rows``)."""
    labelled = fit_rows[fit_rows["is_negative"].notna()]
    if labelled["is_negative"].nunique() < 2:
        raise InputError("the fitting rows hold one class only; nothing can be ranked")
    pipeline = make_pipeline(name, numeric, categorical, seed)
    columns = list(numeric) + list(categorical)
    pipeline.fit(labelled[columns], labelled["is_negative"].astype(int))
    return pipeline, pipeline.predict_proba(score_rows[columns])[:, 1]


# --------------------------------------------------------------------------------------------
# the split
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    boundary: date
    validation_start: date
    train: Any  # boolean masks over the frame
    fit: Any
    validation: Any
    evaluation: Any

    def describe(self, frame) -> dict[str, Any]:
        def window(mask) -> dict[str, Any]:
            rows = frame[mask]
            labelled = rows["is_negative"].notna()
            return {
                "rows": int(len(rows)),
                "labelled": int(labelled.sum()),
                "negatives": int((rows["is_negative"] == 1).sum()),
                "first": str(rows["order_approved_at"].min()) if len(rows) else None,
                "last": str(rows["order_approved_at"].max()) if len(rows) else None,
                "months": sorted(rows["decision_month"].unique().tolist()),
            }

        return {
            "boundary": self.boundary.isoformat(),
            "validation_start": self.validation_start.isoformat(),
            "train": window(self.train),
            "fit": window(self.fit),
            "validation": window(self.validation),
            "evaluation": window(self.evaluation),
        }


def month_start_back(day: date, months: int) -> date:
    """The first day of the month ``months - 1`` months before ``day``'s month."""
    index = day.year * 12 + (day.month - 1) - (months - 1)
    return date(index // 12, index % 12 + 1, 1)


def time_split(frame, boundary: date | None, validation_months: int | None) -> Split:
    """Train <= boundary (whole day) < evaluation; validation = last N months of train."""
    import pandas as pd

    if boundary is None:
        raise NotReadyError("split.boundary is undecided; the time split is the brief's rule")
    months = validation_months or 3
    cut = pd.Timestamp(boundary) + pd.Timedelta(days=1)
    validation_start = month_start_back(boundary, months)
    approved = frame["order_approved_at"]
    train = approved < cut
    evaluation = ~train
    validation = train & (approved >= pd.Timestamp(validation_start))
    fit = train & ~validation
    for name, mask in (("fit", fit), ("validation", validation), ("evaluation", evaluation)):
        if not (frame.loc[mask, "is_negative"] == 1).any():
            raise InputError(
                f"the {name} window holds no negative review; move split.boundary or "
                "model.validation_months so every window can be scored",
                context={"window": name, "boundary": boundary.isoformat()},
            )
    return Split(boundary, validation_start, train, fit, validation, evaluation)


# --------------------------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------------------------


def _ordered(rows, scores):
    """Rows and scores sorted by order_id, so ties in a ranking break the same way every run."""
    import numpy as np

    order = np.argsort(rows["order_id"].to_numpy(), kind="stable")
    return rows.iloc[order], np.asarray(scores)[order]


def evaluate(rows, scores, *, share: float, shares: Sequence[float]) -> dict[str, Any]:
    """Every number of brief section 3 for one ranking over one window."""
    from sklearn.metrics import average_precision_score, roc_auc_score

    rows, scores = _ordered(rows, scores)
    labelled = rows["is_negative"].notna().to_numpy()
    lab_rows = rows[labelled]
    lab_scores = [float(value) for value in scores[labelled]]
    labels = [bool(value) for value in lab_rows["is_negative"].to_numpy()]
    out: dict[str, Any] = {
        "rows": int(len(rows)),
        "labelled": int(labelled.sum()),
        "negatives": int(sum(labels)),
        "negative_rate": (sum(labels) / len(labels)) if labels else None,
        "auc": float(roc_auc_score(labels, lab_scores)) if 0 < sum(labels) < len(labels) else None,
        "average_precision": float(average_precision_score(labels, lab_scores))
        if any(labels)
        else None,
        "capacity": capacity_table(labels, lab_scores, list(shares)),
        "operating_point": metrics_at_share(lab_scores, labels, share),
    }
    # the operational check: the quota is 10 % of *all* approved orders, unlabelled ones included
    every = [float(value) for value in scores]
    flags = [bool(value == 1) for value in rows["is_negative"].to_numpy()]
    out["capacity_all_approved"] = capacity_table(flags, every, list(shares))
    months = []
    for month in sorted(lab_rows["decision_month"].unique()):
        inside = (lab_rows["decision_month"] == month).to_numpy()
        month_labels = [flag for flag, keep in zip(labels, inside, strict=True) if keep]
        month_scores = [value for value, keep in zip(lab_scores, inside, strict=True) if keep]
        row = capacity_table(month_labels, month_scores, [share])[0]
        months.append(
            {
                "month": month,
                "labelled": len(month_labels),
                "negatives": int(sum(month_labels)),
                "reviewed": row["reviewed"],
                "caught": row["caught"],
                "recall": row["caught_share"],
                "auc": float(roc_auc_score(month_labels, month_scores))
                if 0 < sum(month_labels) < len(month_labels)
                else None,
            }
        )
    out["months"] = months
    return out


def recall_at(rows, scores, share: float) -> float | None:
    rows, scores = _ordered(rows, scores)
    keep = rows["is_negative"].notna().to_numpy()
    labels = [bool(v) for v in rows.loc[keep, "is_negative"].to_numpy()]
    return capacity_table(labels, [float(v) for v in scores[keep]], [share])[0]["caught_share"]


def paired_bootstrap(rows, first, second, share: float, *, rounds: int = 200, seed: int = 0):
    """95 % interval of recall(first) - recall(second) over resampled evaluation orders."""
    import numpy as np

    rng = np.random.default_rng(seed)
    keep = rows["is_negative"].notna().to_numpy()
    rows = rows[keep].reset_index(drop=True)
    first, second = np.asarray(first)[keep], np.asarray(second)[keep]
    deltas = []
    for _ in range(rounds):
        pick = rng.integers(0, len(rows), len(rows))
        sample = rows.iloc[pick].reset_index(drop=True)
        sample = sample.assign(order_id=[f"{i:08d}" for i in range(len(sample))])
        a = recall_at(sample, first[pick], share)
        b = recall_at(sample, second[pick], share)
        if a is not None and b is not None:
            deltas.append(a - b)
    if not deltas:
        return None
    low, high = np.percentile(deltas, [2.5, 97.5])
    return {"rounds": rounds, "low": float(low), "high": float(high), "mean": float(np.mean(deltas))}


# --------------------------------------------------------------------------------------------
# baseline rule
# --------------------------------------------------------------------------------------------


def rule_scores(rows, column: str, direction: int):
    """The rule as a ranking: the column itself (blank = least risky)."""
    import numpy as np

    values = rows[column].astype(float).to_numpy() * direction
    return np.where(np.isnan(values), -np.inf, values)


def choose_rule(frame, split: Split, share: float) -> dict[str, Any]:
    """Pick the single-column rule with the best validation recall at the quota."""
    validation = frame[split.validation]
    tried = []
    for column, direction, sentence in RULE_CANDIDATES:
        if column not in frame.columns:
            continue
        recall = recall_at(validation, rule_scores(validation, column, direction), share)
        tried.append(
            {"column": column, "direction": direction, "rule": sentence, "validation_recall": recall}
        )
    if not tried:
        raise InputError("none of the rule candidates is in the frame")
    best = max(tried, key=lambda row: (row["validation_recall"] or -1.0, row["column"]))
    return {"chosen": best, "tried": tried}


# --------------------------------------------------------------------------------------------
# drift
# --------------------------------------------------------------------------------------------


def psi(expected, actual, *, bins: int = 10) -> float | None:
    """Population stability index of ``actual`` against ``expected``'s deciles (NaN = own bin)."""
    import numpy as np

    expected = np.asarray(expected, dtype=float)
    actual = np.asarray(actual, dtype=float)
    finite = expected[~np.isnan(expected)]
    if len(finite) == 0:
        return None
    edges = np.unique(np.quantile(finite, np.linspace(0, 1, bins + 1)))
    if len(edges) < 2:
        edges = np.array([finite.min() - 0.5, finite.max() + 0.5])

    def shares(values):
        nan = np.isnan(values)
        inner = np.clip(values[~nan], edges[0], edges[-1])
        counts = np.histogram(inner, bins=edges)[0].astype(float)
        counts = np.append(counts, nan.sum())
        return np.clip(counts / max(len(values), 1), 1e-4, None)

    e, a = shares(expected), shares(actual)
    return float(np.sum((a - e) * np.log(a / e)))


def category_psi(expected, actual) -> float:
    import numpy as np
    import pandas as pd

    e = pd.Series(expected).fillna("(blank)").value_counts(normalize=True)
    a = pd.Series(actual).fillna("(blank)").value_counts(normalize=True)
    keys = e.index.union(a.index)
    e = np.clip(e.reindex(keys, fill_value=0).to_numpy(), 1e-4, None)
    a = np.clip(a.reindex(keys, fill_value=0).to_numpy(), 1e-4, None)
    return float(np.sum((a - e) * np.log(a / e)))


def drift(frame, split: Split, columns: Sequence[str]) -> list[dict[str, Any]]:
    """Train window vs evaluation window, per feature: PSI, KS test, means/medians."""
    from scipy.stats import ks_2samp

    train, test = frame[split.train], frame[split.evaluation]
    out = []
    for column in columns:
        if column in CATEGORICAL:
            top_train = train[column].fillna("(blank)").value_counts(normalize=True).head(1)
            top_test = test[column].fillna("(blank)").value_counts(normalize=True).head(1)
            out.append(
                {
                    "feature": column,
                    "kind": "category",
                    "psi": category_psi(train[column], test[column]),
                    "train_top": f"{top_train.index[0]} {top_train.iloc[0]:.3f}",
                    "eval_top": f"{top_test.index[0]} {top_test.iloc[0]:.3f}",
                }
            )
            continue
        a = train[column].astype(float)
        b = test[column].astype(float)
        a_ok, b_ok = a.dropna(), b.dropna()
        ks = ks_2samp(a_ok, b_ok) if len(a_ok) and len(b_ok) else None
        out.append(
            {
                "feature": column,
                "kind": "number",
                "psi": psi(a, b),
                "ks_statistic": float(ks.statistic) if ks else None,
                "ks_pvalue": float(ks.pvalue) if ks else None,
                "train_median": float(a_ok.median()) if len(a_ok) else None,
                "eval_median": float(b_ok.median()) if len(b_ok) else None,
                "train_missing": float(a.isna().mean()),
                "eval_missing": float(b.isna().mean()),
            }
        )
    return sorted(out, key=lambda row: -(row["psi"] or 0.0))


def negative_rate_by_month(frame) -> list[dict[str, Any]]:
    labelled = frame[frame["is_negative"].notna()]
    grouped = labelled.groupby("decision_month")["is_negative"].agg(["count", "mean"])
    return [
        {"month": month, "labelled": int(row["count"]), "negative_rate": float(row["mean"])}
        for month, row in grouped.iterrows()
    ]


#: what a config with no roster has to be told; `max()` over an empty selection says neither
#: which file nor which key, and arrives only after the feature frame has been read.
ROSTER_HELP = (
    "the config names no model families; [model] families needs at least two "
    "'family.variant' entries, e.g. ['logreg.c0_1', 'hgb.d3']"
)


def roster_of(config: CapstoneConfig) -> list[str]:
    """`[model].families` as a list, refused while it is still empty.

    Measured on a live run: a config whose `[model] families` was empty reached `max(selection)`
    with an empty list and raised `ValueError: max() iterable argument is empty` -- after the frame
    had been read, and without naming the file or the key that was at fault.
    """
    roster = list(config.model.families)
    if len(roster) < 2:
        raise InputError(f"{ROSTER_HELP}; got {len(roster)}")
    return roster


def seed_label(config: CapstoneConfig) -> str:
    """How a witness should print the seed: a `None` seed is not 0, it is an undecided key.

    Three scripts coerced a missing seed to zero, so a config with no seed printed `seed 0` and the
    run looked as reproducible as one that had said which seed it used.
    """
    seed = config.model.random_seed
    return f"seed {seed}" if seed is not None else "seed not set in config"


# --------------------------------------------------------------------------------------------
# the whole experiment
# --------------------------------------------------------------------------------------------


def experiment(frame, config: CapstoneConfig, *, with_drift: bool = True) -> dict[str, Any]:
    """Everything: split, rule, roster on validation, refit, evaluation, external with/without.

    Returns the metrics dict plus the fitted winner under ``"_pipeline"`` (not JSON).
    """
    # Both guards come before `np`: a config that cannot be honoured costs no frame read and no
    # import, which is the difference between a typo and a wasted minute on a shared cluster.
    seed_value = config.model.random_seed
    if seed_value is None:
        raise InputError(
            "[model] random_seed is not set; a run that invents a seed is not a reproducible run "
            f"({seed_label(config)}; read from {config.source}). The recorded seed lives in "
            "config/capstone.default.toml; a local config/capstone.toml replaces that file whole, "
            "so copy capstone.default.toml (not the example) when you need a machine-local one"
        )
    seed = int(seed_value)
    roster = roster_of(config)
    import numpy as np

    share = float(config.capacity.share)
    shares = list(config.capacity.report_shares)
    split = time_split(frame, config.split.boundary, config.model.validation_months)
    external_values = (
        EXTERNAL_COLUMN in frame.columns and frame.loc[split.train, EXTERNAL_COLUMN].notna().any()
    )
    numeric, categorical = feature_columns(frame, external=False)

    fit_rows, val_rows = frame[split.fit], frame[split.validation]
    train_rows, eval_rows = frame[split.train], frame[split.evaluation]

    # 1. the rule baseline, chosen on validation, scored on evaluation
    rule = choose_rule(frame, split, share)
    chosen_rule = rule["chosen"]
    rule_eval = rule_scores(eval_rows, chosen_rule["column"], chosen_rule["direction"])

    # 2. the roster on the validation slice (fit = train months before validation)
    selection = []
    for name in roster:
        _, val_scores = fit_score(name, fit_rows, val_rows, numeric, categorical, seed)
        labelled = val_rows["is_negative"].notna().to_numpy()
        from sklearn.metrics import roc_auc_score

        selection.append(
            {
                "model": name,
                "family": name.split(".")[0],
                "validation_recall": recall_at(val_rows, val_scores, share),
                "validation_auc": float(
                    roc_auc_score(val_rows["is_negative"][labelled], val_scores[labelled])
                ),
            }
        )
    winner = max(selection, key=lambda row: (row["validation_recall"] or -1, row["validation_auc"]))

    # 3. every candidate refitted on the whole training window, scored once on evaluation. Only
    # the winner (chosen above, before this loop) is the system; the others are context rows.
    evaluated: dict[str, Any] = {}
    pipelines: dict[str, Any] = {}
    eval_scores: dict[str, Any] = {}
    for name in roster:
        pipeline, scores = fit_score(name, train_rows, eval_rows, numeric, categorical, seed)
        pipelines[name] = pipeline
        eval_scores[name] = scores
        evaluated[name] = evaluate(eval_rows, scores, share=share, shares=shares)
    best = winner["model"]
    rule_eval_metrics = evaluate(eval_rows, rule_eval, share=share, shares=shares)

    # 4. the external signal, with and without, same model, same split
    external: dict[str, Any] = {"available": bool(external_values)}
    if external_values:
        ext_numeric, _ = feature_columns(frame, external=True)
        _, ext_val = fit_score(best, fit_rows, val_rows, ext_numeric, categorical, seed)
        _, ext_eval = fit_score(best, train_rows, eval_rows, ext_numeric, categorical, seed)
        coverage = float(frame[EXTERNAL_COLUMN].notna().mean())
        with_metrics = evaluate(eval_rows, ext_eval, share=share, shares=shares)
        external.update(
            {
                "column": EXTERNAL_COLUMN,
                "model": best,
                "coverage": coverage,
                "validation_recall_without": winner["validation_recall"],
                "validation_recall_with": recall_at(val_rows, ext_val, share),
                "eval_recall_without": evaluated[best]["operating_point"]["recall"],
                "eval_recall_with": with_metrics["operating_point"]["recall"],
                "eval_auc_without": evaluated[best]["auc"],
                "eval_auc_with": with_metrics["auc"],
                "eval_recall_delta_ci95": paired_bootstrap(
                    eval_rows, ext_eval, eval_scores[best], share, seed=seed
                ),
                "negative_rate_by_density_quartile": density_quartiles(frame[split.train]),
            }
        )

    # 5. a model against the rule, with an interval, so "better" is a measured claim
    model_vs_rule = paired_bootstrap(eval_rows, eval_scores[best], rule_eval, share, seed=seed)

    out: dict[str, Any] = {
        "label": "is_negative = worst review score of the order <= 2 (v_order_label)",
        "decision_moment": "order_approved_at",
        "share": share,
        "split": split.describe(frame),
        "features": {"numeric": numeric, "categorical": categorical},
        "rule_baseline": {
            **rule,
            "evaluation": rule_eval_metrics,
        },
        "selection": selection,
        "chosen_model": best,
        "evaluation": evaluated,
        "model_vs_rule_recall_delta_ci95": model_vs_rule,
        "external": external,
        "negative_rate_by_month": negative_rate_by_month(frame),
    }
    if with_drift:
        out["drift"] = drift(frame, split, numeric + categorical)
        out["drift_vs_model"] = drift_explains(evaluated[best]["months"], out["drift"])
    out["_pipeline"] = pipelines[best]
    out["_eval_scores"] = eval_scores[best]
    out["_train_scores"] = pipelines[best].predict_proba(
        train_rows[numeric + categorical]
    )[:, 1]
    out["_split"] = split
    out["_columns"] = (numeric, categorical)
    del np
    return out


def density_quartiles(rows) -> list[dict[str, Any]]:
    """The external hypothesis in its rawest form: negative rate by density quartile (train only)."""
    import pandas as pd

    labelled = rows[rows["is_negative"].notna() & rows[EXTERNAL_COLUMN].notna()]
    if labelled.empty:
        return []
    quartile = pd.qcut(labelled[EXTERNAL_COLUMN], 4, labels=["q1 (sparse)", "q2", "q3", "q4 (dense)"])
    grouped = labelled.groupby(quartile, observed=True)
    return [
        {
            "quartile": str(name),
            "orders": int(len(group)),
            "density_median": float(group[EXTERNAL_COLUMN].median()),
            "negative_rate": float(group["is_negative"].mean()),
        }
        for name, group in grouped
    ]


#: a month with fewer labelled orders than this is reported but not judged: the evaluation window's
#: tail (2018-09) holds one labelled order, and a recall of 1.0 on one order is not a month
MIN_MONTH_LABELLED = 500


def drift_explains(months: list[dict[str, Any]], drift_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The facts the drift paragraph needs side by side: monthly recall, the second metric, top shifts.

    The recall range and the brief's second metric (the same number on the last evaluation month)
    use only months with at least ``MIN_MONTH_LABELLED`` labelled orders; thinner months are listed
    under ``months_not_judged`` instead of silently dropped.
    """
    judged = [
        row for row in months if row["recall"] is not None and row["labelled"] >= MIN_MONTH_LABELLED
    ]
    recalls = [row["recall"] for row in judged]
    last = judged[-1] if judged else None
    return {
        "monthly_recall": {row["month"]: row["recall"] for row in months},
        "min_month_labelled": MIN_MONTH_LABELLED,
        "months_not_judged": [row["month"] for row in months if row not in judged],
        "recall_range": (max(recalls) - min(recalls)) if recalls else None,
        "last_full_month": (
            {key: last[key] for key in ("month", "labelled", "negatives", "recall", "auc")}
            if last
            else None
        ),
        "top_shifted_features": [
            {"feature": row["feature"], "psi": row["psi"]} for row in drift_rows[:5]
        ],
    }


def public(result: dict[str, Any]) -> dict[str, Any]:
    """The JSON half of :func:`experiment` (drops the fitted objects)."""
    return {key: value for key, value in result.items() if not key.startswith("_")}


# --------------------------------------------------------------------------------------------
# persistence and the chain step
# --------------------------------------------------------------------------------------------


def run_id_now() -> str:
    return datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")


def save_artifact(path: Path, result: dict[str, Any], config: CapstoneConfig, run_id: str) -> Path:
    import joblib

    numeric, categorical = result["_columns"]
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {
            "run_id": run_id,
            "model": result["chosen_model"],
            "pipeline": result["_pipeline"],
            "numeric": numeric,
            "categorical": categorical,
            "trained_on": f"order_approved_at <= {config.split.boundary}",
            "share": float(config.capacity.share),
        },
        path,
    )
    return path


def store_scores(connection: Any, frame, result: dict[str, Any], run_id: str, boundary: date) -> int:
    """``capstone.model_run`` + ``capstone.order_score``: the scored frame, in the database."""
    split = result["_split"]
    cursor = connection.cursor()
    cursor.execute(SCORE_DDL)
    cursor.execute(
        f"INSERT INTO {MODEL_TABLE} (run_id, boundary, model, metrics) VALUES (%s, %s, %s, %s)",
        (run_id, boundary, result["chosen_model"], json.dumps(public(result), default=str)),
    )
    written = 0
    with cursor.copy(
        f"COPY {SCORE_TABLE} (run_id, order_id, order_approved_at, window_name, score) FROM STDIN"
    ) as copy:
        for window, mask, scores in (
            ("train", split.train, result["_train_scores"]),
            ("eval", split.evaluation, result["_eval_scores"]),
        ):
            rows = frame.loc[mask, ["order_id", "order_approved_at"]]
            for (order_id, approved), score in zip(rows.itertuples(index=False), scores, strict=True):
                copy.write_row((run_id, order_id, approved.to_pydatetime(), window, float(score)))
                written += 1
    connection.commit()
    return written


def run(config: CapstoneConfig) -> dict[str, Any]:
    """The ``model`` chain step: read the frame, run the experiment, persist scores and model."""
    from .features import connect, database_uri

    uri = database_uri(config, purpose="read the frame for the model step")
    connection = connect(uri, purpose="read the frame and write the scores")
    try:
        frame = read_frame(connection, external=None if config.external.enabled else False)
        result = experiment(frame, config)
        run_id = run_id_now()
        written = store_scores(connection, frame, result, run_id, config.split.boundary)
    finally:
        connection.close()
    curated = config.features.curated_dir
    artifact = save_artifact(curated / ARTIFACT_NAME, result, config, run_id)
    metrics = public(result)
    metrics["run_id"] = run_id
    (curated / METRICS_NAME).write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8"
    )
    best = result["evaluation"][result["chosen_model"]]
    return {
        "run_id": run_id,
        "model": result["chosen_model"],
        "scores_written": written,
        "artifact": str(artifact),
        "metrics": str(curated / METRICS_NAME),
        "eval_recall_at_share": best["operating_point"]["recall"],
        "eval_auc": best["auc"],
    }


def _json_default(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return str(value)
