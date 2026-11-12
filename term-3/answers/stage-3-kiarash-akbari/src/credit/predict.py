"""Score a fresh file: the same ingest, the same view, the same prepared matrix.

Deliverable 9 of the challenge is a script that takes a new file with the same schema
and produces predictions, applying the preparation unchanged. The cheap version of
"unchanged" is a second copy of the transform code that drifts; the version here
reuses the three things that define the pipeline, in order:

1. :func:`credit.load.collect_records` - paging, contract check, quarantine;
2. ``credit.v_staging_model_input`` - the row-wise prep, as SQL, over the staging
   table, with the sentinel codes and the utilization cap defined once;
3. :func:`credit.prep.apply` with the constants saved in the bundle - the fitted part,
   which by definition cannot be refitted on rows being scored.

So the only difference between scoring and training is which table the view reads.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import sqlalchemy as sa
from data_factory.errors import DataError, PipelineError
from data_factory.exit_codes import ExitCode

from .columns import DB_STAGING_TABLE
from .config import CreditConfig, parse_credit_config
from .cost import CHALLENGE_MATRIX, CostMatrix, binary_counts, cost_per_1000
from .db import engine_for
from .load import application_table, collect_records, insert_records
from .models import Bundle
from .prep import apply as apply_prep
from .read import load_frame
from .source import file_fingerprint, file_sha256


@dataclass(frozen=True)
class ScoringResult:
    """Rows scored, rows rejected, and the cost if the file happened to carry labels."""

    file_name: str
    sha256: str
    fingerprint: str
    rows_staged: int
    rows_rejected: int
    scored: pd.DataFrame
    cost_per_1000: float | None
    counts: dict[str, int] | None

    def summary(self) -> str:
        cost = (
            "unavailable (no labels in the file)"
            if self.cost_per_1000 is None
            else f"{self.cost_per_1000:.2f}"
        )
        return (
            f"file={self.file_name} sha256={self.sha256[:12]} rows={self.rows_staged} "
            f"rejected={self.rows_rejected} cost_per_1000={cost}"
        )


def staging_config(cfg: CreditConfig, csv_path: Path) -> CreditConfig:
    """Point a config at a different file, without touching the training paths.

    The page directory moves under ``data/raw/staging-pages/<file name>`` so a rerun
    on the same file resumes instead of re-reading, and a rerun on a different file
    never reuses another file's pages - the fingerprint check in the load step is the
    backstop, the directory separation is what makes the failure loud.
    """
    values: dict[str, object] = {
        "csv_path": str(csv_path),
        "sql_migrations_dir": str(cfg.sql_migrations_dir),
        "schema": cfg.schema,
        "page_size": cfg.page_size,
        "batch_size": cfg.batch_size,
        "quarantine_max_share": cfg.quarantine_max_share,
        "quarantine_min_rows": cfg.quarantine_min_rows,
        "curated_dir": str(cfg.curated_dir),
        "raw_pages_dir": str(
            cfg.raw_pages_dir.parent / "staging-pages" / csv_path.stem
        ),
        "quarantine_dir": str(cfg.quarantine_dir / "staging"),
    }
    return parse_credit_config(values, base=Path.cwd())


def stage_file(
    engine: sa.Engine, cfg: CreditConfig, csv_path: Path
) -> tuple[int, int, str, str]:
    """Validate and insert a fresh file into ``credit.staging_application``."""
    staging_cfg = staging_config(cfg, csv_path)
    records, report, _fetch = collect_records(staging_cfg)
    target = application_table(staging_cfg.schema, DB_STAGING_TABLE)
    with engine.begin() as connection:
        connection.execute(sa.delete(target))
        insert_records(connection, target, records, batch_size=staging_cfg.batch_size)
    return (
        len(records),
        report.quarantined,
        file_sha256(csv_path),
        file_fingerprint(csv_path),
    )


def score_staged(
    engine: sa.Engine, bundle: Bundle, *, schema: str = "credit"
) -> pd.DataFrame:
    """Prepare the staged rows and apply the saved threshold."""
    frame = load_frame(engine, "staging_input", schema=schema)
    if frame.empty:
        raise DataError("the staging table is empty; stage a file first")
    from .features import build_features  # local: only the scoring path needs features

    prepared = apply_prep(build_features(frame), bundle.prep_stats)
    scores = bundle.score(prepared)
    out = pd.DataFrame({"row_id": frame["row_id"].to_numpy()})
    out["score"] = scores
    out["flag"] = (scores >= bundle.threshold).astype("int64")
    out["threshold"] = bundle.threshold
    out["model"] = bundle.family
    if (
        "serious_dlqin_2yrs" in frame.columns
        and frame["serious_dlqin_2yrs"].notna().all()
    ):
        out["serious_dlqin_2yrs"] = frame["serious_dlqin_2yrs"].to_numpy()
    return out


def cost_of(
    scored: pd.DataFrame, *, matrix: CostMatrix = CHALLENGE_MATRIX
) -> tuple[float | None, dict[str, int] | None]:
    """Expected cost per 1,000 if the scored file carried outcomes, else ``(None, None)``."""
    if "serious_dlqin_2yrs" not in scored.columns:
        return None, None
    counts = binary_counts(scored["serious_dlqin_2yrs"], scored["flag"])
    return cost_per_1000(counts, matrix, n=len(scored)), counts


def score_file(
    engine: sa.Engine,
    cfg: CreditConfig,
    csv_path: Path,
    bundle: Bundle,
    *,
    matrix: CostMatrix = CHALLENGE_MATRIX,
) -> ScoringResult:
    staged, rejected, sha256, fingerprint = stage_file(engine, cfg, csv_path)
    scored = score_staged(engine, bundle, schema=cfg.schema)
    cost, counts = cost_of(scored, matrix=matrix)
    return ScoringResult(
        file_name=csv_path.name,
        sha256=sha256,
        fingerprint=fingerprint,
        rows_staged=staged,
        rows_rejected=rejected,
        scored=scored,
        cost_per_1000=cost,
        counts=counts,
    )


def resolve(path: Path | None, config_path: Path) -> CreditConfig:
    """Read the stage-3 config the same way ``credit.load`` does."""
    from .load import resolve_credit_config

    return resolve_credit_config(config_path, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m credit.predict")
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument(
        "--bundle", required=True, type=Path, help="directory saved by Bundle.save"
    )
    parser.add_argument("--config", default="../stage-1/config/default.toml")
    parser.add_argument("--credit-config", default=None, type=Path)
    parser.add_argument(
        "--out", default=None, type=Path, help="write the scores as CSV here"
    )
    args = parser.parse_args(argv)

    try:
        if not args.file.is_file():
            raise DataError("no such file to score", context={"path": str(args.file)})
        cfg = resolve(args.credit_config, Path(args.config))
        bundle = Bundle.load(args.bundle)
        engine = engine_for(Path(args.config))
        try:
            result = score_file(engine, cfg, args.file, bundle)
        finally:
            engine.dispose()
    except PipelineError as exc:
        print(f"{type(exc).__name__}: {exc} context={exc.context}", file=sys.stderr)
        return int(exc.exit_code)
    except (ValueError, KeyError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return int(ExitCode.DATA)
    print(result.summary())
    top = result.scored.sort_values("score", ascending=False).head(5)
    print("highest scores:")
    print(top.to_string(index=False))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        result.scored.to_csv(args.out, index=False)
        print(f"wrote={args.out} rows={len(result.scored)}")
    return int(ExitCode.OK)


if __name__ == "__main__":
    raise SystemExit(main())
