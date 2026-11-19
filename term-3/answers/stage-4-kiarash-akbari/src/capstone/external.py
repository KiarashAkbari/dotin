"""The external signal: one request, a validated response, and a table that can be empty.

decisions.md item 8 registers ``ibge_agregados_municipio``, records the URL and the response shape the
endpoint really answers with, and records which half of that is witnessed from where: the fetch
returned HTTP 200 for the student running it, and returned nothing from the environment this code was
written in. That combination -- a decided source, a response only one of the two machines can see --
is what this module is shaped for:

* **the fetch is one request.** No retry loop, no backoff, no pagination walk. IBGE's agregados API
  publishes no rate limit, and the advice is one to two requests per second; a student project that
  hammers a public statistics endpoint with an unbounded retry loop is not a resilient client, it is
  a small attack that happens to be polite in its docstrings. A failed fetch prints the reason and
  leaves the table alone.
* **the response is validated here, not by the database.** ``capstone.external_municipio`` has CHECK
  constraints, and they stay as the backstop; the point of checking before the insert is that a schema
  change arrives as ``external returned no "localidades" key in result 0`` and not as 400 constraint
  violations after a partial write. Two envelopes are accepted, because this API has answered with
  both, and both are refused in the same places: municipality level only, a name and a state to build
  the join key from, a four-digit year to date the row by (see ``normalize``).
* **no SQL of its own.** Rows are stored by calling ``capstone.upsert_external_municipio``, the same
  rule ``sql/005_external_signal.sql`` states for every loader: a module that writes its own ``INSERT``
  bypasses the idempotency the re-apply guard tests.
* **an empty table is a result, not an error.** ``load`` and ``report`` both answer with the match rate
  they found, including ``0 of 4310 keys`` -- the count ``capstone.v_external_match_rate`` takes over
  the distinct ``(customer_city, customer_state)`` pairs of ``olist.customers``, which is the same
  denominator the external audit reports. The negative result the brief asks for -- an external signal
  that adds nothing -- has to be printable, so nothing in this module raises on it.

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
from typing import Any, Callable, Iterable, NamedTuple, Sequence

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
#:
#: The ``data_*`` keys are the second envelope, the one the endpoint actually answered with
#: (decisions.md item 8): a top-level list, ``resultados`` inside each element, the
#: municipalities inside ``series``, one municipality per ``localidade`` and the number as a string
#: under ``serie[<year>]``. They are spelled out as keys rather than written into the loop because a
#: parser that hides its assumptions in control flow is the reason the first draft needed replacing.
#: ``level`` is the one expectation that does not bend between the two: N6, a municipality, or nothing.
CONTRACTS: dict[str, dict[str, Any]] = {
    IBGE_AREGADOS: {
        "table": "capstone.external_municipio",
        "mutator": "capstone.upsert_external_municipio",
        "results_key": "result",
        "value_key": "valor",
        "period_key": "periodo",
        "locality_key": "localidades",
        "data_results_key": "resultados",
        "data_series_key": "series",
        "data_locality_key": "localidade",
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


#: where a locality's aggregation level is read from, in the shape that carries it there
LEVEL_PATH = ("nivel", "id")
#: where the two-letter state comes from, in this order, across the locality record, its block and
#: the variable element -- the three places a sigla has been seen in this API's family. Explicit
#: because the alternative ("find something that looks like a state") is how a response-shaped surprise
#: turns into a silently wrong join key, and because the first two digits of an IBGE code are *not*
#: a UF: they are a state code, and mapping one to the other is a table this repository has no witness
#: for, so a missing sigla is refused rather than repaired.
UF_PATHS = (("uf", "sigla"), ("uf",), ("sigla_uf",), ("UF",), ("nivel", "sigla"))
#: the name keys, in both languages this API has answered in
NAME_KEYS = ("nome", "name")
#: the number's home in the envelope form, if the contract's own key is absent; in the data form it is
#: ``serie[<year>]``, which is why no fallback here may be consulted for a record that has a ``serie``
VALUE_KEYS = ("valor", "value")


def _value_keys(contract: dict) -> tuple[str, ...]:
    """The contract's key first, then the English spelling -- and nothing that looks alike."""
    keys = [contract["value_key"], *VALUE_KEYS]
    return tuple(dict.fromkeys(keys))


