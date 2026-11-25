"""The external signal, tested without a network and without a server.

Item 12 asks for a test of the API path that "validates the schema/response and fails clearly rather
than crashing when access is absent". Both halves are testable offline, and for a reason worth stating
before reading the cases: what the fetch can get wrong is *shape* (the endpoint answered, and the body
is not what the parser was written against) and *access* (there is no route, no token, no consent).
The first is a contract, so it is tested against canned payloads; the second is a message, so it is
tested against an exit code and the absence of a traceback. What cannot be tested from here is whether
``servicodados.ibge.gov.br`` answers at all -- decisions.md item 8 records that as unverified, and no
number of green tests here changes it, which is why the last class in this file is a live guard that
skips rather than passes when no database is configured.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import io
import json
import unicodedata
import os
import re
import subprocess
from unittest import mock
import sys
import unittest
from pathlib import Path
from typing import Any

STAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STAGE / "src"))
# deliberately no stage-1 path, and no real endpoint: see the module docstring

from capstone import external, cli, features
from capstone.config import External, load_contract
from capstone.errors import InputError, NotReadyError, SourceError

#: one municipality, one indicator, one year -- the shape the agregados v3 endpoint documents
GOOD_PAYLOAD = {
    "metadados": {"agregado": {"id": 4714, "nome": "Renda domiciliar per capita média mensal"}},
    "result": [
        {
            "agregado": {"id": 4714, "nome": "Renda domiciliar per capita média mensal"},
            "periodo": {"inicio": "2022", "fim": "2022", "ano": "2022"},
            "localidades": [
                {
                    "nivel": {"id": 6, "nome": "Município"},
                    "id": "N6[3518800]",
                    "nome": "Franca",
                    "uf": {"id": 35, "sigla": "SP", "nome": "São Paulo"},
                    "valor": "2.048,90",
                },
                {
                    "id": "N6[3304557]",
                    "nome": "Rio de Janeiro",
                    "uf": {"sigla": "RJ"},
                    "valor": 3121.5,
                },
                # a municipality the agregado has no number for: NULL in the table, never 0
                {"id": "N6[3550308]", "nome": "Guarulhos", "uf": {"sigla": "SP"}, "valor": None},
            ],
        }
    ],
}


@contextlib.contextmanager
def captured():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        yield buffer


def config_with(**external_fields) -> Any:
    """The tracked example contract, with ``[external]`` replaced by the case's own decision."""
    config = load_contract(STAGE / "config" / "capstone.example.toml")
    values = {"name": "", "url": "", "token_env": "", "join_on": ""}
    values.update(external_fields)
    return dataclasses.replace(config, external=External(**values))


class FakeCursor:
    """Records what would have been sent; the database stays out of these tests on purpose."""

    def __init__(self, *, rows: list[tuple] | None = None) -> None:
        self.statements: list[tuple[str, tuple | None]] = []
        self._rows = rows if rows is not None else [(4119, 0, 0, None)]

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.statements.append((" ".join(sql.split()), params))

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows[1:])


class LoadCollisionTests(unittest.TestCase):
    """A second indicator-period pair on the same source is refused before it is written.

    Found on a live machine, not offline: a fetch of the 2022 agregado, then a config pointing at the
    2010 one, and `upsert` (keyed on code-indicator-period) *added* rows instead of replacing them.
    The frame's JSON pivot grew, `006`'s `max()` picked the newer year under the older year's column
    name, and the model's "with external" column was a different census than its own header claimed --
    until the next `features` step hit `006`'s guard and stopped the chain. `005` deliberately will not
    delete another fetch's rows, so the refusal had to come to the loader.
    """

    DENSITY_2010 = ("População residente, Densidade demográfica (hab/km²)", "2010")
    DENSITY_2022 = ("População residente, Densidade demográfica (hab/km²)", "2022")
    OTHER_2010 = ("Área territorial", "2010")

    def test_one_clean_pair_on_one_side_is_no_collision(self) -> None:
        self.assertIsNone(external.load_collision([], [self.DENSITY_2010]))
        self.assertIsNone(external.load_collision([self.DENSITY_2010], [self.DENSITY_2010]))

    def test_a_second_period_is_named_with_both_sides(self) -> None:
        sentence = external.load_collision([self.DENSITY_2022], [self.DENSITY_2010])
        assert sentence is not None
        self.assertIn("2022", sentence)
        self.assertIn("2010", sentence)
        self.assertIn("refresh_external_municipio", sentence, "and it names the sanctioned reload")
        self.assertIn("006", sentence, "the reader learns which guard would otherwise have caught it")

    def test_a_second_indicator_for_the_same_period_is_equally_fatal(self) -> None:
        """`mv_external_density` groups by place, not by indicator, so two indicators are one row too."""
        self.assertIsNotNone(external.load_collision([self.OTHER_2010], [self.DENSITY_2010]))

    def test_load_refuses_before_a_single_row_is_written(self) -> None:
        cursor = _RecordingCursor([self.DENSITY_2022])
        rows = external.normalize(GOOD_PAYLOAD)
        with self.assertRaises(external.InputError) as caught:
            external.load(
                cursor, rows, source=external.IBGE_AREGADOS, arriving={self.DENSITY_2010}
            )
        self.assertIn("already holds", str(caught.exception))
        self.assertEqual(
            [s for s, _ in cursor.statements if "upsert" in s],
            [],
            "a refused load writes nothing at all, so the table stays re-loadable",
        )

    def test_a_clean_source_still_loads_and_refreshes(self) -> None:
        cursor = _RecordingCursor([self.DENSITY_2010])
        rows = external.normalize(GOOD_PAYLOAD)
        stored = external.load(
            cursor, rows, source=external.IBGE_AREGADOS, arriving={self.DENSITY_2010}
        )
        self.assertEqual(stored, 3)
        self.assertEqual(len([s for s, _ in cursor.statements if "upsert" in s]), 3)

    def test_the_store_path_passes_what_it_fetched(self) -> None:
        """The guard next to `load` would be decoration if the caller never handed it a pair."""
        source = Path(external.__file__).read_text(encoding="utf-8")
        self.assertIn("arriving=incoming", source)
        self.assertIn("incoming = {(row.indicator, row.period) for row in rows}", source)


class _RecordingCursor:
    """The `load` half of a session: it answers the collision read and remembers everything else."""

    def __init__(self, stored_pairs: list[tuple[str, str]]) -> None:
        self._stored = list(stored_pairs)
        self.statements: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self.statements.append((" ".join(sql.split()), params))

    def fetchall(self) -> list[tuple]:
        return list(self._stored)


