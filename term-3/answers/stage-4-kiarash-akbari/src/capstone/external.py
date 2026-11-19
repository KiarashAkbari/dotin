"""The external signal: one request, a validated response, and a table that can be empty.

decisions.md item 8 registers ``ibge_agregados_municipio`` and records that the endpoint has not been
reachable from the environment this code was written in. That combination -- a decided source, an
unverified fetch -- is what this module is shaped for:

* **the fetch is one request.** No retry loop, no backoff, no pagination walk. IBGE's agregados API
  publishes no rate limit, and the advice is one to two requests per second; a student project that
  hammers a public statistics endpoint with an unbounded retry loop is not a resilient client, it is
  a small attack that happens to be polite in its docstrings. A failed fetch prints the reason and
  leaves the table alone.
* **the response is validated here, not by the database.** ``capstone.external_municipio`` has CHECK
  constraints, and they stay as the backstop; the point of checking before the insert is that a schema
  change arrives as ``external returned no "localidades" key in result 0`` and not as 400 constraint
  violations after a partial write.
* **no SQL of its own.** Rows are stored by calling ``capstone.upsert_external_municipio``, the same
  rule ``sql/005_external_signal.sql`` states for every loader: a module that writes its own ``INSERT``
  bypasses the idempotency the re-apply guard tests.
* **an empty table is a result, not an error.** ``load`` and ``report`` both answer with the match rate
  they found, including ``0 of 4119 keys``. The negative result the brief asks for -- an external
  signal that adds nothing -- has to be printable, so nothing in this module raises on it.

``normalize`` and ``validate`` are pure functions over a parsed payload. That is not decoration: the
contract of a third-party API is the one part of item 12 that can be tested with no network at all,
and the half that cannot be tested here (does the endpoint answer?) is reported as unverified rather
than asserted.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .config import CapstoneConfig
from .errors import InputError, NotReadyError, SourceError
from .features import FRAME_VIEW, connect, database_uri

#: the only source registered in decisions.md so far; a second one is a new entry in ``CONTRACTS``
IBGE_AREGADOS = "ibge_agregados_municipio"

#: the shape of ``capstone.external_municipio``'s key, restated here so a bad row is refused with a
#: sentence instead of a constraint violation
IBGE_CODE_RE = re.compile(r"^[0-9]{7}$")
CITY_STATE_RE = re.compile(r"^[^|]+\|[a-z]{2}$")
PERIOD_RE = re.compile(r"^[0-9]{4}$")
#: the identifier the API echoes back per locality, e.g. ``"N6[3550308]"`` -- N6 is the municipality
#: level, and the digits inside the brackets are the IBGE code the table keys on
LOCALITY_ID_RE = re.compile(r"^N(?P<level>\d+)\[(?P<code>[0-9]+)\]$")

#: per-source expectations. The URL comes from the config, because a base URL is the kind of thing
#: that moves when a mirror appears; what cannot move is the response's shape, and that is what lives
#: here. ``value_key`` is where the number sits inside one locality object.
CONTRACTS: dict[str, dict[str, Any]] = {
    IBGE_AREGADOS: {
        "table": "capstone.external_municipio",
        "mutator": "capstone.upsert_external_municipio",
        "results_key": "result",
        "value_key": "valor",
        "period_key": "periodo",
        "locality_key": "localidades",
        "level": 6,
    },
}


@dataclass(frozen=True)
class SignalRow:
    """One indicator, for one municipality, for one year. The unit the table and the API agree on."""

    ibge_code: str
    city_state: str
    indicator: str
    period: str
    value: float | None

    def as_sql_params(self, source: str) -> tuple[str, str, str, str, float | None, str]:
        """Positional arguments for ``capstone.upsert_external_municipio``, in its own order."""
        return (
            self.ibge_code,
            self.city_state,
            self.indicator,
            self.period,
            self.value,
            source,
        )


def fold(name: str, uf: str) -> str:
    """The join key, built the way the table's constraint defines it.

    Accents are stripped and the case is lowered because that is what the *orders* side needs: the
    customer cities in the corpus are written 2,184 different ways by spelling and case alone, and
    ``city_state`` is the only key the audit permits (163 bare city names recur in more than one
    state, so the city alone would join a São Paulo order to Rio's numbers).
    """
    decomposed = unicodedata.normalize("NFD", str(name))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = re.sub(r"\s+", " ", stripped).strip().casefold()
    key = f"{cleaned}|{str(uf).strip().casefold()}"
    if not CITY_STATE_RE.match(key):
        raise SourceError(f"cannot build a join key from {name!r}/{uf!r}: {key!r} is not <city>|<uf>")
    return key


def validate(row: SignalRow) -> None:
    """One row against the table's constraints, before a server has to say no."""
    if not IBGE_CODE_RE.match(row.ibge_code):
        raise SourceError(f"ibge_code {row.ibge_code!r} is not seven digits")
    if not CITY_STATE_RE.match(row.city_state):
        raise SourceError(f"city_state {row.city_state!r} is not <lowercase city>|<two-letter uf>")
    if not PERIOD_RE.match(row.period):
        raise SourceError(f"period {row.period!r} is not a four-digit year")
    if not row.indicator.strip():
        raise SourceError("an indicator with no name cannot be pivoted into a column")


def parse_value(raw: Any, *, source: str = IBGE_AREGADOS, where: str = "") -> float:
    """One number, whether the API wrote ``2000.06``, ``"2000.06"`` or ``"2.000,06"``.

    The decimal separator is the whole function. A blind ``replace(".", "")`` -- which is what the
    first draft did, trusting the docs to be about pt-BR output -- turns ``"2000.06"`` into 200006,
    and a value that is wrong by two orders of magnitude passes every constraint the table has. The
    rule is therefore permissive where the two spellings cannot be confused and strict where they can:
    a string carrying both separators is pt-BR, one carrying only a dot is already ISO, and anything
    else is refused rather than guessed at.
    """
    if isinstance(raw, bool):
        raise SourceError(f"{source} value {raw!r} for {where} is a boolean, not a measure")
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip().replace("\u00a0", "")
    if "," in text and "." in text:
        # both separators present: the dot is the thousands mark and the comma is the decimal one
        if text.rfind(",") < text.find("."):
            raise SourceError(
                f"{source} value {raw!r} for {where} has a dot after its comma; refusing to guess"
                " which one is the decimal separator"
            )
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        if text.count(",") > 1:
            raise SourceError(f"{source} value {raw!r} for {where} has two commas and no rule")
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError as error:
        raise SourceError(f"{source} value {raw!r} for {where} is not a number: {error}") from error


def normalize(payload: Any, *, source: str = IBGE_AREGADOS) -> list[SignalRow]:
    """The API's JSON, as rows. Pure, and the reason the contract is testable without a network.

    A shape surprise raises ``SourceError`` naming the key that is missing and where it was looked
    for. That sentence is the whole difference between "the API is down" and "the API answered with
    something we do not understand", and the second one is the failure that gets silently swallowed
    into an empty table.
    """
    contract = CONTRACTS.get(source)
    if contract is None:
        known = ", ".join(sorted(CONTRACTS))
        raise InputError(
            f"{source!r} has no response contract; the ones this module knows are: {known}. "
            "Naming a source in the config is a decision about *which* API; teaching the code its "
            "shape is a second decision, and inventing a parser is not how it gets made."
        )
    results = payload.get(contract["results_key"]) if isinstance(payload, dict) else None
    if results is None:
        got = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise SourceError(
            f"{source} returned no {contract['results_key']!r} key (top level: {got}); "
            "the response shape is not the one this parser was written against"
        )
    rows: list[SignalRow] = []
    for position, entry in enumerate(results):
        if not isinstance(entry, dict):
            raise SourceError(f"{source} result {position} is {type(entry).__name__}, not an object")
        indicator = _text(entry, ("agregado", "nome"), position, source) or _text(
            entry, ("variavel", "nome"), position, source
        )
        period = _text(entry, (contract["period_key"], "ano"), position, source)
        localities = entry.get(contract["locality_key"])
        if not isinstance(localities, list):
            raise SourceError(f"{source} result {position} has no {contract['locality_key']!r} list")
        if not localities:
            # the key is there and the answer is nothing: a wrong ``localidades=`` parameter reads
            # exactly like this, and "no such municipality" is not the same finding as "no parser bug"
            raise SourceError(
                f"{source} result {position} returned an empty {contract['locality_key']!r} list"
                " -- check the localidades parameter before suspecting the parser"
            )
        for locality in localities:
            rows.append(_one(locality, indicator, period, contract, position, source))
    if not rows:
        # an empty list from a 200 response is the classic "the query was valid, the answer was
        # nothing" case -- and storing it silently is how a dead parameter reads as a clean run
        raise SourceError(
            f"{source} answered with {len(results)} result(s) and no usable locality rows; an empty "
            "response is not a fetched signal, so nothing will be stored"
        )
    return rows


def _text(entry: dict, path: Sequence[str], position: int, source: str) -> str | None:
    """A nested string, or ``None`` when the path is absent. Never a guess from a sibling key."""
    node: Any = entry
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    if node is None:
        return None
    if not isinstance(node, (str, int)):
        raise SourceError(f"{source} result {position} has {path[-1]!r} as {type(node).__name__}")
    return str(node)


def _one(
    locality: Any, indicator: str | None, period: str | None, contract: dict, position: int, source: str
) -> SignalRow:
    if not isinstance(locality, dict):
        raise SourceError(f"{source} result {position} has a locality that is not an object")
    raw_id = locality.get("id")
    found = LOCALITY_ID_RE.match(str(raw_id)) if raw_id is not None else None
    if found is None:
        raise SourceError(
            f"{source} result {position} locality id {raw_id!r} is not N<level>[<code>]; the parser"
            " reads the municipality code out of that shape and cannot invent one"
        )
    if int(found.group("level")) != contract["level"]:
        raise SourceError(
            f"{source} returned locality level N{found.group('level')} where the contract says"
            f" N{contract['level']}: a signal keyed on a state cannot be joined to a city"
        )
    name = locality.get("nome")
    uf = (locality.get("uf") or {}).get("sigla") if isinstance(locality.get("uf"), dict) else None
    if not name or not uf:
        raise SourceError(
            f"{source} locality {raw_id!r} has no name or uf; the city_state key cannot be built"
            " from a half-identified place, and the alternative is a silent NULL join"
        )
    raw = locality.get(contract["value_key"])
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        # a missing value is a fact about the municipality, not a zero, and NUMERIC NULL is what the
        # table says about it -- the pivot aggregates over the indicator, not over the absence
        value: float | None = None
    else:
        value = parse_value(raw, source=source, where=str(raw_id))
    if indicator is None or period is None:
        raise SourceError(
            f"{source} result {position} has no indicator name or period year; a row that cannot be"
            " named cannot be pivoted, and storing it would hide the shape change"
        )
    row = SignalRow(
        ibge_code=found.group("code"),
        city_state=fold(str(name), str(uf)),
        indicator=indicator,
        period=period,
        value=value,
    )
    validate(row)
    return row


def request_url(config: CapstoneConfig, params: dict[str, str] | None = None) -> str:
    """The URL to fetch, with the config's own query string left exactly as the student wrote it.

    Splitting the URL apart to "helpfully" insert parameters is how a configured ``localidades=N6``
    silently becomes ``localidades=N6&...`` twice; the only edit made here is appending a query.
    """
    external = config.external
    if not external.enabled:
        raise NotReadyError(
            "no external source is configured: [external].name is empty (undecided) or 'none'"
            " (declined). The decision belongs in stage-4/decisions.md item 8 and the config mirrors"
            " it; until then this step reports the missing signal instead of fetching one"
        )
    if not external.url:
        raise NotReadyError(f"{external.name} is named but has no [external].url to fetch")
    check_join(config)
    if not params:
        return external.url
    # the loop variables are named ``name``/``text`` and not ``key``/``value``: ``value`` is the token's
    # name in ``token`` below, and the test that keeps a credential out of every f-string reads names,
    # so reusing it here would either trip that guard or teach the next person to weaken it
    joined = "&".join(f"{field}={text}" for field, text in sorted(params.items()))
    separator = "&" if "?" in external.url else "?"
    return f"{external.url}{separator}{joined}"


def check_join(config: CapstoneConfig) -> None:
    """Refuse a ``join_on`` this table cannot serve, instead of quietly joining on something else.

    ``[external].join_on`` is a decision the student writes down, and ``sql/005`` builds exactly one
    join: ``city_state``. Honouring "date" would mean inventing a second view in a patch that does not
    mention one, and ignoring the key would mean the config's word means nothing. The refusal is the
    honest third option, and it says what would have to change.
    """
    key = config.external.join_on
    if key and key != "city_state":
        raise InputError(
            f"external.join_on is {key!r}, but {IBGE_AREGADOS} is stored per municipality"
            " (capstone.external_municipio, joined through capstone.v_external_signal on city_state)."
            f" A {key}-keyed signal needs its own table and view, and that is a decision for"
            " decisions.md item 8 rather than a fallback in a fetcher"
        )


def token(config: CapstoneConfig, environ: dict[str, str]) -> str | None:
    """The bearer token, if the source needs one. Read once, never printed.

    ``token_env`` names a variable, it does not hold a secret; a config that carried the value would
    end up in a commit, which is why the indirection exists and why the error message below quotes the
    variable's *name* and nothing else.
    """
    name = config.external.token_env
    if not name:
        return None
    value = environ.get(name, "").strip()
    if not value:
        raise SourceError(
            f"{config.external.name} needs the variable {name}, and it is not set here; the value is"
            " never stored, so export it in the shell that runs the fetch"
        )
    return value


def fetch(
    config: CapstoneConfig,
    *,
    opener: Callable[[str, dict[str, str]], Any] | None = None,
    environ: dict[str, str] | None = None,
    parse: Callable[[Any], Any] = json.loads,
) -> list[SignalRow]:
    """One GET, one parse, one validated list of rows. ``opener`` is injectable so the path is testable.

    Every failure -- DNS, TLS, a proxy in the way, a 500, a body that is not JSON -- becomes a
    ``SourceError`` whose message says which one. There is no exception class leaking out of this
    function and no traceback for a laptop without a route to the endpoint, because "access is absent"
    is a state this project has to be able to report.
    """
    url = request_url(config)
    headers = {
        "Accept": "application/json",
        "User-Agent": "capstone-stage-4 (student project; contact in the repository)",
    }
    credentials = token(config, environ if environ is not None else _environ())
    if credentials:
        headers["Authorization"] = f"Bearer {credentials}"
    try:
        payload = (opener or _open)(url, headers)
        body = payload if not isinstance(payload, (bytes, bytearray)) else payload.decode("utf-8")
        parsed = parse(body) if isinstance(body, (str, bytes, bytearray)) else body
    except SourceError:
        raise
    except Exception as error:  # every transport failure, reported as one kind of sentence
        raise SourceError(
            f"could not fetch {config.external.name} from {url}: {type(error).__name__}:"
            f" {str(error).strip() or '(the driver said nothing)'}"
        ) from error
    return normalize(parsed, source=config.external.name)


def load(cursor: Any, rows: Iterable[SignalRow], *, source: str) -> int:
    """Store through the SQL function that owns this table, and report how many rows went in.

    Refuses an empty list: zero rows through ``upsert`` is a no-op that *looks* like a success, which
    is how a silently empty external table survives a whole project.
    """
    materialised = list(rows)
    if not materialised:
        raise InputError(
            "load was given no rows; run the fetch or pass a fixture, and let an empty external"
            " signal be reported as an empty external signal rather than written as one"
        )
    for row in materialised:
        validate(row)
    # the casts are not decoration. Python hands a float as ``double precision`` and the server will
    # not implicitly narrow that into the function's ``numeric`` parameter, so an unqualified call
    # fails with "function ... does not exist" -- a signature error, from a function that exists. The
    # offline tests cannot see this; the smoke run found it on a real server.
    statement = (
        "SELECT capstone.upsert_external_municipio("
        "%s::text, %s::text, %s::text, %s::text, %s::numeric, %s::text)"
    )
    for row in materialised:
        cursor.execute(statement, row.as_sql_params(source))
    return len(materialised)


def match_rate(cursor: Any) -> dict[str, Any]:
    """The join's real coverage, read from the view that exists to be read.

    A rate of zero is returned, not raised: the honest finding for an unverified source is "0 of 4119
    keys matched", and the report has to be able to print that sentence without an exception on the
    way to it.
    """
    cursor.execute(
        "SELECT source_keys, external_keys, matched_keys, key_match_rate"
        " FROM capstone.v_external_match_rate"
    )
    row = cursor.fetchone()
    if row is None:
        return {"source_keys": 0, "external_keys": 0, "matched_keys": 0, "key_match_rate": None}
    rate = row[3]
    return {
        "source_keys": int(row[0]),
        "external_keys": int(row[1]),
        "matched_keys": int(row[2]),
        "key_match_rate": None if rate is None else round(float(rate), 4),
    }


def unmatched_cities(cursor: Any, *, limit: int = 10) -> list[tuple[str, str, int]]:
    """The cities that did not match, biggest first -- the part of the finding that is a list."""
    cursor.execute(
        "SELECT city, uf, orders FROM capstone.v_external_unmatched_cities ORDER BY orders DESC"
        f" LIMIT {int(limit)}"
    )
    return [(str(city), str(uf), int(orders)) for city, uf, orders in cursor.fetchall()]


def describe(config: CapstoneConfig, rate: dict[str, Any], stored: int | None = None) -> str:
    """Three sentences a report can paste: what was fetched, how much joined, what it cannot claim."""
    external = config.external
    lines = [
        f"source: {external.name or '(undecided: [external].name is empty)'}"
        f" via {external.url or '(no url configured)'}"
    ]
    lines.append(
        f"stored: {stored} row(s)" if stored is not None else "stored: not attempted (read-only run)"
    )
    if not rate["source_keys"]:
        lines.append("match rate: no customer keys at all, so the join has nothing to match against")
    else:
        lines.append(
            f"match rate: {rate['matched_keys']} of {rate['source_keys']} (city, uf) keys ="
            f" {rate['key_match_rate'] if rate['key_match_rate'] is not None else 'n/a'};"
            f" the external table holds {rate['external_keys']} key(s)"
        )
    lines.append(
        "claim: none yet -- an external signal earns its place by a lift measured on the model's own"
        " split, and a match rate is not that lift"
    )
    return "\n".join(lines)


def status(config: CapstoneConfig, *, uri: str | None = None) -> dict[str, Any]:
    """What the external layer knows without touching the network or the database.

    The default mode of ``capstone external``: a person running it on a laptop that can reach neither
    the endpoint nor their server still gets the four facts that decide what to do next -- which source
    is named, whether a URL is attached, whether a credential is expected, and whether a database is
    reachable -- instead of a stack trace about the one thing that is missing.
    """
    external = config.external
    return {
        "configured": external.enabled,
        "name": external.name or "(empty: undecided)",
        "url": external.url or "(none)",
        "join_on": external.join_on or "(none)",
        "credential": "expected" if external.token_env else "not needed",
        "token_env": external.token_env or "(unset)",
        "database": "available" if uri else "not configured (set CAPSTONE_DATABASE_URI)",
        "frame": FRAME_VIEW,
        "table": CONTRACTS[IBGE_AREGADOS]["table"],
    }


def refresh(
    config: CapstoneConfig,
    *,
    fetch_rows: bool = False,
    store: bool = False,
    report: bool = True,
    opener: Callable[[str, dict[str, str]], Any] | None = None,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fetch, store, read the match rate back -- each half optional, and only the halves that need a
    database ask for one.

    A laptop with no server can still run the fetch, which is the half that answers "is this endpoint
    reachable from Iran at all"; a server with no route to the endpoint can still report the match rate
    of whatever the table already holds. Coupling the two would mean neither can be measured alone, and
    item 12's answer is a number for each of them.

    Reading the rate *after* storing is the point of doing it here: the number the report quotes then
    came from the database the model reads, not from the loader's own arithmetic.
    """
    rows: list[SignalRow] = fetch(config, opener=opener, environ=environ) if fetch_rows else []
    out: dict[str, Any] = {
        "fetched": len(rows) if fetch_rows else None,
        "stored": None,
        "match_rate": None,
        "unmatched_cities": None,
    }
    if not (store or report):
        return out
    uri = database_uri(config, purpose="load the external signal")
    connection = connect(uri, autocommit=True, purpose="load the external signal")
    try:
        cursor = connection.cursor()
        if store:
            out["stored"] = load(cursor, rows, source=config.external.name)
        if report:
            out["match_rate"] = match_rate(cursor)
            out["unmatched_cities"] = [
                {"city": city, "uf": uf, "orders": orders}
                for city, uf, orders in unmatched_cities(cursor, limit=5)
            ]
    finally:
        connection.close()
    return out


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def _open(url: str, headers: dict[str, str]) -> Any:
    """The default transport, kept this small so the tests can replace it without monkeypatching."""
    from urllib.request import Request, urlopen

    return urlopen(Request(url, headers=headers), timeout=20).read()  # noqa: S310 - https by config
