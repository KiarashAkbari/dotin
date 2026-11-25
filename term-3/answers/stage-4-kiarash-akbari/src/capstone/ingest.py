"""The ingest layer of the capstone: Olist CSVs into PostgreSQL through stage 1's own loader.

``04`` says the receive layer *is* stage 1's package, with its schema contract. So this module
does not load a single row itself. It does three small things around ``data_factory``:

1. checks that the nine Olist CSVs are where the capstone config says, with the header stage 2's
   schema expects, and counts their records (the counts go to the chain manifest; a full public
   Olist dump is recognised by its known counts, a smaller file -- the synthetic e2e fixture, or a
   sealed cut -- is accepted and reported as such);
2. writes a stage-1 config whose every path is absolute, because stage 1 resolves paths against
   its working directory (review B4: run from stage 4, the old chain died on
   ``config/default.toml``; run from stage 1, it started the NYC-311 download);
3. builds the ``load-database --reset --skip-factory-sink`` command, run with ``cwd`` = stage 1
   and the database password passed only through the environment.

The Olist folder comes from ``[ingest] olist_csv_dir`` in the capstone config (relative to the
config file), overridden by ``CAPSTONE_OLIST_DIR``. Raw data is never committed.
"""

from __future__ import annotations

import csv
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .config import CapstoneConfig
from .errors import CapstoneError, ConfigError, InputError

#: the file each stage-2 table is loaded from (mirrors data_factory.database.OLIST_CSV_FILES)
OLIST_FILES = {
    "product_category_name_translation": "product_category_name_translation.csv",
    "customers": "olist_customers_dataset.csv",
    "sellers": "olist_sellers_dataset.csv",
    "products": "olist_products_dataset.csv",
    "geolocation": "olist_geolocation_dataset.csv",
    "orders": "olist_orders_dataset.csv",
    "order_items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "order_reviews": "olist_order_reviews_dataset.csv",
}

#: record counts of the public Kaggle dump (olistbr/brazilian-ecommerce); used to *label* a run
#: as the full dataset, never to refuse a smaller one
PUBLIC_OLIST_ROWS = {
    "product_category_name_translation": 71,
    "customers": 99441,
    "sellers": 3095,
    "products": 32951,
    "geolocation": 1000163,
    "orders": 99441,
    "order_items": 112650,
    "payments": 103886,
    "order_reviews": 99224,
}

#: the columns a file must carry (first columns of each stage-2 table); a file without them is
#: the wrong file, and loading it would fail later with a far worse message
REQUIRED_HEADERS = {
    "product_category_name_translation": ("product_category_name", "product_category_name_english"),
    "customers": ("customer_id", "customer_unique_id", "customer_zip_code_prefix"),
    "sellers": ("seller_id", "seller_zip_code_prefix", "seller_city", "seller_state"),
    "products": ("product_id", "product_category_name"),
    "geolocation": ("geolocation_zip_code_prefix", "geolocation_lat", "geolocation_lng"),
    "orders": ("order_id", "customer_id", "order_status", "order_purchase_timestamp",
               "order_approved_at"),
    "order_items": ("order_id", "order_item_id", "product_id", "seller_id", "price"),
    "payments": ("order_id", "payment_sequential", "payment_type", "payment_value"),
    "order_reviews": ("review_id", "order_id", "review_score"),
}

OLIST_DIR_ENV = "CAPSTONE_OLIST_DIR"
DATABASE_URI_ENV = "CAPSTONE_DATABASE_URI"
#: the variable stage 1 reads the password from (stage-1 config.toml ``password_env``)
STAGE1_PASSWORD_ENV = "OLIST_DB_PASSWORD"
DEFAULT_OLIST_DIR = "../data/raw/olist"
DEFAULT_STAGE1_DIR = "../../stage-1-kiarash-akbari"


@dataclass(frozen=True)
class IngestPlan:
    """Everything the ingest step needs to run stage 1, and nothing secret."""

    olist_dir: Path
    stage1_dir: Path
    stage1_config: Path
    command: tuple[str, ...]
    counts: dict[str, int]
    full_public_olist: bool


def _ingest_section(config: CapstoneConfig) -> dict:
    with config.source.open("rb") as handle:
        raw = tomllib.load(handle)
    section = raw.get("ingest", {})
    if not isinstance(section, dict):
        raise ConfigError("[ingest] must be a table")
    return section