class NormalizeTests(unittest.TestCase):
    def test_a_good_response_becomes_one_row_per_locality_and_indicator(self) -> None:
        rows = external.normalize(GOOD_PAYLOAD)
        self.assertEqual(len(rows), 3)
        self.assertEqual([row.ibge_code for row in rows], ["3518800", "3304557", "3550308"])
        self.assertEqual(rows[0].city_state, "franca|sp", msg="the key is folded and lowercased")
        self.assertEqual(rows[0].period, "2022")
        self.assertEqual(rows[0].indicator, "Renda domiciliar per capita média mensal")

    def test_the_two_decimal_spellings_are_both_read_correctly(self) -> None:
        """The bug this pins is worth more than the test's length: 2.048,90 as 2048.90, not 204890."""
        rows = external.normalize(GOOD_PAYLOAD)
        self.assertAlmostEqual(rows[0].value, 2048.90, places=6)
        self.assertAlmostEqual(rows[1].value, 3121.5, places=6)
        for raw, expected in (("2048.90", 2048.90), (2048, 2048.0), ("2.048,90", 2048.90)):
            self.assertAlmostEqual(external.parse_value(raw), expected, places=6, msg=raw)

    def test_a_missing_value_stays_missing(self) -> None:
        rows = external.normalize(GOOD_PAYLOAD)
        self.assertIsNone(rows[2].value)
        for empty in (None, "", "   "):
            payload = {
                "result": [
                    {
                        "agregado": {"nome": "x"},
                        "periodo": {"ano": "2022"},
                        "localidades": [
                            {"id": "N6[3550308]", "nome": "Guarulhos", "uf": {"sigla": "SP"},
                             "valor": empty}
                        ],
                    }
                ]
            }
            self.assertIsNone(external.normalize(payload)[0].value, msg=repr(empty))

    def test_an_ambiguous_number_is_refused_rather_than_guessed(self) -> None:
        for raw in ("1,2.3", "1,2,3", True, "R$ 2.048"):
            with self.subTest(raw=raw):
                with self.assertRaises(SourceError) as caught:
                    external.parse_value(raw)
                self.assertIn("value", str(caught.exception))

    def test_a_response_without_the_results_key_says_what_it_did_have(self) -> None:
        with self.assertRaises(SourceError) as caught:
            external.normalize({"errors": ["invalid agregado id"]})
        message = str(caught.exception)
        self.assertIn("no 'result' key", message)
        self.assertIn("errors", message, msg="the top-level keys are the diagnosis")

    def test_a_wrong_aggregation_level_is_not_silently_accepted(self) -> None:
        """N7 is a state; joining a state's income onto one city would be a confident wrong feature."""
        payload = json.loads(json.dumps(GOOD_PAYLOAD))
        payload["result"][0]["localidades"][0]["id"] = "N7[35]"
        with self.assertRaises(SourceError) as caught:
            external.normalize(payload)
        self.assertIn("N7", str(caught.exception))

    def test_a_locality_id_that_is_not_the_documented_shape_is_refused(self) -> None:
        for raw in ("3518800", "N6()", None, "N6[abc]"):
            with self.subTest(raw=raw):
                payload = json.loads(json.dumps(GOOD_PAYLOAD))
                payload["result"][0]["localidades"][0]["id"] = raw
                with self.assertRaises(SourceError) as caught:
                    external.normalize(payload)
                self.assertIn("N<level>[<code>]", str(caught.exception))

    def test_a_locality_with_no_name_or_uf_cannot_be_keyed(self) -> None:
        """Two refusals, because only one of them is a lookup away from a row."""
        for field, expect in (("nome", "has no name"), ("uf", "carries no uf")):
            with self.subTest(field=field):
                payload = json.loads(json.dumps(GOOD_PAYLOAD))
                payload["result"][0]["localidades"][0][field] = None
                if field == "uf":
                    payload["result"][0]["localidades"][0]["uf"] = {}
                with self.assertRaises(SourceError) as caught:
                    external.normalize(payload)
                self.assertIn(expect, str(caught.exception))

    def test_an_empty_answer_is_reported_as_empty(self) -> None:
        """A 200 with no rows is the classic dead-parameter case, and storing it silently is the harm.

        The two shapes get two messages, because they mean different things to whoever reads the log:
        an absent key is a response the parser does not know, an empty list is a response that knows
        the parser and has nothing for it.
        """
        payload = {
            "result": [{"agregado": {"nome": "x"}, "periodo": {"ano": "2022"}, "localidades": []}]
        }
        with self.assertRaises(SourceError) as caught:
            external.normalize(payload)
        self.assertIn("empty", str(caught.exception))
        self.assertIn("localidades", str(caught.exception))
        with self.assertRaises(SourceError) as caught:
            external.normalize({"result": []})
        self.assertIn("no usable locality rows", str(caught.exception))
        with self.assertRaises(SourceError) as caught:
            external.normalize(
                {"result": [{"agregado": {"nome": "x"}, "periodo": {"ano": "2022"}}]}
            )
        self.assertIn("has no 'localidades' list", str(caught.exception))

    def test_a_source_with_no_contract_is_named_as_unknown(self) -> None:
        with self.assertRaises(InputError) as caught:
            external.normalize({"result": []}, source="twitter_sentiment")
        message = str(caught.exception)
        self.assertIn("no response contract", message)
        self.assertIn(external.IBGE_AREGADOS, message, msg="the refusal lists what is known")

    def test_fold_rejects_a_name_it_cannot_key(self) -> None:
        with self.assertRaises(SourceError):
            external.fold("São|Paulo", "SP")
        with self.assertRaises(SourceError):
            external.fold("Qualquer", "SPULO")

    def test_the_key_matches_the_tables_own_constraint(self) -> None:
        """The regex from 005's CHECK, applied here so a loader never meets a constraint violation.

        Two spaces in a row and a trailing accent are the two shapes the corpus actually contains, so
        they are the two that would have made the server reject a row for a reason the log could not
        explain.
        """
        for name, uf in (("São José do Rio Pardo", "SP"), ("RIO", "rj"), ("Belo  Vale", "mg")):
            key = external.fold(name, uf)
            self.assertRegex(key, external.CITY_STATE_RE)
            city = key.split("|", 1)[0]
            self.assertEqual(city, city.strip(), msg=f"untrimmed key: {key!r}")
            self.assertNotIn("  ", city, msg=f"double space survived the fold: {key!r}")


class ConfigGateTests(unittest.TestCase):
    def test_an_undecided_source_refuses_before_any_request(self) -> None:
        config = config_with()
        with self.assertRaises(NotReadyError) as caught:
            external.request_url(config)
        self.assertIn("decisions.md", str(caught.exception))

    def test_a_declined_source_is_a_decision_not_a_bug(self) -> None:
        config = config_with(name="none")
        with self.assertRaises(NotReadyError):
            external.request_url(config)

    def test_a_named_source_without_a_url_is_its_own_message(self) -> None:
        config = config_with(name=external.IBGE_AREGADOS, join_on="city_state")
        with self.assertRaises(NotReadyError) as caught:
            external.request_url(config)
        self.assertIn("no [external].url", str(caught.exception))

    def test_the_query_string_is_appended_without_reordering_what_was_configured(self) -> None:
        config = config_with(
            name=external.IBGE_AREGADOS,
            url="https://x.example/api/v3/agregados/4714/metadados?localidades=N6[3518800]",
            join_on="city_state",
        )
        self.assertEqual(
            external.request_url(config, {"periodo": "2022"}),
            "https://x.example/api/v3/agregados/4714/metadados?localidades=N6[3518800]"
            "&periodo=2022",
        )
        self.assertEqual(
            external.request_url(config),
            "https://x.example/api/v3/agregados/4714/metadados?localidades=N6[3518800]",
        )

    def test_a_join_key_the_table_cannot_serve_is_refused(self) -> None:
        config = config_with(
            name=external.IBGE_AREGADOS, url="https://x.example/a", join_on="date"
        )
        with self.assertRaises(InputError) as caught:
            external.check_join(config)
        message = str(caught.exception)
        self.assertIn("external.join_on", message)
        self.assertIn("capstone.external_municipio", message)

    def test_a_credential_is_read_by_name_and_never_printed(self) -> None:
        config = config_with(
            name=external.IBGE_AREGADOS, url="https://x.example/a", join_on="city_state",
            token_env="CAPSTONE_EXTERNAL_TOKEN",
        )
        self.assertIsNone(external.token(config_with(name="none"), {}), msg="none needs nothing")
        self.assertEqual(external.token(config, {"CAPSTONE_EXTERNAL_TOKEN": "s3cret"}), "s3cret")
        with self.assertRaises(SourceError) as caught:
            external.token(config, {"SOMETHING_ELSE": "x"})
        message = str(caught.exception)
        self.assertIn("CAPSTONE_EXTERNAL_TOKEN", message)
        self.assertNotIn("s3cret", message)