def _dig(node: Any, path: Sequence[str]) -> Any:
    """Follow a path of dict keys; ``None`` when any step is absent or is not an object."""
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _scalar(node: Any, *path: str) -> str | None:
    """A nested string or number as text; anything else, including a dict, reads as ``None``.

    ``periodo`` is the reason this exists separately. The data form answers
    ``{"periodo": {"ini": "2022", "fim": "2022"}}``, and its year is already the key of ``serie`` --
    so an object in that slot is not a surprise to report, it is just not where this parser reads.
    """
    found = _dig(node, path)
    if isinstance(found, bool) or not isinstance(found, (str, int)):
        return None
    text = str(found).strip()
    return text or None


def _text(entry: dict, path: Sequence[str], position: int, source: str) -> str | None:
    """A nested string, or ``None`` when the path is absent. Never a guess from a sibling key.

    The envelope form's strictness lives here: in that shape a non-scalar at the end of the path means
    the response is not the one this contract describes, and saying so beats reading half of it.
    """
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


def _level_number(raw: Any) -> int | None:
    """``"N6"``, ``6`` and ``"6"`` all mean level six; anything else means the response is talking about
    something this parser cannot place, which the caller turns into a refusal."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    text = str(raw).strip().upper().replace(" ", "")
    if text.startswith("N"):
        text = text[1:]
    return int(text) if text.isdigit() else None


def _envelope_records(payload: dict, contract: dict, source: str) -> list[Sample]:
    """``{"result": [{"localidades": [{"valor": …}]}]}``: one number per locality.

    The shape the first draft was written against, from the metadata-style answers. The level comes
    out of the bracketed ``N6[3518800]`` id, which is why a bare code is refused *here*: this shape has
    no ``nivel`` to fall back on, so a record without brackets is a record whose level is unknown.
    """
    results = payload.get(contract["results_key"])
    if results is None:
        got = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise SourceError(
            f"{source} returned no {contract['results_key']!r} key (top level: {got}); "
            "the response shape is not the one this parser was written against"
        )
    found: list[Sample] = []
    for position, entry in enumerate(results):
        if not isinstance(entry, dict):
            raise SourceError(f"{source} result {position} is {type(entry).__name__}, not an object")
        indicator = _text(entry, ("agregado", "nome"), position, source) or _text(
            entry, ("variavel", "nome"), position, source
        )
        period = _text(entry, (contract["period_key"], "ano"), position, source) or _text(
            entry, (contract["period_key"],), position, source
        )
        localities = entry.get(contract["locality_key"])
        if localities is None:
            localities = entry.get("localidades")
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
            if not isinstance(locality, dict):
                raise SourceError(f"{source} result {position} has a locality that is not an object")
            raw = next((locality[key] for key in _value_keys(contract) if key in locality), None)
            found.append((position, locality, entry, entry, indicator, period, raw, False))
    return found


def _data_records(payload: list, contract: dict, source: str) -> list[Sample]:
    """The shape the endpoint really answers with: list -> ``resultados`` -> ``series`` -> record.

    Tolerant about *where* a record sits, strict about what it must hold. ``resultados`` may be a list
    of blocks or the blocks may be the elements themselves; a record either nests its ``localidade`` or
    is one. What cannot be ambiguous is the year and the level, because those two decide which row
    joins to which order.
    """
    found: list[Sample] = []
    for position, element in enumerate(payload):
        if not isinstance(element, dict):
            raise SourceError(
            f"{source} response {position} is {type(element).__name__}, not an object"
        )
        indicator = (
            _scalar(element, "variavel")
            or _scalar(element, "agregado", "nome")
            or _scalar(element, "nome")
        )
        hint = _scalar(element, contract["period_key"]) or _scalar(element, "ano")
        results_key = contract["data_results_key"]
        series_key = contract["data_series_key"]
        locality_key = contract["data_locality_key"]
        blocks = element.get(results_key)
        if blocks is None:
            blocks = [element]
        elif not isinstance(blocks, list):
            raise SourceError(
                f"{source} response {position} has `resultados` as {type(blocks).__name__}, not a list"
            )
        for block in blocks:
            if not isinstance(block, dict):
                raise SourceError(
                    f"{source} response {position} has a {results_key!r} entry that is not an object"
                )
            records = block.get(series_key)
            if records is None:
                records = [block]
            elif not isinstance(records, list):
                raise SourceError(
                    f"{source} response {position} has {series_key!r} as"
                    f" {type(records).__name__}, not a list"
                )
            if not records:
                raise SourceError(
                    f"{source} response {position} returned an empty 'series' list -- check the"
                    " localidades parameter before suspecting the parser"
                )
            for record in records:
                if not isinstance(record, dict):
                    raise SourceError(
                        f"{source} response {position} has a series record that is not an object"
                    )
                locality = record.get(locality_key)
                if locality is None:
                    # a record either nests its municipality or is one; the sample nests it, and the
                    # bare form is accepted because both have been answered by this API family
                    locality = record
                if not isinstance(locality, dict):
                    raise SourceError(
                        f"{source} response {position} has a locality that is not an object"
                    )
                series = record.get("serie")
                if series is None:
                    series = locality.get("serie")
                if isinstance(series, dict) and series:
                    for period, raw in series.items():
                        if not PERIOD_RE.match(str(period)):
                            raise SourceError(
                                f"{source} response {position} has a serie key {period!r} that is not a"
                                " four-digit year; a row cannot be dated, and dropping it silently would"
                                " read as a clean run"
                            )
                        found.append(
                            (position, locality, block, element, indicator, str(period), raw, True)
                        )
                    continue
                if series:
                    raise SourceError(
                        f"{source} response {position} has `serie` as {type(series).__name__}, not an"
                        " object keyed by year"
                    )
                raw = next((locality[key] for key in VALUE_KEYS if key in locality), None)
                found.append((position, locality, block, element, indicator, hint, raw, True))
    return found


def _code_and_level(
    locality: dict, raw_id: Any, contract: dict, position: int, source: str, *, from_nivel: bool
) -> str:
    """The seven-digit municipality code, with the level checked on the way out."""
    text = str(raw_id).strip() if raw_id is not None else ""
    bracketed = LOCALITY_ID_RE.match(text)
    level = _level_number(_dig(locality, LEVEL_PATH)) if from_nivel else None
    if bracketed is not None:
        bracket_level = int(bracketed.group("level"))
        if level is None:
            level = bracket_level
        elif level != bracket_level:
            raise SourceError(
                f"{source} result {position} disagrees with itself: `nivel.id` says N{level} and the id"
                f" says {raw_id!r}. A row cannot be joined on a level the response argues about"
            )
    if level is None:
        raise SourceError(
            f"{source} result {position} locality id {raw_id!r} is not N<level>[<code>] and carries no"
            " `nivel.id`; the parser reads the municipality code and the aggregation level out of those"
            " two and cannot invent either"
        )
    if level != contract["level"]:
        raise SourceError(
            f"{source} returned locality level N{level} where the contract says"
            f" N{contract['level']}: a signal keyed on a state cannot be joined to a city"
        )
    return bracketed.group("code") if bracketed is not None else text


#: where a municipality's state lives in IBGE's own administrative list
UF_IN_LIST_PATH = ("microrregiao", "mesorregiao", "UF", "sigla")
#: the list that turns a seven-digit municipality code into the two letters ``005`` keys on. It is a
#: module constant and not a config key because it is not the student's decision: which agregado and
#: which variable to read is theirs, the shape and the plumbing of the source are this module's
#: contract. Free, public and keyless -- the same three criteria item 8 applied to the signal, on the
#: same host, so a mirror would be a new decision rather than a new default.
LOCALIDADES_URL = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios"
#: the seam that lets the offline harness answer this URL without a network. Nothing else should set
#: it, and a wrong value costs a refusal rather than a silent skip.
LOCALIDADES_URL_ENV = "CAPSTONE_IBGE_LOCALIDADES_URL"


class Municipality(NamedTuple):
    """One entry of IBGE's administrative list: what the place is called, and which state it is in.

    Both halves travel together on purpose. A row whose state was looked up but whose city was copied
    from the signal keyed as ``sao paulo - sp|sp`` -- the answer writes ``São Paulo - SP`` into
    ``localidade.nome`` -- and joined to nothing, which is how a fetched-and-stored table of 5570 rows
    still reported ``0 of 4310`` keys. A key is a *pair*, so a lookup that supplies one half has to
    supply the spelling of the other, and ``name`` is empty rather than guessed when the list has none.
    """

    sigla: str
    name: str = ""


def parse_uf_map(payload: Any) -> tuple[dict[str, Municipality], int, int]:
    """``[{ "id": 1100015, "nome": "Alta Floresta D'Oeste", "microrregiao": { … "UF": { "sigla": "RO" }}}]``.

    Returns the map plus two counts -- entries seen, entries that carried a state -- because the report
    has to be able to say ``5568 of 5570`` rather than imply a complete list. Each value is a
    ``Municipality``, so the name arrives with the sigla instead of being cut out of the signal's
    ``"City - UF"`` display string afterwards. A missing entry is not a
    broken list: the municipality it describes then hits the refusal in ``_row``, which is where a
    unknown code belongs. A list that yields *nothing* is a broken list, and says so.
    """
    if not isinstance(payload, list):
        raise SourceError(
            "the municipality reference is not the one this parser was written against: the list at"
            f" {LOCALIDADES_URL} answered with {type(payload).__name__} where an array of municipalities"
            " was expected"
        )
    mapping: dict[str, Municipality] = {}
    with_uf = 0
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            continue
        code = str(raw_id).strip()
        if not IBGE_CODE_RE.match(code):
            continue
        holder = _dig(entry, UF_IN_LIST_PATH[:-1])
        sigla = _scalar(holder, "sigla") or _scalar(entry, "UF", "sigla")
        if not sigla or len(sigla) != 2:
            continue
        # the name is read from the same entry, never from the signal's display string; an entry
        # without one is still a usable state, so the row keeps the name the answer gave it
        mapping[code] = Municipality(
            sigla=sigla,
            name=next((_scalar(entry, key) for key in NAME_KEYS if _scalar(entry, key)), "") or "",
        )
        with_uf += 1
    if not mapping:
        raise SourceError(
            f"the municipality reference from {LOCALIDADES_URL} has no usable municipality code: it"
            f" answered with {len(payload)} entries and none of them carried an id and"
            f" {str(UF_IN_LIST_PATH)}; the response shape of the *reference* changed, not the signal's"
        )
    return mapping, len(payload), with_uf


def _sigla(*holders: Any) -> str | None:
    """The state's two letters, from the first place they are found in -- and from no other place."""
    for holder in holders:
        for path in UF_PATHS:
            found = _scalar(holder, *path)
            if found:
                return found
    return None


