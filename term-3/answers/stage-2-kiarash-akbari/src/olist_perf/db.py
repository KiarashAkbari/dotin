"""Engine construction and session settings for the stage-2 measurements."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import sqlalchemy as sa
from data_factory.config import load_config
from data_factory.database import build_database_url

STAGE2_DIR = Path(__file__).resolve().parents[2]
STAGE1_DIR = STAGE2_DIR.parent / "stage-1-kiarash-akbari"
CONFIG_ENV = "OLIST_CONFIG"
APPLICATION_NAME = "olist_perf"

# Benchmark session settings. They are set per session with SET (never
# ALTER SYSTEM), so they do not change the server for anyone else.
#   jit=off: JIT compilation adds 10-100 ms of noise to sub-second queries.
#   max_parallel_workers_per_gather=0: the measurement machine is a shared
#     2-vCPU box; parallel workers made timings depend on the other load.
#   work_mem=32MB: every hash and sort of these queries fits in memory.
SESSION_SETTINGS = {
    "jit": "off",
    "max_parallel_workers_per_gather": "0",
    "work_mem": "32MB",
    "statement_timeout": "120s",
}


def config_path() -> Path:
    """Return the stage-1 config the notebook uses.

    ``OLIST_CONFIG`` (absolute, or relative to the stage-2 folder) wins over
    the committed ``../stage-1-kiarash-akbari/config/default.toml``.
    """
    override = os.environ.get(CONFIG_ENV)
    if override:
        path = Path(override)
        return path if path.is_absolute() else (STAGE2_DIR / path).resolve()
    return STAGE1_DIR / "config" / "default.toml"


def make_engine(path: Path | None = None) -> sa.Engine:
    """Build an engine from the stage-1 config; the password comes from env only.

    Fails fast (no prompt) when the password variable is unset, so a headless
    Restart & Run All never blocks waiting for input.
    """
    cfg = load_config(path or config_path())
    if cfg.database is None:
        raise RuntimeError("The [database] section is missing from the config.")
    variable = cfg.database.password_env
    if variable and not os.environ.get(variable):
        raise RuntimeError(
            f"Set the {variable} environment variable before running the notebook."
        )
    return sa.create_engine(
        build_database_url(cfg),
        pool_pre_ping=True,
        # prepare_threshold=None: psycopg would otherwise turn a statement run
        # five times into a server-side prepared statement and keep its plan;
        # every measured run here is planned fresh, like the EXPLAIN beside it.
        connect_args={"application_name": APPLICATION_NAME,
                      "prepare_threshold": None},
    )


def apply_session_settings(connection: sa.Connection) -> None:
    """SET the benchmark settings on this session (bound values, fixed names)."""
    for name, value in SESSION_SETTINGS.items():
        connection.execute(
            sa.text("SELECT set_config(:name, :value, false)"),
            {"name": name, "value": value},
        )


@contextmanager
def session(engine: sa.Engine) -> Iterator[sa.Connection]:
    """A connection with the benchmark settings; always returned to the pool."""
    with engine.connect() as connection:
        apply_session_settings(connection)
        connection.commit()
        yield connection


OLIST_TABLES = ("customers", "sellers", "product_category_name_translation",
                "products", "orders", "order_items", "payments", "order_reviews",
                "geolocation", "monthly_category_snapshot")


def vacuum_analyze(engine: sa.Engine) -> list[dict]:
    """VACUUM (ANALYZE) every olist table; returns per-table elapsed ms.

    A bulk load leaves no visibility map (Index Only Scans fall back to heap
    fetches) and no planner statistics until autovacuum gets to the tables.
    Running it explicitly makes the first measurement after a load the same
    as the tenth. VACUUM cannot run in a transaction, hence AUTOCOMMIT.
    """
    from time import perf_counter

    from psycopg import sql as pgsql

    out = []
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        raw = connection.connection.driver_connection
        for table in OLIST_TABLES:
            started = perf_counter()
            raw.execute(pgsql.SQL("VACUUM (ANALYZE) {}").format(
                pgsql.Identifier("olist", table)))
            out.append({"table": table,
                        "elapsed_ms": round((perf_counter() - started) * 1000, 1)})
    return out


def leftover_sessions(engine: sa.Engine) -> list[dict]:
    """Sessions this package opened that are still connected to the database.

    Checked from a separate, unpooled probe connection (its own application
    name), so the probe itself is never counted. Call after engine.dispose().
    """
    probe = sa.create_engine(
        engine.url, poolclass=sa.pool.NullPool,
        connect_args={"application_name": "olist_perf_probe"},
    )
    try:
        with probe.connect() as connection:
            return [dict(row) for row in connection.execute(
                sa.text(
                    "SELECT pid, state, backend_start FROM pg_stat_activity "
                    "WHERE datname = current_database() AND application_name = :app"
                ),
                {"app": APPLICATION_NAME},
            ).mappings()]
    finally:
        probe.dispose()