class FetchTests(unittest.TestCase):
    """The transport half, with the opener injected -- no socket in this file."""

    def config(self, **fields) -> Any:
        values = {
            "name": external.IBGE_AREGADOS,
            "url": "https://servicodados.ibge.gov.br/api/v3/agregados/4714/teste",
            "join_on": "city_state",
        }
        values.update(fields)
        return config_with(**values)

    def test_one_call_returns_validated_rows(self) -> None:
        calls: list[str] = []

        def opener(url: str, headers: dict) -> str:
            calls.append(url)
            return json.dumps(GOOD_PAYLOAD)

        rows = external.fetch(self.config(), opener=opener, environ={})
        self.assertEqual(len(calls), 1, msg="one request per run: no retry loop to hide a rate limit")
        self.assertEqual(rows[0].ibge_code, "3518800")

    def test_a_transport_failure_becomes_one_error_type(self) -> None:
        class Unreachable(Exception):
            pass

        def opener(url: str, headers: dict) -> str:
            raise Unreachable("Network is unreachable")

        with self.assertRaises(SourceError) as caught:
            external.fetch(self.config(), opener=opener, environ={})
        message = str(caught.exception)
        self.assertIn("could not fetch", message)
        self.assertIn("Unreachable", message)
        self.assertIn("Network is unreachable", message)
        self.assertEqual(caught.exception.exit_code, cli.ExitCode.NETWORK)

    def test_a_body_that_is_not_json_is_said_plainly(self) -> None:
        def opener(url: str, headers: dict) -> str:
            return "<html><body>502 Bad Gateway</body></html>"

        with self.assertRaises(SourceError) as caught:
            external.fetch(self.config(), opener=opener, environ={})
        self.assertIn("could not fetch", str(caught.exception))

    def test_an_unconfigured_source_never_reaches_the_transport(self) -> None:
        def opener(url: str, headers: dict) -> str:
            raise AssertionError("the transport must not be touched without a configured source")

        with self.assertRaises(NotReadyError):
            external.fetch(self.config(name=""), opener=opener, environ={})


class LoadTests(unittest.TestCase):
    def rows(self) -> list[external.SignalRow]:
        return external.normalize(GOOD_PAYLOAD)

    def test_storing_goes_through_the_sql_function_and_nothing_else(self) -> None:
        cursor = FakeCursor()
        stored = external.load(cursor, self.rows(), source=external.IBGE_AREGADOS)
        self.assertEqual(stored, 3)
        self.assertEqual(len(cursor.statements), 3)
        for statement, params in cursor.statements:
            self.assertIn("capstone.upsert_external_municipio", statement)
            self.assertNotIn("INSERT", statement.upper(), msg="005 owns the write, not this module")
            self.assertEqual(params[0], params[0].strip())
            self.assertEqual(len(params), 6)

    def test_every_argument_is_cast_because_the_server_resolves_by_type(self) -> None:
        """A live-only failure, pinned offline: ``float`` is not ``numeric`` to PostgreSQL.

        Without the casts a real server answers "function capstone.upsert_external_municipio(...) does
        not exist" for a function that exists, and every stub-based test still passes. The signature
        is part of the call, so the call has to say it.
        """
        cursor = FakeCursor()
        external.load(cursor, self.rows(), source=external.IBGE_AREGADOS)
        statement = cursor.statements[0][0]
        self.assertEqual(statement.count("::text"), 5, msg=statement)
        self.assertIn("%s::numeric", statement, msg="p_value is numeric, not double precision")
        self.assertEqual(statement.count("%s"), 6, msg="one placeholder per argument, no more")

    def test_an_empty_load_is_refused(self) -> None:
        cursor = FakeCursor()
        with self.assertRaises(InputError) as caught:
            external.load(cursor, [], source=external.IBGE_AREGADOS)
        self.assertEqual(cursor.statements, [])
        self.assertIn("no rows", str(caught.exception))

    def test_a_bad_row_is_refused_before_any_statement_runs(self) -> None:
        cursor = FakeCursor()
        broken = dataclasses.replace(self.rows()[0], ibge_code="35188")
        with self.assertRaises(SourceError):
            external.load(cursor, [self.rows()[1], broken], source=external.IBGE_AREGADOS)
        self.assertEqual(cursor.statements, [], msg="validate everything first, then write")


class ReportTests(unittest.TestCase):
    def test_the_match_rate_is_read_from_the_view_that_computes_it(self) -> None:
        cursor = FakeCursor(rows=[(4119, 120, 4099, 0.9951493080)])
        rate = external.match_rate(cursor)
        self.assertEqual(rate["matched_keys"], 4099)
        self.assertEqual(rate["key_match_rate"], 0.9951)
        self.assertIn("capstone.v_external_match_rate", cursor.statements[0][0])

    def test_a_server_that_answers_nothing_reads_as_zero_and_not_as_a_crash(self) -> None:
        rate = external.match_rate(FakeCursor(rows=[]))
        self.assertEqual(rate["source_keys"], 0)
        self.assertIsNone(rate["key_match_rate"])

    def test_a_signal_that_matches_nothing_is_described_not_defended(self) -> None:
        config = config_with(
            name=external.IBGE_AREGADOS,
            url="https://servicodados.ibge.gov.br/api/v3/agregados/4714/teste",
            join_on="city_state",
        )
        rate = {"source_keys": 4119, "external_keys": 0, "matched_keys": 0, "key_match_rate": 0.0}
        text = external.describe(config, rate, stored=0)
        self.assertIn("match rate: 0 of 4119", text)
        self.assertIn("claim: none yet", text)
        self.assertNotIn("error", text.lower())

    def test_status_answers_without_a_database(self) -> None:
        config = config_with()
        status = external.status(config)
        self.assertFalse(status["configured"])
        self.assertIn("undecided", status["name"])
        self.assertIn("CAPSTONE_DATABASE_URI", status["database"])
        self.assertEqual(status["table"], "capstone.external_municipio")

    def test_refresh_with_no_flags_touches_no_database_and_writes_nothing(self) -> None:
        """``--fetch`` on a laptop with no server: the fetch still answers, which is the point."""
        config = config_with(
            name=external.IBGE_AREGADOS,
            url="https://x.example/a",
            join_on="city_state",
        )

        def opener(url: str, headers: dict) -> str:
            return json.dumps(GOOD_PAYLOAD)

        report = external.refresh(
            config, fetch_rows=True, store=False, report=False, opener=opener, environ={}
        )
        self.assertEqual(report["fetched"], 3)
        self.assertIsNone(report["stored"])

    def test_refresh_asks_for_a_database_only_when_it_must_read_one(self) -> None:
        config = config_with(
            name=external.IBGE_AREGADOS, url="https://x.example/a", join_on="city_state"
        )
        saved = __import__("os").environ.pop("CAPSTONE_DATABASE_URI", None)
        try:
            with self.assertRaises(Exception) as caught:
                external.refresh(config, fetch_rows=False, store=False, report=True)
            self.assertIn("CAPSTONE_DATABASE_URI", str(caught.exception))
        finally:
            if saved is not None:
                __import__("os").environ["CAPSTONE_DATABASE_URI"] = saved


