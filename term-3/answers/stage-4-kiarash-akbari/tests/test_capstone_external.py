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
        for field in ("nome", "uf"):
            with self.subTest(field=field):
                payload = json.loads(json.dumps(GOOD_PAYLOAD))
                payload["result"][0]["localidades"][0][field] = None
                if field == "uf":
                    payload["result"][0]["localidades"][0]["uf"] = {}
                with self.assertRaises(SourceError) as caught:
                    external.normalize(payload)
                self.assertIn("name or uf", str(caught.exception))

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
                "credentials = token(config, environ if environ is not None else _environ())",
                "if credentials:",
                'headers["Authorization"] = f"Bearer {credentials}"',
            ],
            msg="the fetch reads the token once and puts it in exactly one place",
        )
        self.assertNotIn("--token", source, msg="a secret never becomes a command-line argument")

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


if __name__ == "__main__":
    unittest.main()
