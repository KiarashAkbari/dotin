# Stage 3 plan — «تصمیم پرهزینه» (Challenge_Machine_Learning_03)

Working notes for the delivery folder `stage-3/`.
This file is **not** part of the deliverable; `report.md` is, and it will be written
from measurements, not from this plan.

Spec source: `files-given/03_Challenge_Machine_Learning.md` (sha256
`4c9500818ff73ec57845c594fac3862db1b50c9644f8c9d1b153f087362ed5a8`), rules from
`files-given/AGENTS.md`. Runtime cap for the notebook: **20 minutes**.
Shared grading: 30% correctness, 25% engineering+Git, 25% evidence, 20%
reproducibility — and reproducibility is a precondition, not a bonus.

## Two things block the graded part, and both are outside this sandbox

| Blocker | Why I cannot clear it | What unblocks it |
|---|---|---|
| `cs-training.csv` (Kaggle *Give Me Some Credit*) | Kaggle is a competition: rules must be accepted with the student's own account, and this sandbox can only reach github/npm/pypi | put the file at `data/raw/cs-training.csv` (git-ignored) + the data dictionary next to it |
| PostgreSQL with the stage-2 data | no server here, and the challenge forbids reading the model data from CSV | the owner's `psql`/CLI runs, output tee-saved into `docs/evidence/` |

Until the first lands, no metric in a report can exist. Everything below that can be
built and tested without them has been built.

## Acceptance checklist, mapped

Spec section → where it lives → status.

| Required | File / step | Status |
|---|---|---|
| data from the Database, not a CSV | `src/credit/source.py` feeds stage 1's `fetch_all`; reading back via SQLAlchemy view | adapter done, verified offline against the real stage-1 machinery; the DB round-trip is NOT RUN |
| dataset and label known; numeric-looking categoricals; sentinels; impossible outliers | `columns.py` contract + `sql/queries/credit_sentinel_spikes.sql` | measurement queries written; numbers pending the file |
| missingness: random or not, with evidence, ≥2 policies compared | `missingness.py` (two-proportion z, co-missingness odds ratio) | maths tested; profile pending data |
| leak-proof boundary, inspectable | `prep.py` + `sql/010_credit_schema.sql` view split | **not started** — needs sklearn pipeline + the split |
| ≥4 features with a leakage audit and an ablation | `features.py` (6 specs, each with hypothesis + decision-time note + boundary) | built and tested; ablation numbers pending data |
| baseline + ≥2 model families, ≥25% cost reduction | `cost.py` baselines done; models pending | baseline maths tested (hand-computed) |
| threshold chosen by cost, sensitivity to 12:1 → 4:1, cost per 1,000 | `cost.py` `select_threshold`, `matrix_sensitivity` | tested on a constructed case where the optimum moves 0.0 → 0.405 |
| full evaluation, multi-fold spread, one look at a holdout | notebook + `evaluate` step | not started |
| interpretation, Model Card, "where it breaks", fairness | notebook sections | not started |
| ≥8 tests, ≥10 commits, model + prep saved, predict-a-new-file script | `tests/` (137 offline), `predict.py` pending | tests: done; artifacts: pending data |
| ≥1 section of prep inside the Database | `credit.v_model_input`, `credit.v_column_missingness` | DDL written, constraint-level tested; execution NOT RUN |

## Order of work (each step ends in a commit; nothing is "final")

1. ✅ **Contracts and cost maths.** Loader-facing pieces that cannot be argued about
   later: header contract, reason codes, cost per 1,000, threshold selection,
   feature definitions with their audit fields.
2. ⏳ **`credit` through the factory.** Config section with a `kind = "file"`, manifest
   branch for file sources (requests = 0, source = `file:<name>` + sha), CLI
   `load-credit` reusing `read_csv_batches`/`_insert_records`/`load_run`, and
   `010`+`011` applied by the same migration runner, discovered from `sql/` rather than
   named in config (a name in config is exactly how 011 went unapplied). This is the
   refactor the spec warns
   about — it measured *small*: `data_factory.fetch.PageSource` is a one-method
   protocol and `pipeline.py` is already source-agnostic, so it is an adapter plus
   config/manifest branches, not a rewrite. Owner runs the load; evidence file.
3. ⏳ **Split and prep.** 60/20/20 stratified train/validation/test with a fixed seed,
   holdout untouched until step 7; `ColumnTransformer` fitted on train only;
   scaling justified per model family (needed for `LogisticRegression`, not for
   `HistGradientBoostingClassifier`) and written that way in the report; a printed
   audit of every fitted constant, so the leak-proof boundary is inspectable.
4. ⏳ **Models and ablation.** Two families (logistic regression, gradient boosting),
   each feature added and removed on validation cost, one feature cut if it earns
   nothing — and the cut reported.
5. ⏳ **Threshold.** Cost curve on validation, chosen threshold, sensitivity at 4:1,
   expected cost per 1,000 as the headline number.
6. ⏳ **Notebook.** Thin narrative: DB query → frame → model; `Run All` under the cap;
   numbers saved to `data/curated/credit-runs.json` so the report cites a file the
   reader can diff.
7. ⏳ **Holdout, interpretation, Model Card, fairness, report, README, evidence.**
   One look at the holdout. Top-5 importances against business intuition, including
   whatever the 96/98 rows turn out to be — if treating them as counts inflates the
   metric, that story goes into the report as the leakage-adjacent finding it is.

## Hours, honestly

The estimate I can defend: **~4.5 h of assistant work that needs no data** (steps 1–3
code, tests, docs) + **~3 h of owner-run loops** (load, benchmark, restart-and-run-all,
evidence capture) + **~2.5 h of writing** (notebook narrative, report, model card).
Roughly 10 h wall-clock across two working days, which matches the 7–10 h range and is
more than the 5 h in the earlier message: the load/verify loop is serial, because the
sandbox has no server, and each round-trip costs a commit cycle.

## Division of labour, so two agents do not build the same folder twice

This workspace owns the delivery folder above. If the other assistant starts stage 3 in
its own checkout, it should take steps 2–3 only after pulling this tree
(`arena/9448ddf0-data-factory-v2` in `KiarashAkbari/data-factory-v2`), otherwise the
contracts in `columns.py` and the constraint names in the DDL will diverge — and those
two files are the ones everything else asserts against.