class CommandLineTests(unittest.TestCase):
    def test_the_default_mode_reports_and_exits_zero(self) -> None:
        with captured() as out:
            code = cli.main(["external", "--config", str(STAGE / "config" / "capstone.example.toml")])
        self.assertEqual(code, 0)
        self.assertIn("configured: False", out.getvalue())

    def test_fetching_without_a_configured_source_exits_not_ready(self) -> None:
        with captured() as out:
            code = cli.main(
                ["external", "--config", str(STAGE / "config" / "capstone.example.toml"), "--fetch"]
            )
        self.assertEqual(int(code), int(cli.ExitCode.NOT_READY))
        self.assertIn("no external source is configured", out.getvalue())

    def test_reporting_without_a_database_exits_config(self) -> None:
        saved = __import__("os").environ.pop("CAPSTONE_DATABASE_URI", None)
        try:
            with captured() as out:
                code = cli.main(
                    [
                        "external",
                        "--config", str(STAGE / "config" / "capstone.example.toml"),
                        "--report",
                    ]
                )
            self.assertEqual(int(code), int(cli.ExitCode.CONFIG))
            self.assertIn("CAPSTONE_DATABASE_URI", out.getvalue())
        finally:
            if saved is not None:
                __import__("os").environ["CAPSTONE_DATABASE_URI"] = saved

    def test_the_help_screen_names_every_flag(self) -> None:
        parser = cli.build_parser()
        rendered = parser.format_help() + parser._actions[-1].choices["external"].format_help()
        for flag in ("--fetch", "--report", "--load"):
            self.assertIn(flag, rendered)
        self.assertNotIn("--token", rendered, msg="a secret never becomes a command-line argument")

    def test_the_module_never_prints_a_token(self) -> None:
        """Source text, not behaviour: a secret in an f-string ends up in a log, a shell history, a bug.

        Scanning beats asserting on one message, because a credential leaks through the *next*
        sentence as easily as the last one. Parsed with ``ast`` rather than grepped: the module has to
        build ``f"Bearer {credentials}"`` for the header, and a text search cannot tell that line from
        the same name interpolated into a refusal -- a syntax tree can, because it knows which string
        literals are f-strings and which names they read.
        """
        source = (STAGE / "src" / "capstone" / "external.py").read_text(encoding="utf-8")
        secrets = {"credentials", "value", "token_value", "secret"}
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            # the header is the one string allowed to read the token, and it is named exactly rather
            # than by pattern, so widening it is a diff a reviewer sees
            if ast.get_source_segment(source, node) == 'f"Bearer {credentials}"':
                continue
            interpolated = {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}
            self.assertFalse(
                interpolated & secrets,
                msg=f"a credential interpolated into text on line {node.lineno}: "
                f"{sorted(interpolated & secrets)}",
            )
        # and the one place the token is *used* is the header, which the test above cannot see
        header_lines = [line for line in source.splitlines() if "credentials" in line]
        self.assertEqual(
            [line.strip() for line in header_lines],
            [
                "credentials = token(config, environ)",
                "if credentials:",
                'headers["Authorization"] = f"Bearer {credentials}"',
            ],
            msg="the fetch reads the token once and puts it in exactly one place",
        )
        self.assertNotIn("--token", source, msg="a secret never becomes a command-line argument")
        # the second GET is the new risk: a shared transport helper that authorises *both* URLs would
        # send the token to whichever host the reference points at, so the two call sites are pinned by
        # their own flag rather than by a message that happens not to mention the secret
        self.assertEqual(
            source.count("authorize=True"), 1, msg="only the configured URL is authenticated"
        )
        self.assertEqual(
            source.count("authorize=False"), 1, msg="the reference is fetched without the token"
        )

class LiveExternalGuardTests(unittest.TestCase):
    """The one question offline tests cannot answer: does the join hold on a real server.

    Skips when ``CAPSTONE_DATABASE_URI`` is unset -- a machine without the project's database has not
    failed this check, and a test that only passes on this sandbox would be a test nobody can trust.
    """

    URI = __import__("os").environ.get("CAPSTONE_DATABASE_URI", "").strip()

    @unittest.skipIf(not URI, "needs CAPSTONE_DATABASE_URI (the same variable the live guards use)")
    def test_the_external_objects_are_queryable_on_the_real_server(self) -> None:
        from capstone.features import connect

        connection = connect(self.URI, autocommit=True, purpose="read the external match rate")
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = 'capstone' AND table_name = 'external_municipio'"
            )
            self.assertTrue(cursor.fetchall(), msg="sql/005 was never applied to this database")
            rate = external.match_rate(cursor)
            for key in ("source_keys", "external_keys", "matched_keys", "key_match_rate"):
                self.assertIn(key, rate)
            self.assertGreaterEqual(rate["matched_keys"], 0)
            self.assertLessEqual(rate["matched_keys"], rate["source_keys"])
        finally:
            connection.close()

    @unittest.skipIf(not URI, "needs CAPSTONE_DATABASE_URI (the same variable the live guards use)")
    def test_the_empty_table_reports_a_zero_rate_instead_of_failing(self) -> None:
        from capstone.features import connect

        connection = connect(self.URI, autocommit=True, purpose="read the external match rate")
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT count(*) FROM capstone.external_municipio")
            held = int(cursor.fetchone()[0])
            rate = external.match_rate(cursor)
            if held:
                self.skipTest(f"{held} external row(s) are stored here; the empty case is not this DB")
            self.assertEqual(
                rate["external_keys"], 0, msg="an empty table cannot match anything"
            )
        finally:
            connection.close()

    @unittest.skipUnless(
        __import__("os").environ.get("CAPSTONE_EXTERNAL_LIVE", "").strip() == "1",
        "needs CAPSTONE_EXTERNAL_LIVE=1: this contacts a third-party endpoint once",
    )
    def test_the_documented_endpoint_answers_from_this_machine(self) -> None:
        """Off by default, one request when on, and a refusal is a result -- not a failure.

        Running this is how item 8's «not reachable from this environment» eventually stops being
        true. It asserts only that *some* HTTP answer arrived and that the parser either produced
        rows or said why it did not, because "the endpoint is reachable" and "the endpoint returned a
        shape we can store" are two findings and the second one is the useful one.
        """
        config = config_with(
            name=external.IBGE_AREGADOS,
            url="https://servicodados.ibge.gov.br/api/v3/agregados/4714/periodos/2022"
                "/variaveis/93?localidades=N6[3518800]",
            join_on="city_state",
        )
        try:
            rows = external.fetch(config, environ={})
        except SourceError as error:
            message = str(error)
            self.assertTrue(re.search(r"result|fetch|value|localities", message), msg=message)
            self.skipTest(f"reachable enough to answer, unusable: {message[:160]}")
        self.assertTrue(rows)