def _row(
    locality: dict,
    block: Any,  # the enclosing block, which sometimes carries the sigla the locality omits
    element: Any,
    indicator: str | None,
    period: str | None,
    raw: Any,
    contract: dict,
    position: int,
    source: str,
    *,
    level_from_nivel: bool,
    municipality_by_code: dict[str, Municipality] | None = None,
) -> SignalRow:
    """One locality record as one row of ``capstone.external_municipio``, or a refusal that says why."""
    raw_id = locality.get("id")
    code = _code_and_level(locality, raw_id, contract, position, source, from_nivel=level_from_nivel)
    name = next((_scalar(locality, key) for key in NAME_KEYS if _scalar(locality, key)), None)
    uf = _sigla(locality, block, element)
    if not name:
        raise SourceError(
            f"{source} locality {raw_id!r} has no name; the city_state key cannot be built from a"
            f" half-identified place. Keys this locality carried: {sorted(locality)}"
        )
    if not uf:
        # the two cases are told apart because only one of them is worth a second request: a response
        # that named the place but not its state is missing a field the administrative list has,
        # while a response that named nothing has a shape problem no lookup can fix
        if municipality_by_code is None:
            raise SourceError(
                f"{source} locality {raw_id!r} ({name}) carries no uf, and no municipality reference"
                " was supplied for this run; 005 keys on '<city>|<uf>' because 163 names recur in two"
                " states (decisions.md §8), so the alternative to refusing is inventing a state."
                f" Keys this locality carried: {sorted(locality)}",
                context={"missing_uf": code},
            )
        entry = municipality_by_code.get(code)
        if entry is None:
            raise SourceError(
                f"{source} locality {raw_id!r} ({name}) has no uf and the municipality code is not in"
                f" the reference list from {LOCALIDADES_URL}; a code IBGE's own administrative list"
                " does not know is not a code to join orders onto"
            )
        # the pair, or neither: the list is what tells us which state the code belongs to, and it is
        # equally what the place is *called* in a key that has to agree with 005's lower(city)||uf
        uf = entry.sigla
        if entry.name:
            name = entry.name
    if indicator is None or period is None:
        raise SourceError(
            f"{source} result {position} has no indicator name or period year; a row that cannot be"
            " named cannot be pivoted, and storing it would hide the shape change"
        )
    value: float | None = None
    if raw is not None and not (isinstance(raw, str) and not raw.strip()):
        value = parse_value(raw, source=source, where=str(raw_id))
    row = SignalRow(
        ibge_code=code,
        city_state=fold(str(name), str(uf)),
        indicator=indicator,
        period=period,
        value=value,
    )
    validate(row)
    return row


