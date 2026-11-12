"""Configuration for the credit source, read the way stage 1 reads its own.

Nothing here is a magic value: paths, page size, batch size and the quarantine
limit all come from a TOML file. Two locations are accepted, in this order:

1. ``config/credit.toml`` next to the notebook (or wherever ``--credit-config``
   points) - a stage-3 file, so stage 3 can add a source without editing the
   shared config;
2. a ``[credit]`` section inside the stage-1 ``config/default.toml`` - the place
   it belongs once the section is adopted, and stage 1 ignores unknown sections,
   so adding it there cannot break the factory.

The database settings are *not* duplicated here: they are read through
``data_factory.config.load_config`` so the password still comes from the single
``database.password_env`` the previous stages established.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from data_factory.errors import ConfigError

#: the only schema this stage writes, and the name the migrations create
CREDIT_SCHEMA = "credit"
#: a share this large means the file is not what the contract describes
DEFAULT_QUARANTINE_MAX_SHARE = 0.05
DEFAULT_QUARANTINE_MIN_ROWS = 1_000
REQUIRED_KEYS = ("csv_path", "sql_migrations_dir")
#: keys a config must not carry, with the reason they were removed
_REMOVED_KEYS = {
    "migration": (
        "every *.sql in credit.sql_migrations_dir is applied in filename "
        "order, so the directory is the list; delete this line"
    )
}


@dataclass(frozen=True)
class CreditConfig:
    """Everything the credit ingest needs, and nothing it hard-codes."""

    csv_path: Path
    sql_migrations_dir: Path
    schema: str = CREDIT_SCHEMA
    page_size: int = 10_000
    batch_size: int = 5_000
    quarantine_max_share: float = DEFAULT_QUARANTINE_MAX_SHARE
    quarantine_min_rows: int = DEFAULT_QUARANTINE_MIN_ROWS
    curated_dir: Path = Path("data/curated")
    raw_pages_dir: Path = Path("data/raw/credit-pages")
    quarantine_dir: Path = Path("data/quarantine")
    manifest_name: str = "credit_run_manifest.json"

    @property
    def manifest_path(self) -> Path:
        return self.curated_dir / self.manifest_name


def _positive_int(values: dict, key: str, default: int, *, minimum: int = 1) -> int:
    raw = values.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
        raise ConfigError(f"credit.{key} must be an integer >= {minimum}")
    return int(raw)


def _ratio(values: dict, key: str, default: float) -> float:
    raw = values.get(key, default)
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not 0.0 <= float(raw) <= 1.0
    ):
        raise ConfigError(f"credit.{key} must be a number between 0 and 1")
    return float(raw)


#: defaults live here as strings so they resolve against the config file like any
#: other path; a default that skipped that step would point at whatever directory
#: the process happened to start in, and two runs would quietly share a page store
DEFAULT_CURATED_DIR = "../data/curated"
DEFAULT_RAW_PAGES_DIR = "../data/raw/credit-pages"
DEFAULT_QUARANTINE_DIR = "../data/quarantine"


def _path(values: dict, key: str, base: Path, *, default: str | None = None) -> Path:
    raw = values.get(key, default)
    if raw is None:
        raise ConfigError(f"Missing config key: credit.{key}")
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"credit.{key} must be a non-empty string path")
    path = Path(raw)
    return path if path.is_absolute() else (base / path).resolve()


def parse_credit_config(
    values: dict, *, base: Path, relative_to: Path | None = None
) -> CreditConfig:
    """Build a :class:`CreditConfig` from a mapping of TOML keys.

    Relative paths resolve against ``base``, which is the directory of the config
    file - the same rule stage 1 uses, so the same ``../`` prefixes work in both.
    """
    for key in REQUIRED_KEYS:
        if key not in values:
            raise ConfigError(f"Missing config key: credit.{key}")
    for key, reason in _REMOVED_KEYS.items():
        if key in values:
            raise ConfigError(f"credit.{key} is no longer a setting: {reason}")
    if not isinstance(values.get("csv_path"), str) or not values["csv_path"].strip():
        raise ConfigError("credit.csv_path must be a non-empty string path")
    anchor = relative_to or base
    schema = str(values.get("schema", CREDIT_SCHEMA))
    if not schema.replace("_", "").isalnum() or schema[0].isdigit():
        raise ConfigError(f"credit.schema must be a SQL identifier, got {schema!r}")
    return CreditConfig(
        csv_path=_path(values, "csv_path", anchor),
        sql_migrations_dir=_path(values, "sql_migrations_dir", anchor),
        schema=schema,
        page_size=_positive_int(values, "page_size", 10_000),
        batch_size=_positive_int(values, "batch_size", 5_000),
        quarantine_max_share=_ratio(
            values, "quarantine_max_share", DEFAULT_QUARANTINE_MAX_SHARE
        ),
        quarantine_min_rows=_positive_int(
            values, "quarantine_min_rows", DEFAULT_QUARANTINE_MIN_ROWS
        ),
        curated_dir=_path(values, "curated_dir", anchor, default=DEFAULT_CURATED_DIR),
        raw_pages_dir=_path(
            values, "raw_pages_dir", anchor, default=DEFAULT_RAW_PAGES_DIR
        ),
        quarantine_dir=_path(
            values, "quarantine_dir", anchor, default=DEFAULT_QUARANTINE_DIR
        ),
        manifest_name=str(values.get("manifest_name", "credit_run_manifest.json")),
    )


def read_toml(path: Path) -> dict:
    path = Path(path)
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc


def missing_config_message(path: Path) -> str:
    """Describe an absent config file by what the reader can do about it.

    The path alone sent someone to hunt through a README for a command; the example
    file is sitting next to it, so the message names it only when it is really there.
    """
    message = f"Config file not found: {path}"
    example = path.parent / "credit.example.toml"
    if example.exists():
        return (
            f"{message}. Copy the example: cp {example} {path} "
            "- then set credit.csv_path to the training file"
        )
    return f"{message}. It must contain a [credit] table with credit.csv_path"


def load_credit_config(path: Path) -> CreditConfig:
    """Load a standalone stage-3 config file containing a ``[credit]`` section."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(missing_config_message(path))
    data = read_toml(path)
    section = data.get("credit")
    if not isinstance(section, dict):
        raise ConfigError(f"Missing config section: [credit] in {path}")
    return parse_credit_config(section, base=Path(path).resolve().parent)


def credit_config_from_mapping(data: dict, *, base: Path) -> CreditConfig | None:
    """Pick up a ``[credit]`` section from any parsed TOML, or report absence."""
    section = data.get("credit")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError("config key [credit] must be a table")
    return parse_credit_config(section, base=base)