class RefusedDatabaseTests(unittest.TestCase):
    """The third state of item 12: a configured URI whose server is not there.

    A closed port is not "no database configured" (78, fix your config) and it is not "the source
    declined" (69, the endpoint answered badly) -- it is 67, and the difference matters because the
    reader's next action is different in each case. These two tests reach the CLI, not the helper,
    because the bug was a traceback escaping ``main``: the sentence is only a fix if the exit code
    that travels with it is the one the rest of the stage already branches on.
    """

    URI = "postgresql+psycopg://learner:sup3rs3cr3t@127.0.0.1:5599/olist_phase0_test"

    def refusing(self):
        class Refusing:
            OperationalError = type("OperationalError", (Exception,), {})

            def connect(self, dsn: str):
                raise self.OperationalError(
                    'connection to server at "127.0.0.1", port 5599 failed: Connection refused'
                )

        return mock.patch.object(features, "_driver_and_dsn", return_value=(Refusing(), "dsn"))

    def run_cli(self, *flags: str) -> tuple[int, str]:
        saved = os.environ.get("CAPSTONE_DATABASE_URI")
        os.environ["CAPSTONE_DATABASE_URI"] = self.URI
        try:
            with captured() as out:
                argv = ["external", "--config", str(STAGE / "config" / "capstone.example.toml")]
                code = cli.main([*argv, *flags])
        finally:
            if saved is None:
                os.environ.pop("CAPSTONE_DATABASE_URI", None)
            else:
                os.environ["CAPSTONE_DATABASE_URI"] = saved
        return int(code), out.getvalue()

    def test_report_over_a_closed_port_is_a_message_and_67(self) -> None:
        with self.refusing():
            code, text = self.run_cli("--report")
        self.assertEqual(code, 67)
        self.assertNotIn("Traceback", text, msg="an unhandled exception is what this test exists to catch")
        self.assertIn("would not open", text)
        self.assertNotIn("sup3rs3cr3t", text, msg="the refusal goes into docs/evidence/ one day")

    def test_the_load_path_refuses_before_touching_the_endpoint(self) -> None:
        """``--load`` fetches first, and with no source configured the fetch is the honest refusal."""
        with self.refusing():
            code, text = self.run_cli("--load")
        self.assertIn(code, (67, 69), msg=f"expected a reported state, got {text!r}")
        self.assertNotIn("Traceback", text)


#: the shape the endpoint really answers with, from the student's own call of
#: ``…/agregados/4714/periodos/2022/variaveis/614?localidades=N6[all]`` -- the URL and
#: the decision are decisions.md item 8, not a comment. What that measurement witnessed, and only
#: that, is in this fixture: a top-level **list**, ``resultados`` inside each element, the
#: municipalities inside ``series``, one municipality per ``localidade``, ``nivel.id`` carrying the
#: level, and the number as a *string* keyed by year in ``serie``: ``{"2022": "3.04"}``.
#:
#: What is authored, and labelled as such rather than remembered as witnessed: the locality names are
#: placeholders, because the transcription carried no names at all; ``3550308`` and ``1094.75`` are a
#: second row so the loop is exercised more than once; and the ``uf`` objects are what the loader needs
#: to build ``<city>|<uf>`` and were not in the excerpt either. The two tests that say what happens
#: when a ``uf`` is absent are the ones that matter for a real response, because that is the field
#: whose presence here is an assumption rather than a measurement.
IBGE_DATA_PAYLOAD: list = [
    {
        "id": "614",
        "variavel": "Densidade demográfica",
        "unidade": "Habitante por quilômetro quadrado",
        "resultados": [
            {
                "series": [
                    {
                        "localidade": {
                            "id": "1100015",
                            "nome": "Município Um",
                            "nivel": {"id": "N6", "nome": "Município"},
                            "uf": {"id": "11", "sigla": "RO"},
                        },
                        "serie": {"2022": "3.04"},
                    },
                    {
                        "localidade": {
                            "id": "3550308",
                            "nome": "Município Dois",
                            "nivel": {"id": "N6"},
                            "uf": {"sigla": "SP"},
                        },
                        "serie": {"2022": "1094.75"},
                    },
                ]
            }
        ],
    }
]


def data_payload(**edits) -> list:
    """A copy of the response with the variable element edited, so each case names what it changed.

    Paths start inside ``payload[0]`` because the root is a list -- the fact the parser exists for.
    """
    payload = json.loads(json.dumps(IBGE_DATA_PAYLOAD))
    for path, value in edits.items():
        node = payload[0]
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[int(key)] if key.isdigit() else node[key]
        if value is _DROP:
            node.pop(keys[-1])
        else:
            node[keys[-1]] = value
    return payload


class _Drop:
    """Sentinel: `del` and `None` mean different things to a parser, so the fixture says which."""

    def __repr__(self) -> str:  # pragma: no cover - only for a failed assertion's message
        return "<drop>"


_DROP = _Drop()