def normalize(
    payload: Any,
    *,
    source: str = IBGE_AREGADOS,
    municipality_by_code: dict[str, Municipality] | None = None,
) -> list[SignalRow]:
    """The API's JSON, as rows. Pure, and the reason the contract is testable without a network.

    Two envelopes are understood, because this API has answered with both:

    * the **data form** -- a top-level list whose elements carry ``resultados``, whose blocks carry
      ``series``, and whose records are a ``localidade`` object plus a ``serie`` keyed by year. That is
      what ``/api/v3/agregados/4714/periodos/2022/variaveis/614?localidades=N6[all]`` returned on
      2026-11-19, from the student's own call; here the level comes from ``nivel.id`` and the value is
      a *string* such as ``"3.04"``.
    * the **envelope form** -- ``{"result": [{"localidades": [{"valor": …}]}]}``, which the first draft
      was written against and which the metadata-style answers still use. There the id is the bracketed
      ``N6[3518800]`` and a non-scalar where a scalar belongs is an error worth naming.

    ``municipality_by_code`` is the municipality list's ``{code: Municipality(sigla, name)}``,
    consulted **only** for a locality that has a name but no state of its own -- which is what the
    endpoint actually sends. A sigla in the response always wins over the reference, because the answer
    about a place beats a table about its code; when the table is consulted, its name is used too, for
    the same reason: the signal writes ``São Paulo - SP`` and ``005`` keys on ``sao paulo|sp``.

    A shape surprise raises ``SourceError`` naming the key that is missing and where it was looked for.
    That sentence is the whole difference between "the API is down" and "the API answered with something
    we do not understand", and the second one is the failure that gets silently swallowed into an
    empty table.
    """
    contract = CONTRACTS.get(source)
    if contract is None:
        known = ", ".join(sorted(CONTRACTS))
        raise InputError(
            f"{source!r} has no response contract; the ones this module knows are: {known}. "
            "Naming a source in the config is a decision about *which* API; teaching the code its "
            "shape is a second decision, and inventing a parser is not how it gets made."
        )
    if isinstance(payload, list):
        samples = _data_records(payload, contract, source)
        results: Any = payload
    elif isinstance(payload, dict):
        samples = _envelope_records(payload, contract, source)
        results = payload.get(contract["results_key"]) or []
    else:
        raise SourceError(
            f"{source} answered with {type(payload).__name__} where an object or a list was expected;"
            " the response shape is not the one this parser was written against"
        )
    if not samples:
        # an empty list from a 200 response is the classic "the query was valid, the answer was
        # nothing" case -- and storing it silently is how a dead parameter reads as a clean run
        raise SourceError(
            f"{source} answered with {len(results)} result(s) and no usable locality rows; an empty"
            " response is not a fetched signal, so nothing will be stored"
        )
    return [
        _row(
            locality,
            block,
            element,
            indicator,
            period,
            raw,
            contract,
            position,
            source,
            level_from_nivel=nivel,
            municipality_by_code=municipality_by_code,
        )
        for position, locality, block, element, indicator, period, raw, nivel in samples
    ]


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


