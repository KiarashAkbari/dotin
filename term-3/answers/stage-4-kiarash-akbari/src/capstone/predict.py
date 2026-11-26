"""The prediction entry point: the schema gate, then the training path's own preparation and model.

``04`` asks for one file, the same validation, the same preparation, a ranked list --
and a clear refusal when the schema is wrong, naming the file and the column. That
last part needs no model, so it is written now, on purpose: the most common way to
fail this requirement is to build the scoring path first and bolt the validation on at
the end, where it quietly becomes a second implementation of the main path.

The expected columns come from stage 2's DDL, not from a list copied into this file.
A hard-coded column list is how a schema change turns into a silent pass.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .audit import columns_from_ddl
from .config import CapstoneConfig
from .errors import ConfigError, InputError  # noqa: F401

#: the table a fresh-order file is expected to match
INPUT_TABLE = "orders"


def expected_columns(config: CapstoneConfig, table: str = INPUT_TABLE) -> list[str]:
    """The contract for an incoming file, read from the DDL the database enforces."""
    ddl = config.features.sql_migrations_dir / "001_create_olist_schema.sql"
    if not ddl.is_file():
        # stage 4 has no DDL of its own until phase 4, so fall back to stage 2's
        ddl = config.root.parent / "stage-2-kiarash-akbari" / "sql" / "001_create_olist_schema.sql"
    if not ddl.is_file():
        raise ConfigError(
            "cannot say what a valid order file looks like: no 001_create_olist_schema.sql "
            f"under {config.features.sql_migrations_dir} or ../stage-2-kiarash-akbari/sql"
        )
    tables = columns_from_ddl(ddl)
    if table not in tables or not tables[table]:
        raise ConfigError(f"{ddl} has no olist.{table} table to take a schema from")
    return tables[table]


def read_header(path: Path) -> list[str]:
    """The first row of a file, with an error that names the file."""
    if not path.is_file():
        raise InputError(f"no input file at {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        try:
            header = next(csv.reader(handle))
        except StopIteration:
            raise InputError(f"{path.name} is empty; a header row is required") from None
    return [cell.strip().lower() for cell in header]


def check_schema(header: list[str], expected: list[str], *, origin: str) -> None:
    """Refuse a wrong header, naming the file and each offending column."""
    wanted, seen = set(expected), set(header)
    missing = sorted(wanted - seen)
    extra = sorted(seen - wanted)
    if not missing and not extra:
        return
    parts: list[str] = []
    if missing:
        parts.append(f"missing column(s): {', '.join(missing)}")
    if extra:
        parts.append(f"unexpected column(s): {', '.join(extra)}")
    if not header:
        parts.append("the header row is empty")
    raise InputError(
        f"{origin} is not an order file as the schema defines it -- " + "; ".join(parts),
        context={"file": str(origin), "missing": missing, "unexpected": extra},
    )


def validate_file(path: Path, config: CapstoneConfig) -> list[str]:
    """Run the gate a grader's sealed cut has to pass, and return the header."""
    header = read_header(path)
    check_schema(header, expected_columns(config), origin=str(path))
    return header


#: the fresh-order bundle (decisions.md section 17): the files an order needs at its decision
#: moment, with the Kaggle names and stage 2's columns. Products and sellers are optional: a bundle
#: whose items name only known ones needs neither. Reviews are never read -- they are the label.
BUNDLE_REQUIRED = {
    "orders": "olist_orders_dataset.csv",
    "customers": "olist_customers_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
}
BUNDLE_OPTIONAL = {"products": "olist_products_dataset.csv", "sellers": "olist_sellers_dataset.csv"}
#: FK-safe load order and its reverse for the delete
LOAD_ORDER = ("customers", "sellers", "products", "orders", "order_items", "payments")
STAGE_SCHEMA = "capstone_predict_stage"
#: the snapshots sql/003 keeps; fresh orders change four of them (the zip centroids are reference).
#: The order matters: ``mv_order_centroids`` reads ``mv_order_item_rollup`` for the primary seller, so
#: it is refreshed after it -- refreshed first, a fresh order would get no seller centroid
SNAPSHOTS = (
    "capstone.mv_customer_order_history",
    "capstone.mv_order_item_rollup",
    "capstone.mv_order_payment_rollup",
    "capstone.mv_order_centroids",
)