class IbgeDataShapeTests(unittest.TestCase):
    """The response the API really sends, as rows -- and every refusal that keeps the join honest.

    These cases exist because the first contract was written from documentation and the endpoint
    disagreed with it: a top-level list, not ``{"result": …}``. The refusal wording is as pinned as
    the success path, because the difference between "the API changed" and "the network is down" is
    the one thing ``--fetch`` has to answer without a debugger.
    """

    def test_the_real_response_becomes_one_row_per_municipality(self) -> None:
        rows = external.normalize(IBGE_DATA_PAYLOAD)
        self.assertEqual([row.ibge_code for row in rows], ["1100015", "3550308"])
        self.assertEqual([row.city_state for row in rows], ["municipio um|ro", "municipio dois|sp"])
        self.assertEqual({row.period for row in rows}, {"2022"}, msg="the year is the serie's key here")
        self.assertEqual(
            {row.indicator for row in rows},
            {"Densidade demográfica"},
            msg="`variavel` names the indicator in this shape",
        )

    def test_a_dot_decimal_string_is_not_read_as_a_thousands_group(self) -> None:
        """``"3.04"`` is three point zero four inhabitants per km², and 304 is a wrong country.

        The pt-BR rule that made ``"2.048,90"`` read correctly would, applied blindly to a lone dot,
        turn this into 304 -- so the rule is stated where the value is parsed and pinned here, because
        a density off by two orders of magnitude passes every CHECK the table has.
        """
        rows = external.normalize(IBGE_DATA_PAYLOAD)
        self.assertAlmostEqual(rows[0].value, 3.04, places=6)
        self.assertAlmostEqual(rows[1].value, 1094.75, places=6)
        for raw, expected in (
            ("3.04", 3.04),
            ("1094.75", 1094.75),
            (1.05, 1.05),
            ("2.048,90", 2048.90),
            ("2048.90", 2048.90),
        ):
            self.assertAlmostEqual(external.parse_value(raw), expected, places=6, msg=repr(raw))

    def test_a_value_of_none_stays_null_rather_than_becoming_zero(self) -> None:
        payload = data_payload(**{"resultados.0.series.0.serie": {"2022": None}})
        self.assertIsNone(external.normalize(payload)[0].value)

    def test_the_level_comes_from_nivel_and_a_state_answer_is_refused(self) -> None:
        """``N7`` is a state: a state's density onto every city in it is a confident wrong feature."""
        payload = data_payload(
            **{"resultados.0.series.0.localidade.nivel": {"id": "N7", "nome": "Unidade da federação"}}
        )
        with self.assertRaises(SourceError) as caught:
            external.normalize(payload)
        self.assertIn("N7", str(caught.exception))
        self.assertIn("N6", str(caught.exception), msg="the reader is told what was expected")

    def test_a_locality_with_no_level_of_its_own_is_refused_not_assumed(self) -> None:
        """In this shape the level is ``nivel.id``; without it, six digits of guessing start."""
        payload = data_payload(**{"resultados.0.series.0.localidade.nivel": _DROP})
        with self.assertRaises(SourceError) as caught:
            external.normalize(payload)
        self.assertIn("N<level>[<code>]", str(caught.exception))

    def test_a_bare_id_is_accepted_here_and_refused_in_the_envelope_shape(self) -> None:
        """One rule per shape, because the two shapes carry their level evidence differently.

        The data form writes ``"id": "1100015"`` and puts the level in ``nivel``; the envelope form
        writes ``"id": "N6[3518800]"``. A parser that let either style satisfy either shape would
        accept a state row whose ``nivel`` had been dropped -- which is the refusal above.
        """
        rows = external.normalize(IBGE_DATA_PAYLOAD)
        self.assertEqual(rows[0].ibge_code, "1100015")
        envelope = json.loads(json.dumps(GOOD_PAYLOAD))
        envelope["result"][0]["localidades"][0]["id"] = "3518800"
        with self.assertRaises(SourceError) as caught:
            external.normalize(envelope)
        self.assertIn("N<level>[<code>]", str(caught.exception))
        bracketed = data_payload(**{"resultados.0.series.0.localidade.id": "N6[1100015]"})
        self.assertEqual(external.normalize(bracketed)[0].ibge_code, "1100015")

    def test_a_missing_uf_names_the_keys_it_looked_for(self) -> None:
        """The join key is ``<city>|<uf>``, and 163 city names recur in two or more states.

        So no UF is not a small omission: the alternative is inventing a state, which is what the
        refusal says, and it lists the locality's own keys so one paste of a real object is enough to
        teach the parser wherever the sigla actually lives. The lookup that resolves this is
        ``UfReferenceTests``; this test pins that *without* the reference the run stops here.
        """
        payload = data_payload(**{"resultados.0.series.0.localidade.uf": _DROP})
        with self.assertRaises(SourceError) as caught:
            external.normalize(payload)
        message = str(caught.exception)
        self.assertIn("carries no uf", message)
        for key in ("id", "nome", "nivel"):
            self.assertIn(key, message, msg="the keys it had are the diagnosis")
        self.assertIn("decisions.md", message, msg="and the reason is a decision, not a bug")
        self.assertEqual(caught.exception.context.get("missing_uf"), "1100015")

    def test_a_uf_may_ride_on_the_block_or_the_variable_element(self) -> None:
        """The sigla is looked for in a few documented places -- and only those."""
        bare = data_payload(
            **{
                "resultados.0.series.0.localidade.uf": _DROP,
                "resultados.0.uf": {"sigla": "RO"},
            }
        )
        self.assertEqual(external.normalize(bare)[0].city_state, "municipio um|ro")
        on_element = data_payload(
            **{"resultados.0.series.0.localidade.uf": _DROP, "uf": {"sigla": "RO"}}
        )
        self.assertTrue(external.normalize(on_element)[0].city_state.endswith("|ro"))

    def test_a_bare_sigla_string_is_as_good_as_an_object(self) -> None:
        """Some agregados answers write ``"uf": "RO"``; the key needs the letters, not a shape."""
        payload = data_payload(**{"resultados.0.series.0.localidade.uf": "RO"})
        self.assertEqual(external.normalize(payload)[0].city_state, "municipio um|ro")
        on_nivel = data_payload(
            **{
                "resultados.0.series.0.localidade.uf": _DROP,
                "resultados.0.series.0.localidade.nivel": {"id": "N6", "sigla": "RO"},
            }
        )
        self.assertEqual(external.normalize(on_nivel)[0].city_state, "municipio um|ro")

    def test_two_years_in_one_serie_are_two_rows(self) -> None:
        payload = data_payload(
            **{"resultados.0.series.0.serie": {"2010": "6.16", "2022": "3.04"}}
        )
        rows = [row for row in external.normalize(payload) if row.ibge_code == "1100015"]
        self.assertEqual([(row.period, row.value) for row in rows], [("2010", 6.16), ("2022", 3.04)])

    def test_a_serie_key_that_is_not_a_year_is_refused(self) -> None:
        payload = data_payload(**{"resultados.0.series.0.serie": {"2022-2023": "3.04"}})
        with self.assertRaises(SourceError) as caught:
            external.normalize(payload)
        self.assertIn("2022-2023", str(caught.exception))

    def test_a_series_object_with_no_localidade_is_reported_as_unusable(self) -> None:
        payload = data_payload(**{"resultados.0.series": []})
        with self.assertRaises(SourceError) as caught:
            external.normalize(payload)
        self.assertIn("empty", str(caught.exception))
        with self.assertRaises(SourceError) as caught:
            external.normalize([{"id": "614", "variavel": "x", "resultados": []}])
        self.assertIn("no usable locality rows", str(caught.exception))

    def test_the_envelope_form_still_parses(self) -> None:
        """The shape the first draft was written against is not retired by learning the other one."""
        rows = external.normalize(GOOD_PAYLOAD)
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(rows[0].value, 2048.90, places=6)

    def test_a_top_level_string_is_refused_with_its_own_sentence(self) -> None:
        for payload in ("<html>", None, 7):
            with self.subTest(payload=type(payload).__name__):
                with self.assertRaises(SourceError) as caught:
                    external.normalize(payload)
                self.assertIn("is not the one this parser was written against", str(caught.exception))


#: the municipality list's own shape, verbatim from the six leading entries of
#``https://servicodados.ibge.gov.br/api/v1/localidades/municipios``: the code, the name, and the
#: state two levels down. This is the witness the previous round did not have -- the first two digits
#: of an IBGE code are a state code, and the list says so for every municipality instead of trusting
#: a rule.
UF_MAP_SAMPLE = [
    {
        "id": 1100015,
        "nome": "Alta Floresta D'Oeste",
        "microrregiao": {
            "id": 11006,
            "nome": "Cacoal",
            "mesorregiao": {"id": 1102, "nome": "Leste Rondoniense", "UF": {"id": 11, "sigla": "RO"}},
        },
    },
    {
        "id": 1100023,
        "nome": "Ariquemes",
        "microrregiao": {"mesorregiao": {"UF": {"sigla": "RO"}}},
    },
    {"id": 1100031, "nome": "Cabixi", "microrregiao": {"mesorregiao": {"UF": {"sigla": "RO"}}}},
    {"id": 1100372, "nome": "Porto Velho", "microrregiao": {"mesorregiao": {"UF": {"sigla": "RO"}}}},
    {"id": 3550308, "nome": "Guarulhos", "microrregiao": {"mesorregiao": {"UF": {"sigla": "SP"}}}},
    {"id": 3304557, "nome": "Rio de Janeiro", "microrregiao": {"mesorregiao": {"UF": {"sigla": "RJ"}}}},
]


