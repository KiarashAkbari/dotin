#!/usr/bin/env python3
"""Store twenty external rows for *this* sandbox corpus, through the two functions 005 allows.

A helper for ``frame-performance-sandbox.py``, and deliberately small: the external table's job in that
log is to show that the join path works end to end -- that rows arrive, that the pivot finds them, that
``capstone.v_external_match_rate`` moves -- and not to pretend to be IBGE. So the values are made up here,
the keys are taken from the cities this cluster's own customers actually carry, and the whole thing is
refused unless the caller says the database is disposable.

Both mutators in ``sql/005_external_signal.sql`` are exercised, in the order a real reload would use
them:

1. ``capstone.refresh_external_municipio('sandbox_synthetic')`` removes only this source's rows, so
   re-running the demo is a refresh and never a duplicate;
2. ``external.load`` -> ``capstone.upsert_external_municipio`` writes the new ones, which is also the
   path ``docs/evidence/../src/capstone/external.py`` uses in production, casts included.

Run from ``stage-4/`` against a cluster you own::

    CAPSTONE_SMOKE_ALLOW_DESTROY=1 CAPSTONE_DATABASE_URI="postgresql://…" \
      python3 docs/evidence/frame-performance-external-demo.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

STAGE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(STAGE / "src"))

from capstone import external  # noqa: E402
from capstone.config import load_contract  # noqa: E402
from capstone.errors import InputError  # noqa: E402
from capstone.features import connect, database_uri  # noqa: E402

#: the label the table stores, and the label ``refresh`` will later clean up
SOURCE = "sandbox_synthetic"
KEYS = 20


def main(argv: list[str]) -> int:
    if os.environ.get("CAPSTONE_SMOKE_ALLOW_DESTROY", "").strip() != "1":
        print(
            "refusing: this writes rows into capstone.external_municipio, so it needs\n"
            "CAPSTONE_SMOKE_ALLOW_DESTROY=1 to say the target database is disposable. Nothing\n"
            "was written and nothing was deleted."
        )
        return 0
    config = load_contract(STAGE / "config" / "capstone.example.toml")
    try:
        uri = database_uri(config, purpose="demo the external signal on a scratch cluster")
    except InputError as error:
        print(f"refusing: {str(error).strip()}")
        return 0
    connection = connect(uri, autocommit=True, purpose="demo the external signal on a scratch cluster")
    try:
        cursor = connection.cursor()
        removed = None
        try:
            cursor.execute("SELECT capstone.refresh_external_municipio(%s)", (SOURCE,))
            removed = cursor.fetchone()[0]
        except Exception as error:  # noqa: BLE001 - a missing function is the server's message, not ours
            # ``sql/005`` not applied is UndefinedFunction, which no error class in this stage owns;
            # the point is to say what could not be cleaned up and keep going to the report
            print(f"refresh refused ({type(error).__name__}): {str(error).strip()[:160]}")
        print(f"refresh: {removed} row(s) of source {SOURCE!r} removed before the reload")
        cursor.execute(
            "SELECT lower(customer_city), lower(customer_state), count(*) FROM olist.customers"
            " GROUP BY 1, 2 ORDER BY 3 DESC, 1 LIMIT %s",
            (KEYS,),
        )
        cities = cursor.fetchall()
        if not cities:
            print("no customer cities to key on; this cluster has no olist.customers rows")
            return 0
        rows = [
            external.SignalRow(
                # invented, but shaped like the real thing: seven digits, and stable per key so a
                # re-run upserts the same row instead of adding a second one
                ibge_code=str(3500000 + index * 1111),
                city_state=f"{city}|{uf}",
                indicator="renda_mensal_por_capita",
                period="2022",
                value=900.0 + 120.0 * index,
            )
            for index, (city, uf, _orders) in enumerate(cities)
        ]
        stored = external.load(cursor, rows, source=SOURCE)
        print(f"stored: {stored} row(s) via capstone.upsert_external_municipio, {rows[0].city_state!r} first")
        rate = external.match_rate(cursor)
        print(
            f"match rate: {rate['matched_keys']} of {rate['source_keys']} keys"
            f" = {rate['key_match_rate']}, external table holds {rate['external_keys']}"
        )
        print(
            "note: the rate is small because the synthetic corpus spreads invented city names thin;"
            "\n      on the real corpus the denominator is the 4,119 distinct customer cities, and the"
            "\n      number that matters is measured by the student's run, not by this demo"
        )
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