def bundle_files(path: Path) -> dict[str, Path]:
    """The bundle's files: a folder, or the orders file with its siblings beside it."""
    folder = path if path.is_dir() else path.parent
    if not path.exists():
        raise InputError(f"no input at {path}; pass a folder of Olist-schema CSVs or its orders file")
    files: dict[str, Path] = {}
    missing = []
    for table, name in BUNDLE_REQUIRED.items():
        candidate = path if (table == "orders" and path.is_file()) else folder / name
        if candidate.is_file():
            files[table] = candidate
        else:
            missing.append(name)
    if missing:
        raise InputError(
            f"the fresh-order bundle at {folder} lacks {', '.join(missing)}: an order is scored with "
            "the same features it was trained with, and those need its items, payments and customer",
            context={"folder": str(folder), "missing": missing},
        )
    for table, name in BUNDLE_OPTIONAL.items():
        if (folder / name).is_file():
            files[table] = folder / name
    return files


def validate_bundle(files: dict[str, Path], config: CapstoneConfig) -> None:
    """Every file's header against stage 2's DDL: a wrong schema is refused, by file and column."""
    from data_factory.database import COLUMN_ALIASES  # stage 1's own spelling map

    # the Kaggle files spell two product columns "lenght"; stage 1 loads them through this map, so
    # the gate accepts exactly the spellings the loader accepts and nothing else
    spelled = {csv_name: db_name for db_name, csv_name in COLUMN_ALIASES.items()}
    for table, path in files.items():
        header = [spelled.get(name, name) for name in read_header(path)]
        check_schema(header, expected_columns(config, table), origin=str(path))


def score_file(path: Path, config: CapstoneConfig, *, out: Path | None = None) -> dict:
    """Validate, load through stage 1 into a staging schema, score with the trained model, rank.

    Everything happens in one transaction that is rolled back: the fresh rows are typed by stage 1's
    loader (``data_factory.database.load_csv``, the same contract the ingest used), merged into
    ``olist`` so the *same* views compute their features -- customer history included -- the three
    order-level snapshots are refreshed, the frame rows of the fresh orders are read, and then the
    database is put back exactly as it was. The model is the one the chain's model step saved.
    """
    if Path(path).is_file():
        validate_file(Path(path), config)  # a wrong orders file is refused before anything else
    files = bundle_files(Path(path))
    validate_bundle(files, config)
    artifact_path = config.features.curated_dir / "model.joblib"
    if not artifact_path.is_file():
        from .errors import NotReadyError

        raise NotReadyError(
            f"no trained model at {artifact_path}: run the chain (scripts/run_chain.py) first"
        )
    frame, staged = staged_frame(files, config)
    return rank(frame, staged, artifact_path, config, out=out)


def _sqlalchemy_url(uri: str) -> str:
    scheme, _, rest = uri.partition("://")
    return f"postgresql+psycopg://{rest}" if scheme.startswith("postgres") else uri