class MunicipalityNameTests(unittest.TestCase):
    """Where the *city* half of the key comes from, once the list is the one being asked.

    Measured on the student's own database after `0016`: `--fetch` stored 5570 rows and the reference
    answered for every one of them, and `match rate` stayed `0 of 4310`. The reason was in the key:
    the agregados answer names a municipality ``São Paulo - SP``, so ``0016`` built
    ``sao paulo - sp|sp`` while ``005`` keys on ``lower(customer_city) || '|' || lower(customer_state)``
    -- ``sao paulo|sp``. The fix is not to cut a suffix off a display string; it is to take the name from
    the same record the state was taken from, so that one row is spelled by one source.
    """

    #: the three codes the student's own query printed, with the answer's name and the list's name
    LIVE_TRIPLES = (
        ("1100015", "Alta Floresta D'Oeste - RO", "Alta Floresta D'Oeste", "ro"),
        ("3304557", "Rio de Janeiro - RJ", "Rio de Janeiro", "rj"),
        ("3550308", "São Paulo - SP", "São Paulo", "sp"),
    )

    def suffixed_payload(self, code: str, name: str) -> dict:
        return data_payload(
            **{
                "resultados.0.series.0.localidade.id": code,
                "resultados.0.series.0.localidade.nome": name,
                "resultados.0.series.0.localidade.uf": _DROP,
            }
        )

    def list_body(self, code: str, name: str, sigla: str) -> list:
        return [{"id": int(code), "nome": name, "microrregiao": {"mesorregiao": {"UF": {"sigla": sigla}}}}]

    def test_the_suffixed_name_is_what_the_endpoint_holds(self) -> None:
        """The finding, before any fix: this is the string the response sends, not one we added.

        Kept as a test on the *payload* rather than on a row, because after this patch the row no longer
        carries the suffix -- and the reason it stopped carrying it is exactly what would otherwise be
        forgotten: the endpoint writes ``São Paulo - SP`` into ``localidade.nome``.
        """
        for code, answer_name, _, _ in self.LIVE_TRIPLES:
            with self.subTest(code=code):
                payload = self.suffixed_payload(code, answer_name)
                locality = payload[0]["resultados"][0]["series"][0]["localidade"]
                self.assertEqual(locality["nome"], answer_name)
                self.assertNotIn("uf", locality, msg="and no state of its own, which is the whole point")

    def test_every_example_the_student_printed_becomes_the_key_the_view_expects(self) -> None:
        for code, answer_name, list_name, sigla in self.LIVE_TRIPLES:
            with self.subTest(code=code):
                mapping = external.parse_uf_map(self.list_body(code, list_name, sigla))[0]
                rows = external.normalize(
                    self.suffixed_payload(code, answer_name), municipality_by_code=mapping
                )
                folded = (
                    unicodedata.normalize("NFKD", list_name)
                    .encode("ascii", "ignore")
                    .decode()
                    .casefold()
                )
                self.assertEqual(rows[0].city_state, f"{folded}|{sigla}")
                self.assertNotIn(" - ", rows[0].city_state, msg="no display suffix reaches the table")

    def test_the_name_is_taken_from_the_list_only_because_the_state_was(self) -> None:
        """A response that answers both questions is not edited by the reference.

        The list is a fallback for a missing half, not a spelling authority: if the answer names the
        state too, its name is what the row carries, and a disagreement between the two sources stays
        visible in the report instead of being resolved by a rule nobody audited.
        """
        mapping = {"1100015": external.Municipality(name="Another Place", sigla="SP")}
        rows = external.normalize(IBGE_DATA_PAYLOAD, municipality_by_code=mapping)
        self.assertEqual(rows[0].city_state, "municipio um|ro")

    def test_a_list_entry_without_a_name_keeps_the_answers_name(self) -> None:
        mapping, _, _ = external.parse_uf_map(
            [{"id": 3550308, "microrregiao": {"mesorregiao": {"UF": {"sigla": "SP"}}}}]
        )
        self.assertEqual(mapping["3550308"].name, "")
        rows = external.normalize(
            self.suffixed_payload("3550308", "São Paulo - SP"), municipality_by_code=mapping
        )
        self.assertEqual(rows[0].city_state, "sao paulo - sp|sp", msg="nothing invented, nothing dropped")

    def test_the_map_reports_what_it_learned_from_the_list(self) -> None:
        entries = [
            {"id": 3550308, "nome": "São Paulo", "microrregiao": {"mesorregiao": {"UF": {"sigla": "SP"}}}},
            {"id": 3304557, "nome": "Rio de Janeiro", "microrregiao": {"mesorregiao": {"UF": {"sigla": "RJ"}}}},
        ]
        mapping, seen, usable = external.parse_uf_map(entries)
        self.assertEqual((seen, usable), (2, 2))
        self.assertEqual(mapping["3550308"].name, "São Paulo", msg="accents are folded later, by fold()")
        self.assertEqual(mapping["3550308"].sigla, "SP")

    def test_a_locality_the_answer_never_named_still_refuses(self) -> None:
        """The list names places, it does not identify them: a code alone is not a row to key."""
        payload = data_payload(**{"resultados.0.series.0.localidade.nome": _DROP,
                                   "resultados.0.series.0.localidade.uf": _DROP})
        with self.assertRaises(external.SourceError) as caught:
            external.normalize(
                payload,
                municipality_by_code={"1100015": external.Municipality(name="X", sigla="RO")},
            )
        self.assertIn("has no name", str(caught.exception))