def _get(
    config: CapstoneConfig,
    url: str,
    *,
    opener: Callable[[str, dict[str, str]], Any] | None,
    environ: dict[str, str],
    parse: Callable[[Any], Any],
    label: str,
    authorize: bool,
) -> Any:
    """One GET, one parse, and no more than one of each. ``opener`` is injectable so the path is testable.

    The bearer token goes to the configured URL and nowhere else: a credential that rides along to a
    second host because the code was feeling general is how a key leaks into a log. Every failure --
    DNS, TLS, a proxy in the way, a 500, a body that is not JSON -- becomes a ``SourceError`` whose
    message says which one, so that "access is absent" stays a state this project can report.
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": "capstone-stage-4 (student project; contact in the repository)",
    }
    if authorize:
        credentials = token(config, environ)
        if credentials:
            headers["Authorization"] = f"Bearer {credentials}"
    try:
        payload = (opener or _open)(url, headers)
        body = payload if not isinstance(payload, (bytes, bytearray)) else payload.decode("utf-8")
        return parse(body) if isinstance(body, (str, bytes, bytearray)) else body
    except SourceError:
        raise
    except Exception as error:  # every transport failure, reported as one kind of sentence
        raise SourceError(
            f"could not fetch {label} from {url}: {type(error).__name__}:"
            f" {str(error).strip() or '(the driver said nothing)'}"
        ) from error


def fetch(
    config: CapstoneConfig,
    *,
    opener: Callable[[str, dict[str, str]], Any] | None = None,
    environ: dict[str, str] | None = None,
    parse: Callable[[Any], Any] = json.loads,
    notes: dict[str, Any] | None = None,
    force_reference: bool = False,
) -> list[SignalRow]:
    """The signal, and -- only if the response forgot which state each place is in -- the list that says.

    Two GETs at most, and the second is spent on one condition: a locality that has a name but no
    ``uf``. That is what IBGE's agregados endpoint actually returns, so the alternative was either to
    accept a join key built from a guessed state or to leave item 12 unable to load anything at all.
    ``force_reference`` exists for the harness that measures the cost, and ``notes`` is where the
    second request reports its size so the answer is on the page and not in the code.
    """
    env = environ if environ is not None else _environ()
    payload = _get(
        config,
        request_url(config),
        opener=opener,
        environ=env,
        parse=parse,
        label=config.external.name,
        authorize=True,
    )
    reference_url = str(env.get(LOCALIDADES_URL_ENV) or "").strip() or LOCALIDADES_URL

    def reference_map() -> dict[str, str]:
        try:
            body = _get(
                config,
                reference_url,
                opener=opener,
                environ=env,
                parse=parse,
                label="the municipality reference",
                authorize=False,
            )
        except SourceError as error:
            raise SourceError(
                f"{error}; the municipality reference at {reference_url} is the only source for the"
                " state each locality belongs to, and without it the join key cannot be built"
            ) from error
        mapping, seen, with_uf = parse_uf_map(body)
        if notes is not None:
            # ``named`` is reported separately because it is the number that decides whether a row's city
            # half comes from the list or from the answer's display string, and a reader comparing two
            # runs' keys needs to know which of the two they got
            named = sum(1 for entry in mapping.values() if entry.name)
            notes["uf_reference"] = {
                "url": reference_url,
                "entries": seen,
                "codes": with_uf,
                "named": named,
            }
        return mapping

    if force_reference:
        return normalize(payload, source=config.external.name, municipality_by_code=reference_map())
    try:
        return normalize(payload, source=config.external.name)
    except SourceError as error:
        if not error.context.get("missing_uf"):
            raise
        return normalize(payload, source=config.external.name, municipality_by_code=reference_map())


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
    notes: dict[str, Any] = {}
    rows: list[SignalRow] = (
        fetch(config, opener=opener, environ=environ, notes=notes) if fetch_rows else []
    )
    out: dict[str, Any] = {
        "fetched": len(rows) if fetch_rows else None,
        "stored": None,
        "match_rate": None,
        "unmatched_cities": None,
        # present only when the response omitted a state and the administrative list supplied it; the
        # alternative was to print a line about a request that may not have happened
        "uf_reference": notes.get("uf_reference"),
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