def staged_frame(files: dict[str, Path], config: CapstoneConfig):
    """The fresh orders' rows of v_feature_frame, computed inside a rolled-back transaction."""
    import sqlalchemy as sa
    from data_factory.database import load_csv
    from data_factory.errors import PipelineError

    from .features import database_uri
    from .system import read_frame

    engine = sa.create_engine(_sqlalchemy_url(database_uri(config, purpose="score fresh orders")))
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(sa.text(f"DROP SCHEMA IF EXISTS {STAGE_SCHEMA} CASCADE"))
                connection.execute(sa.text(f"CREATE SCHEMA {STAGE_SCHEMA}"))
                for table in LOAD_ORDER:
                    if table not in files:
                        continue
                    connection.execute(
                        sa.text(
                            f"CREATE TABLE {STAGE_SCHEMA}.{table} "
                            f"(LIKE olist.{table} INCLUDING DEFAULTS)"
                        )
                    )
                    try:
                        load_csv(connection, files[table], STAGE_SCHEMA, table, 5000)
                    except PipelineError as exc:
                        raise InputError(
                            f"{files[table].name} breaks the typed load contract: {exc} "
                            f"{getattr(exc, 'context', '')}",
                            context={"file": str(files[table])},
                        ) from exc
                staged = [
                    row[0]
                    for row in connection.execute(
                        sa.text(f"SELECT order_id FROM {STAGE_SCHEMA}.orders ORDER BY order_id")
                    )
                ]
                if not staged:
                    raise InputError(f"{files['orders']} holds no orders to score")
                if len(set(staged)) != len(staged):
                    raise InputError(f"{files['orders']} repeats an order_id")
                merge(connection, files)
                for snapshot in SNAPSHOTS:
                    connection.execute(sa.text(f"REFRESH MATERIALIZED VIEW {snapshot}"))
                # fresh statistics for what this transaction just wrote: without them the planner
                # guessed the staged table and the refreshed snapshots, and one rehearsal read took
                # minutes instead of seconds
                staged_tables = ", ".join(f"{STAGE_SCHEMA}.{table}" for table in files)
                connection.execute(sa.text(f"ANALYZE {staged_tables}, {', '.join(SNAPSHOTS)}"))
                raw = connection.connection.driver_connection
                frame = read_frame(
                    raw,
                    external=None if config.external.enabled else False,
                    where=f"f.order_id IN (SELECT order_id FROM {STAGE_SCHEMA}.orders)",
                    allow_empty=True,
                )
            except sa.exc.IntegrityError as exc:
                raise InputError(
                    "the fresh orders reference rows the database does not have (an unknown "
                    "product, seller or customer); add olist_products_dataset.csv / "
                    f"olist_sellers_dataset.csv to the bundle: {exc.orig}",
                ) from exc
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
    return frame, staged


def merge(connection, files: dict[str, Path]) -> None:
    """Put the staged rows where the views read them; an order already present is replaced."""
    import sqlalchemy as sa

    ids = f"(SELECT order_id FROM {STAGE_SCHEMA}.orders)"
    for table in ("order_reviews", "payments", "order_items", "orders"):
        connection.execute(sa.text(f"DELETE FROM olist.{table} WHERE order_id IN {ids}"))
    for table in LOAD_ORDER:
        if table not in files:
            continue
        conflict = " ON CONFLICT DO NOTHING" if table in ("customers", "sellers", "products") else ""
        connection.execute(
            sa.text(f"INSERT INTO olist.{table} SELECT * FROM {STAGE_SCHEMA}.{table}{conflict}")
        )


def rank(frame, staged: list[str], artifact_path: Path, config: CapstoneConfig, *, out=None) -> dict:
    """Score with the saved pipeline, rank, cut at the quota, write the CSV."""
    import csv
    from datetime import UTC, datetime

    import joblib

    from .capacity import top_share_indexes

    artifact = joblib.load(artifact_path)
    columns = list(artifact["numeric"]) + list(artifact["categorical"])
    absent = [column for column in columns if column not in frame.columns]
    if absent:
        raise InputError(f"the frame lacks {absent}, which the saved model was trained on")
    scores = artifact["pipeline"].predict_proba(frame[columns])[:, 1] if len(frame) else []
    scored = sorted(
        zip(frame["order_id"].tolist(), [float(v) for v in scores], strict=True),
        key=lambda pair: (-pair[1], pair[0]),
    )
    chosen = set(top_share_indexes(len(scored), config.capacity.share))
    target = Path(out) if out else (
        config.features.curated_dir / f"predictions_{datetime.now(UTC):%Y%m%d-%H%M%S}.csv"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rank", "order_id", "score", "follow_up", "model_run_id"])
        for index, (order_id, score) in enumerate(scored):
            writer.writerow([index + 1, order_id, f"{score:.6f}", int(index in chosen), artifact["run_id"]])
    unscored = sorted(set(staged) - {order_id for order_id, _ in scored})
    return {
        "output": str(target),
        "scored": len(scored),
        "follow_up": len(chosen),
        "not_scored_no_approval": len(unscored),
        "model": artifact["model"],
        "model_run_id": artifact["run_id"],
    }