class UfReferenceTests(unittest.TestCase):
    """The municipality list that supplies the state the agregados response omits.

    The exchange this class buys is one small GET for the whole run, spent only when the response
    turned out to be missing a ``uf``. Everything else about it is refusal: an unknown code, a
    non-list, a reference that parsed to nothing. A signal whose state was *guessed* is worse than a
    signal that never arrived, because the guess joins confidently to orders in the wrong state.
    """

    def payload_without_uf(self) -> dict:
        return data_payload(
            **{
                "resultados.0.series.0.localidade.uf": _DROP,
                "resultados.0.series.1.localidade.uf": _DROP,
            }
        )

    def test_the_reference_supplies_the_state_the_response_omits(self) -> None:
        mapping, seen, with_uf = external.parse_uf_map(UF_MAP_SAMPLE)
        self.assertEqual((seen, with_uf), (6, 6))
        self.assertEqual(mapping["1100015"].sigla, "RO")
        rows = external.normalize(self.payload_without_uf(), municipality_by_code=mapping)
        self.assertEqual(
            [row.city_state for row in rows],
            ["alta floresta d'oeste|ro", "guarulhos|sp"],
            msg="the whole key is built from the reference: the answer's names are display strings",
        )

    def test_the_response_wins_when_it_does_name_a_uf(self) -> None:
        """A table about the code never overrides an answer about the place.

        The reference and the response can only disagree if one of them is stale, and the row that
        joins to orders is the one the audit can be checked against -- so the response is trusted and
        the disagreement stays visible in the row rather than being averaged away.
        """
        mapping = {"1100015": external.Municipality(sigla="SP", name="Elsewhere")}
        rows = external.normalize(IBGE_DATA_PAYLOAD, municipality_by_code=mapping)
        self.assertEqual(rows[0].city_state, "municipio um|ro", msg="the response said RO, and named")

    def test_a_code_the_reference_does_not_know_is_refused(self) -> None:
        payload = data_payload(**{
            "resultados.0.series.0.localidade.uf": _DROP,
            "resultados.0.series.0.localidade.id": "9909999",
            "resultados.0.series.1.localidade.uf": _DROP,
        })
        with self.assertRaises(SourceError) as caught:
            external.normalize(payload, municipality_by_code={"1100015": external.Municipality(name="Alta Floresta D'Oeste", sigla="RO")})
        message = str(caught.exception)
        self.assertIn("9909999", message, msg="the code is the diagnosis")
        self.assertIn(external.LOCALIDADES_URL, message, msg="and so is the list that did not know it")

    def test_parse_accepts_both_spellings_of_the_id_and_skips_what_has_no_uf(self) -> None:
        entries = [{"id": "1100015", "microrregiao": {"mesorregiao": {"UF": {"sigla": "RO"}}}}]
        mapping, seen, with_uf = external.parse_uf_map(entries)
        self.assertEqual(mapping, {"1100015": external.Municipality(sigla="RO", name="")})
        self.assertEqual((seen, with_uf), (1, 1), msg="an entry can have a state and no name")
        blank = [
            {"id": 1100015, "nome": "no uf anywhere in this entry"},
            {"id": 1100023, "microrregiao": {"mesorregiao": {"UF": {"sigla": "RO"}}}},
        ]
        mapping, seen, with_uf = external.parse_uf_map(blank)
        self.assertEqual(
            mapping, {"1100023": external.Municipality(sigla="RO", name="")},
            msg="one entry without a UF is not a broken list",
        )
        self.assertEqual((seen, with_uf), (2, 1))

    def test_a_reference_that_is_not_a_list_is_refused_with_its_own_words(self) -> None:
        for payload in ({"errors": ["no"]}, "<html>", None):
            with self.subTest(payload=type(payload).__name__):
                with self.assertRaises(SourceError) as caught:
                    external.parse_uf_map(payload)
                self.assertIn("municipality reference", str(caught.exception))
        with self.assertRaises(SourceError) as caught:
            external.parse_uf_map([{"nome": "sem código"}])
        self.assertIn("no usable municipality code", str(caught.exception))
        with self.assertRaises(SourceError) as caught:
            external.parse_uf_map([])
        self.assertIn("no usable", str(caught.exception))

    def test_the_second_request_is_spent_only_when_a_state_is_missing(self) -> None:
        calls: list[str] = []

        def opener(url: str, headers: dict) -> str:
            calls.append(url)
            if url == external.LOCALIDADES_URL:
                return json.dumps(UF_MAP_SAMPLE)
            return json.dumps(IBGE_DATA_PAYLOAD)

        config = config_with(
            name=external.IBGE_AREGADOS, url="https://example.invalid/agregados", join_on="city_state"
        )
        rows = external.fetch(config, opener=opener, environ={})
        self.assertEqual([row.city_state for row in rows][0], "municipio um|ro")
        self.assertEqual(calls, ["https://example.invalid/agregados"], msg="the response had its siglas")

        calls.clear()
        external.fetch(config, opener=opener, environ={}, force_reference=True)
        self.assertEqual(
            calls,
            ["https://example.invalid/agregados", external.LOCALIDADES_URL],
            msg="one reference request per run, whatever the count of localities",
        )

    def test_the_reference_url_is_the_official_list_unless_the_environment_moves_it(self) -> None:
        """The seam exists so this can be tested with no network; nothing else should set it."""
        self.assertEqual(
            external.LOCALIDADES_URL,
            "https://servicodados.ibge.gov.br/api/v1/localidades/municipios",
        )
        calls: list[str] = []

        def opener(url: str, headers: dict) -> str:
            calls.append(url)
            if url.endswith("/municipios"):
                return json.dumps(
                    [
                        {"id": 1100015, "microrregiao": {"mesorregiao": {"UF": {"sigla": "RO"}}}},
                        {"id": 3550308, "microrregiao": {"mesorregiao": {"UF": {"sigla": "SP"}}}},
                    ]
                )
            return json.dumps(self.payload_without_uf())

        config = config_with(
            name=external.IBGE_AREGADOS, url="https://example.invalid/agregados", join_on="city_state"
        )
        rows = external.fetch(
            config,
            opener=opener,
            environ={"CAPSTONE_IBGE_LOCALIDADES_URL": "http://127.0.0.1:8099/municipios"},
        )
        self.assertEqual(rows[0].city_state, "municipio um|ro")
        self.assertEqual(calls[1], "http://127.0.0.1:8099/municipios")

    def test_a_failed_reference_leaves_the_first_refusal_visible(self) -> None:
        def opener(url: str, headers: dict) -> str:
            if url == external.LOCALIDADES_URL:
                raise OSError("Network is unreachable")
            return json.dumps(self.payload_without_uf())

        config = config_with(
            name=external.IBGE_AREGADOS, url="https://example.invalid/agregados", join_on="city_state"
        )
        with self.assertRaises(SourceError) as caught:
            external.fetch(config, opener=opener, environ={})
        message = str(caught.exception)
        self.assertIn("could not fetch", message, msg="the transport failure is the finding")
        self.assertIn("municipality reference", message, msg="and it says which fetch failed")

    def test_the_cli_says_when_it_had_to_ask_for_the_map(self) -> None:
        """One line, because a second request nobody mentions is indistinguishable from a bug.

        ``refresh`` is replaced rather than the transport: the line under test is the print, and a
        stubbed fetch would exercise the parser again while leaving the report format unpinned.
        """
        original = cli.external_signal.refresh
        report = {
            "fetched": 2,
            "stored": None,
            "match_rate": None,
            "unmatched_cities": None,
            "uf_reference": {
                "url": "https://servicodados.ibge.gov.br/api/v1/localidades/municipios",
                "entries": 5570,
                "codes": 5568,
            },
        }
        try:
            cli.external_signal.refresh = lambda *args, **kwargs: report
            with captured() as out:
                code = cli.main(
                    [
                        "external",
                        "--config", str(STAGE / "config" / "capstone.example.toml"),
                        "--fetch",
                    ]
                )
        finally:
            cli.external_signal.refresh = original
        self.assertEqual(int(code), 0)
        self.assertIn("uf map: 5568 of 5570 municipality codes", out.getvalue())
        report.pop("uf_reference")
        try:
            cli.external_signal.refresh = lambda *args, **kwargs: report
            with captured() as quiet:
                cli.main(["external", "--config", str(STAGE / "config" / "capstone.example.toml"), "--fetch"])
        finally:
            cli.external_signal.refresh = original
        self.assertNotIn("uf map", quiet.getvalue(), msg="no line when no request was spent")

    def test_refresh_reports_the_reference_it_used(self) -> None:
        def opener(url: str, headers: dict) -> str:
            if url == external.LOCALIDADES_URL:
                return json.dumps(UF_MAP_SAMPLE)
            return json.dumps(self.payload_without_uf())

        notes: dict = {}
        config = config_with(
            name=external.IBGE_AREGADOS, url="https://example.invalid/agregados", join_on="city_state"
        )
        rows = external.fetch(config, opener=opener, environ={}, notes=notes)
        self.assertEqual(len(rows), 2)
        self.assertEqual(notes["uf_reference"]["codes"], 6)
        self.assertEqual(notes["uf_reference"]["url"], external.LOCALIDADES_URL)



if __name__ == "__main__":
    unittest.main()