def olist_dir(config: CapstoneConfig, environ: dict[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    if environ.get(OLIST_DIR_ENV):
        return Path(environ[OLIST_DIR_ENV]).expanduser().resolve()
    relative = _ingest_section(config).get("olist_csv_dir", DEFAULT_OLIST_DIR)
    return (config.source.parent / relative).resolve()


def stage1_dir(config: CapstoneConfig) -> Path:
    relative = _ingest_section(config).get("stage1_dir", DEFAULT_STAGE1_DIR)
    path = (config.source.parent / relative).resolve()
    if not (path / "src" / "data_factory").is_dir():
        raise ConfigError(
            f"stage 1 is not at {path}: [ingest] stage1_dir must point at the stage-1 folder "
            "whose package is the capstone's receive layer"
        )
    return path


def count_records(path: Path) -> tuple[list[str], int]:
    """Header and number of CSV *records* (review comments hold newlines, so not lines)."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        return header, sum(1 for _ in reader)


def verify_files(directory: Path) -> dict[str, int]:
    """Refuse a missing or wrong file; return the record count of each one."""
    if not directory.is_dir():
        raise InputError(
            f"Olist CSV folder {directory} does not exist; put the nine Kaggle files there or "
            f"set {OLIST_DIR_ENV}",
            context={"olist_dir": str(directory)},
        )
    counts: dict[str, int] = {}
    problems: list[str] = []
    for table, name in OLIST_FILES.items():
        path = directory / name
        if not path.is_file():
            problems.append(f"{name}: missing")
            continue
        header, rows = count_records(path)
        absent = [column for column in REQUIRED_HEADERS[table] if column not in header]
        if absent:
            problems.append(f"{name}: header lacks {', '.join(absent)}")
            continue
        counts[table] = rows
    if problems:
        raise InputError(
            "the Olist folder is not the dataset stage 2's schema expects: " + "; ".join(problems),
            context={"olist_dir": str(directory), "problems": problems},
        )
    return counts


def stage1_url(uri: str) -> tuple[str, str | None]:
    """The SQLAlchemy URL stage 1 wants, without the password, and the password apart."""
    parts = urlsplit(uri)
    if parts.scheme not in ("postgresql", "postgres", "postgresql+psycopg"):
        raise ConfigError(f"{DATABASE_URI_ENV} must be a postgresql:// URI")
    host = parts.hostname or "localhost"
    netloc = f"{parts.username}@{host}" if parts.username else host
    if parts.port:
        netloc += f":{parts.port}"
    clean = urlunsplit(("postgresql+psycopg", netloc, parts.path, "", ""))
    return clean, parts.password


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_stage1_config(
    stage1: Path, directory: Path, url: str, target: Path
) -> Path:
    """Stage 1's own config with an absolute ``[database]`` section pointing at our Olist."""
    base = (stage1 / "config" / "default.toml").read_text(encoding="utf-8")
    head = base.split("[database]", 1)[0].rstrip()
    stage2 = (stage1.parent / "stage-2-kiarash-akbari").resolve()
    database = "\n".join(
        [
            "[database]",
            f"url = {_toml_string(url)}",
            f"password_env = {_toml_string(STAGE1_PASSWORD_ENV)}",
            f"olist_csv_dir = {_toml_string(str(directory))}",
            f"stage1_curated_dir = {_toml_string(str(stage1 / 'data' / 'curated'))}",
            "batch_size = 5000",
            f"sql_migrations_dir = {_toml_string(str(stage2 / 'sql'))}",
        ]
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# written by capstone.ingest for one chain run; no password in here\n"
        + head + "\n\n" + database + "\n",
        encoding="utf-8",
    )
    return target


def plan(config: CapstoneConfig, environ: dict[str, str] | None = None) -> IngestPlan:
    environ = os.environ if environ is None else environ
    uri = environ.get(DATABASE_URI_ENV, "")
    if not uri:
        raise ConfigError(f"{DATABASE_URI_ENV} is not set; the ingest step needs a database")
    directory = olist_dir(config, environ)
    counts = verify_files(directory)
    stage1 = stage1_dir(config)
    url, _ = stage1_url(uri)
    target = config.features.curated_dir / "stage1-olist.toml"
    write_stage1_config(stage1, directory, url, target)
    command = (
        sys.executable, "-m", "data_factory", "load-database",
        "--config", str(target.resolve()), "--reset", "--skip-factory-sink",
    )
    return IngestPlan(
        olist_dir=directory,
        stage1_dir=stage1,
        stage1_config=target.resolve(),
        command=command,
        counts=counts,
        full_public_olist=counts == PUBLIC_OLIST_ROWS,
    )


def subprocess_env(plan_: IngestPlan, environ: dict[str, str] | None = None) -> dict[str, str]:
    """The child's environment: stage 1 importable, the password only as a variable."""
    environ = dict(os.environ if environ is None else environ)
    _, password = stage1_url(environ.get(DATABASE_URI_ENV, ""))
    if password and not environ.get(STAGE1_PASSWORD_ENV):
        environ[STAGE1_PASSWORD_ENV] = password
    src = str(plan_.stage1_dir / "src")
    environ["PYTHONPATH"] = os.pathsep.join(
        [src, *[p for p in environ.get("PYTHONPATH", "").split(os.pathsep) if p]]
    )
    return environ


class Stage1Failure(CapstoneError):
    """Stage 1's loader failed; the chain passes its exit code on instead of flattening it (B20)."""

    def __init__(self, message: str, exit_code: int, context: dict | None = None) -> None:
        super().__init__(message, context=context)
        self.exit_code = exit_code


def verify(config: CapstoneConfig) -> dict:
    """The ``ingest`` chain step: the nine files are there, with the right headers; counts."""
    directory = olist_dir(config)
    counts = verify_files(directory)
    return {
        "olist_dir": str(directory),
        "records": counts,
        "full_public_olist": counts == PUBLIC_OLIST_ROWS,
    }


def load(config: CapstoneConfig, *, run=None) -> dict:
    """The ``load-database`` chain step: stage 1's ``load-database --reset``, from stage 1's folder."""
    import subprocess

    plan_ = plan(config)
    runner = run or subprocess.run
    result = runner(
        list(plan_.command),
        cwd=str(plan_.stage1_dir),
        env=subprocess_env(plan_),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-600:]
        raise Stage1Failure(
            f"stage 1's load-database exited {result.returncode}: {tail}",
            exit_code=result.returncode,
            context={"command": " ".join(plan_.command), "cwd": str(plan_.stage1_dir)},
        )
    loaded = {}
    for line in (result.stdout or "").splitlines():
        if line.startswith("table=") and " loaded=" in line:
            name, _, count = line[len("table="):].partition(" loaded=")
            loaded[name] = int(count)
    return {"command": " ".join(plan_.command[1:]), "cwd": str(plan_.stage1_dir), "loaded": loaded,
            "files": plan_.counts}
