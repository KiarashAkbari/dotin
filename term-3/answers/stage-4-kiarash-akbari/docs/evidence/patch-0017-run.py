#!/usr/bin/env python3
"""Produce the log that patch 0017 ships with: a fetched table that joined to nothing, and why.

0016 taught the fetcher to ask IBGE's administrative list for the state a municipality answer forgot.
It worked -- the student's own run stored ``5570`` rows and reported ``uf map: 5570 of 5571`` -- and the
match rate stayed ``0 of 4310``. The reason was sitting inside one field: the agregados answer writes
``São Paulo - SP`` into ``localidade.nome``, so a row keyed from that name is ``sao paulo - sp|sp``, while
``sql/005`` builds its half of the join as ``lower(customer_city) || '|' || lower(customer_state)``, which
is ``sao paulo|sp``. The rows were perfect and the key was a display string.

0017 takes the city from the same record the state came from. It does **not** cut a suffix off the
answer's name: a rule that removes " - UF" would be a guess about how places are spelled, it would fire
on names that legitimately contain a dash, and it would leave the key assembled out of two sources.

Four things are recorded:

1. the offline suite, on the plain interpreter -- the gate every patch in this set passes;
2. the student's three codes, run through the parser twice: against ``git archive`` of the previous
   commit's ``src/`` (which reproduces ``sao paulo - sp|sp`` exactly as their database printed it) and
   against this tree (which produces the three keys ``005`` expects). The red half is a real checkout;
   nothing here is an edited-out fix;
3. the shipped CLI against a local two-path server, where the request budget is counted rather than
   restated -- still two GETs for a body with no states, still one for a body that carries them -- and
   the report line that now says the names came from the list too;
4. the measurement that answers item 12's question the way their report card asks for it: on a
   disposable cluster seeded with three ``olist.customers`` rows, the *same* fetch loaded by the old
   code gives ``matched 0`` and loaded by the new one gives ``matched 3``. Both halves are read back
   from ``capstone.v_external_match_rate``, so the number is the database's, not the loader's.

What is deliberately *not* here: any claim about their real 4,310 keys. The seed above is three rows
this harness wrote; the project's rate is the one their ``--report`` prints, and the SQL for it is at
the bottom of section 4. Sandbox timings are «سندباکس».

Run it from ``stage-4/``::

    python3 docs/evidence/patch-0017-run.py | tee docs/evidence/patch-0017-run.txt

``--rev`` names the tree to compare against (default ``HEAD~1``: right after ``git am`` in the mirror
tree, that is the 0016 checkout). Section 4 needs ``pip install pgserver 'psycopg[binary]'``.
"""

from __future__ import annotations

import http.server
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
from pathlib import Path
from typing import Any

STAGE = Path(__file__).resolve().parents[2]
EVIDENCE = STAGE / "docs" / "evidence"
#: the three municipality records IBGE's own list returns for the codes the student's query printed
REFERENCE = EVIDENCE / "ibge-localidades-three-codes.json"
#: the city names as the Olist corpus spells them -- unaccented, which is what ``005`` lowercases
SEED_CUSTOMERS = (
    ("0000000000000000000000000000a101", "Sao Paulo", "SP"),
    ("0000000000000000000000000000a102", "Rio de Janeiro", "RJ"),
    ("0000000000000000000000000000a103", "Alta Floresta D'Oeste", "RO"),
)
MIGRATIONS = (
    "001_capstone_schema.sql",
    "002_time_guard.sql",
    "003_feature_views.sql",
    "004_as_of_functions.sql",
    "005_external_signal.sql",
)
#: the tracked example, copied and decided at run time; ignored in ``stage-4/.gitignore``
TEMP_CONFIG = "patch-0017-evidence.toml"
CONFIG_REL = f"config/{TEMP_CONFIG}"

PROBE = '''"""The suffixed answer, resolved twice: as the old parser keys it and as the new one does.

The probe prints one line per row -- ``code  key  value`` -- and says which spelling authority the tree
it was pointed at used, so the two runs are readable side by side without knowing either patch.
"""
import json
import pathlib
import sys

from capstone import external

body = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
reference = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))

record = getattr(external, "Municipality", None)
params = external.normalize.__code__.co_varnames
keyword = "municipality_by_code" if "municipality_by_code" in params else "uf_by_code"
print(f"reference type: {'Municipality(sigla, name)' if record is not None else 'bare sigla'}")
print(f"normalize keyword: {keyword}")

raw = external.parse_uf_map(reference)[0]
mapping = raw if record is not None else {
    code: value.sigla if hasattr(value, "sigla") else value for code, value in raw.items()
}
try:
    rows = external.normalize(body, **{keyword: mapping})
except Exception as error:  # a refusal is a result worth reading, not a stack to hide
    print(f"refused: {type(error).__name__}: {error}")
    raise SystemExit(1)
print(f"{len(rows)} row(s), {len(mapping)} codes consulted")
suffixed = 0
for row in rows:
    city = row.city_state.split("|")[0]
    marker = "  <- display suffix" if " - " in city else ""
    suffixed += bool(marker)
    print(f"  {row.ibge_code}  {row.city_state}  {row.value!r}{marker}")
print(f"rows whose key still carries the answer's suffix: {suffixed} of {len(rows)}")
raise SystemExit(0)
'''

try:
    import pgserver  # type: ignore[import-not-found]

    PSQL = Path(str(pgserver.__file__)).parent / "pginstall" / "bin" / "psql"
    IMPORT_ERROR = ""
except ModuleNotFoundError as error:  # the skip path, recorded rather than raised
    pgserver = None  # type: ignore[assignment]
    PSQL = Path("psql")
    IMPORT_ERROR = str(error)

ELAPSED = re.compile(r" in [0-9]+\.[0-9]+s")
TMPDIR = re.compile(r"/tmp/capstone-001[67]-[a-z0-9_-]+")
PORT = re.compile(r"127\.0\.0\.1:[0-9]+")


def say(text: str = "") -> None:
    print(text, flush=True)


def rule(title: str) -> None:
    say()
    say("-" * 72)
    say(title)
    say("-" * 72)


def scrub(lines: list[str]) -> list[str]:
    """The per-run accidents, flattened, so two runs of this file compare byte for byte."""
    out = []
    for line in lines:
        line = ELAPSED.sub(r" in <t>s", line)
        out.append(PORT.sub("127.0.0.1:<port>", TMPDIR.sub("<scratch>", line)))
    return out


def step(
    script: list[str],
    *,
    env: dict[str, str],
    label: str = "",
    interpreter: str | None = None,
    tail: int = 0,
    cwd: Path | None = None,
) -> tuple[int, str]:
    """One child process, its output inlined, and its exit code handed back for a check to use."""
    chosen = interpreter or Path(sys.executable).name
    command = scrub([f"$ {chosen} {' '.join(script)}"])[0]
    if len(command) > 96:
        say(textwrap.fill(command, width=96, subsequent_indent="  ... "))
    else:
        say(command)
    proc = subprocess.run(
        [interpreter or sys.executable, *script],
        cwd=str(cwd or STAGE),
        env=env,
        capture_output=True,
        text=True,
    )
    text = proc.stdout + (("\n" + proc.stderr) if proc.stderr.strip() else "")
    shown = text.splitlines() if not tail else text.splitlines()[-tail:]
    for line in scrub(shown):
        say(f"  | {line}"[:99])
    say(f"  [exit {proc.returncode}] {label}")
    return proc.returncode, text


def check(name: str, ok: bool, detail: str = "") -> bool:
    # scrubbed and clipped like every other line: a check's detail is usually a tail of the child's
    # output, and a bare port in it is what makes two honest runs look like different ones
    text = scrub([f"  check[{name}]: {'ok' if ok else 'FAILED'}{(' -- ' + detail) if detail else ''}"])[0]
    say(text[:99])
    return ok


def psql(uri: str, statement: str) -> str:
    proc = subprocess.run(
        [str(PSQL), uri, "-v", "ON_ERROR_STOP=1", "-q", "-t", "-A", "-c", statement],
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout + proc.stderr).strip()


def repo_root() -> Path:
    """Where ``git`` runs, which is not the stage's parent in the mirror tree."""
    found = subprocess.run(
        ["git", "-C", str(STAGE), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(found.stdout.strip()) if found.returncode == 0 else STAGE.parent


REPO = repo_root()


def find_ddl() -> Path | None:
    """stage-2's Olist DDL, in either layout, searched one directory level deep."""
    name = "001_create_olist_schema.sql"
    for base in (STAGE.parent, REPO):
        for pattern in (f"stage-2*/sql/{name}", f"*/stage-2*/sql/{name}"):
            for candidate in sorted(base.glob(pattern)):
                if candidate.is_file():
                    return candidate
    return None


def pre_patch_src(dest: Path, rev: str) -> Path | None:
    """The ``src/`` of ``rev``, whole -- the comparison half, with nothing edited out of either tree."""
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-tree", "-r", "--name-only", rev, "--", "."],
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode:
        return None
    hits = [line for line in listed.stdout.split("\n") if line.endswith("src/capstone/external.py")]
    if len(hits) != 1:
        return None
    src_dir = str(Path(hits[0]).parent.parent)
    extract = subprocess.run(
        f"git -C {REPO} archive {rev} {src_dir} | tar -x -C {dest}",
        shell=True,
        capture_output=True,
        text=True,
    )
    if extract.returncode:
        return None
    found = next(iter(sorted(dest.glob("**/src/capstone/external.py"))), None)
    return found.parents[1] if found is not None else None


def suffixed_body() -> list:
    """The recorded response, rewritten into the shape the live endpoint returns.

    Two changes, both measured rather than imagined: ``uf`` is absent from every locality (the student's
    own ``curl`` said so), and each ``nome`` carries the state the agregados display sticks on the end.
    The codes are the three their query printed, and the third row keeps an empty ``serie`` so a NULL
    still survives the whole path.
    """
    data = json.loads((EVIDENCE / "ibge-4714-614-sample.json").read_text(encoding="utf-8"))
    display = {
        "1100015": "Alta Floresta D'Oeste - RO",
        "3550308": "São Paulo - SP",
        "3304557": "Rio de Janeiro - RJ",
    }
    for element in data:
        for block in element.get("resultados", []):
            for record in block.get("series", []):
                locality = record["localidade"]
                locality.pop("uf", None)
                code = str(locality["id"])
                locality["nome"] = display[code]
    return data


def example_config(dest: Path, url: str) -> Path:
    text = (STAGE / "config" / "capstone.example.toml").read_text(encoding="utf-8")
    head = text[: text.index("[external]")]
    dest.write_text(
        head
        + textwrap.dedent(
            f'''\
            [external]
            # the block a student's config/capstone.toml carries, with both URLs answered locally
            name = "ibge_agregados_municipio"
            url = "{url}"
            token_env = ""
            join_on = "city_state"
            '''
        ),
        encoding="utf-8",
    )
    return dest


class Local(http.server.SimpleHTTPRequestHandler):
    """Two paths, one body each, and a count per path -- the request budget is still under test."""

    files: dict[str, str] = {}
    hits: list[str] = []

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server's own spelling
        name = self.path.strip("/")
        type(self).hits.append(name)
        target = type(self).files.get(name)
        if target is None:
            self.send_error(404, "the harness serves only the two recorded bodies")
            return
        body = Path(target).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(directory: Path, bodies: dict[str, str]) -> Any:
    Local.files = bodies
    Local.hits = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Local)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def rate_from(text: str) -> str:
    """The one clause of the report that matters here, pulled out so a check can compare it."""
    found = re.search(r"match rate: (\d+) of (\d+)", text)
    return f"{found.group(1)} of {found.group(2)}" if found else "not printed"


def main(argv: list[str]) -> int:
    plain = shutil.which("python3") or sys.executable
    rev = argv[argv.index("--rev") + 1] if "--rev" in argv else "HEAD~1"
    failures = 0
    work = Path(tempfile.mkdtemp(prefix="capstone-0017-"))

    def drop_temp_config() -> None:
        (STAGE / "config" / TEMP_CONFIG).unlink(missing_ok=True)

    (work / "signal.json").write_text(json.dumps(suffixed_body(), ensure_ascii=False), encoding="utf-8")
    (work / "reference.json").write_text(REFERENCE.read_text(encoding="utf-8"), encoding="utf-8")
    (work / "probe.py").write_text(PROBE, encoding="utf-8")

    base_env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "PYTHONPATH": f"{STAGE / 'src'}:{STAGE / 'tests'}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    rule("1. the offline suite, plain interpreter, no database and no network")
    code, text = step(
        ["-m", "unittest", "discover", "-s", "tests", "-t", "."],
        env=base_env,
        interpreter=plain,
        label="the whole suite, with MunicipalityNameTests in it",
        tail=8,
    )
    failures += not check("offline suite green", code == 0, f"exit {code}")
    tally = re.search(r"Ran (\d+) tests", text)
    verdict = re.search(r"^(OK\b.*|FAILED.*)$", text, re.M)
    say(
        f"  tally: {tally.group(1)} tests -- {verdict.group(1).strip()}"
        if tally and verdict
        else "  tally: unreadable"
    )

    rule("2. the three codes the student printed, through the old parser and the new one")
    old_src: Path | None = None
    scratch = work / "pre"
    scratch.mkdir()
    old_src = pre_patch_src(scratch, rev)
    if old_src is None:
        say(f"  not measured: `git archive {rev}` gave no src/capstone/external.py here; in the mirror")
        say("  tree the default (HEAD~1) is the tree with 0016 applied, which is what this compares to.")
    else:
        say("  pre-patch src: the previous commit's own `src/`, unpacked into a scratch directory")
        code_old, text_old = step(
            ["probe.py", str(work / "signal.json"), str(work / "reference.json")],
            env=dict(base_env, PYTHONPATH=str(old_src)),
            label="what 0016 produced: rows that exist and keys that join to nothing",
            tail=10,
            cwd=work,
        )
        failures += not check("the old tree fetched rows", code_old == 0, f"exit {code_old}")
        failures += not check(
            "and every key carried the display suffix",
            "3 of 3" in text_old,
            rate_from(text_old) if "match rate" in text_old else text_old.strip().splitlines()[-1],
        )
        failures += not check(
            "sao paulo - sp|sp is what their table held",
            "sao paulo - sp|sp" in text_old,
        )
    code_new, text_new = step(
        ["probe.py", str(work / "signal.json"), str(work / "reference.json")],
        env=base_env,
        label="what 0017 produces: the same three rows, keyed the way 005 spells them",
        tail=10,
        cwd=work,
    )
    failures += not check("this tree fetched rows", code_new == 0, f"exit {code_new}")
    for wanted in ("sao paulo|sp", "rio de janeiro|rj", "alta floresta d'oeste|ro"):
        failures += not check(f"key {wanted}", wanted in text_new)
    failures += not check(
        "and no row carries the suffix any more",
        "0 of 3" in text_new,
        text_new.strip().splitlines()[-1] if text_new.strip() else "nothing printed",
    )

    rule("3. the shipped CLI: the budget is unchanged, and the report says where the names came from")
    bodies = {
        "signal": str(work / "signal.json"),
        "municipios": str(work / "reference.json"),
        "with-uf": str(EVIDENCE / "ibge-4714-614-sample.json"),
    }
    served = work / "http"
    served.mkdir()
    for name, source in bodies.items():
        (served / name).write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
    bodies = {name: str(served / name) for name in bodies}
    server = serve(served, bodies)
    port = int(server.server_address[1])
    temp = STAGE / "config" / TEMP_CONFIG
    if temp.exists():
        say(f"  refusing to overwrite an existing {temp}; remove it and re-run")
        return 1
    try:
        config = example_config(temp, f"http://127.0.0.1:{port}/signal")
        cli_env = dict(
            base_env,
            PYTHONPATH=str(STAGE / "src"),
            CAPSTONE_IBGE_LOCALIDADES_URL=f"http://127.0.0.1:{port}/municipios",
        )
        Local.hits = []
        code, text = step(
            ["-m", "capstone", "external", "--fetch", "--config", CONFIG_REL],
            env=cli_env,
            label="the answer is named but not located, so the list is asked for both halves",
            tail=8,
        )
        failures += not check("--fetch exits 0", code == 0, f"exit {code}")
        failures += not check("it counted the rows", "fetched: 3 row(s)" in text)
        failures += not check(
            "the report line says the names came from the list",
            "municipality codes and their names" in text,
            next((line for line in text.splitlines() if "uf map" in line), "(no line)"),
        )
        failures += not check(
            "two requests, no more",
            Local.hits == ["signal", "municipios"],
            ", ".join(Local.hits) or "none",
        )

        config_with = example_config(temp, f"http://127.0.0.1:{port}/with-uf")
        Local.hits = []
        code, text = step(
            ["-m", "capstone", "external", "--fetch", "--config", CONFIG_REL],
            env=cli_env,
            label="a body that answers both questions: no second request, and no name rewrite",
            tail=6,
        )
        failures += not check("--fetch on a complete body exits 0", code == 0, f"exit {code}")
        failures += not check(
            "exactly one request for a body that needs nothing",
            Local.hits == ["with-uf"],
            ", ".join(Local.hits) or "none",
        )
        failures += not check("and no uf-map line is printed", "uf map" not in text)
    finally:
        server.shutdown()
        server.server_close()

    rule("4. the join, measured on a real server: the same fetch, keyed two ways")
    if pgserver is None:
        say("  skipped: pgserver is not importable on this interpreter (" + IMPORT_ERROR + ").")
        say("  install it -- pip install pgserver 'psycopg[binary]' -- and re-run; a machine without")
        say("  the dev extra has not failed a measurement, so this section exits 0.")
        drop_temp_config()
        return 0 if failures == 0 else 1

    database = None
    fetch_server = None
    try:
        database = pgserver.get_server((work / "pgdata").as_posix(), cleanup_mode="stop")
        uri = str(database.get_uri())
        ddl = find_ddl()
        scripts = ([ddl] if ddl else []) + [STAGE / "sql" / name for name in MIGRATIONS]
        if ddl is None:
            say("  no `001_create_olist_schema.sql` beside this checkout, so `olist.customers` cannot")
            say("  exist and the rate would read 0 of 0 -- which is a measurement of nothing. Stopped.")
            return 1
        for script in scripts:
            proc = subprocess.run(
                [str(PSQL), uri, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(script)],
                capture_output=True,
                text=True,
                check=False,
            )
            failures += not check(f"applied {script.name}", proc.returncode == 0, proc.stderr.strip()[:160])
        seed = ", ".join(
            f"('{cid}', '{cid}', '01310', '{city.replace(chr(39), chr(39) * 2)}', '{uf}')"
            for cid, city, uf in SEED_CUSTOMERS
        )
        inserted = psql(uri, f"INSERT INTO olist.customers (customer_id, customer_unique_id,"
                             f" customer_zip_code_prefix, customer_city, customer_state) VALUES {seed};")
        say(f"  seeded olist.customers with {len(SEED_CUSTOMERS)} rows (this harness wrote them: {inserted})")
        failures += not check("the seed landed", psql(uri, "SELECT count(*) FROM olist.customers;") == "3")

        fetch_server = serve(served, bodies)
        live_env = dict(
            base_env,
            PYTHONPATH=str(STAGE / "src"),
            CAPSTONE_DATABASE_URI=uri,
            XDG_RUNTIME_DIR=str(work),
            CAPSTONE_IBGE_LOCALIDADES_URL=(
                f"http://127.0.0.1:{fetch_server.server_address[1]}/municipios"
            ),
        )
        old_env = dict(live_env, PYTHONPATH=str(old_src)) if old_src is not None else None
        config_load = example_config(
            temp, f"http://127.0.0.1:{fetch_server.server_address[1]}/signal"
        )

        if old_env is None:
            say("  not measured: no pre-patch src to load with, so there is nothing to compare the rate to.")
        else:
            code, text = step(
                ["-m", "capstone", "external", "--load", "--config", CONFIG_REL],
                env=old_env,
                label="0016's loader, 0017's data: three rows in, none of them joinable",
                tail=7,
            )
            say(f"  match rate through the old key: {rate_from(text)}")
            failures += not check("--load on the old tree exits 0", code == 0, f"exit {code}")
            failures += not check("it stored three rows", "stored: 3 row(s)" in text)
            failures += not check(
                "and the rate is zero with the suffix in the key",
                rate_from(text) == "0 of 3",
                rate_from(text),
            )
            cleaned = psql(uri, "DELETE FROM capstone.external_municipio;")
            say(f"  table emptied for the second half (scratch cluster only): {cleaned or 'ok'}")
        code, text = step(
            ["-m", "capstone", "external", "--load", "--config", CONFIG_REL],
            env=live_env,
            label="this tree, the same three rows: the keys the view expects",
            tail=7,
        )
        say(f"  match rate through the new key: {rate_from(text)}")
        failures += not check("--load exits 0", code == 0, f"exit {code}")
        failures += not check("it stored three rows", "stored: 3 row(s)" in text)
        failures += not check(
            "and now every seeded city joins",
            rate_from(text) == "3 of 3",
            rate_from(text),
        )
        keys = psql(uri, "SELECT city_state FROM capstone.external_municipio ORDER BY 1")
        rates = psql(
            uri,
            "SELECT source_keys, external_keys, matched_keys, key_match_rate"
            " FROM capstone.v_external_match_rate",
        )
        say("  the join keys in the table: " + keys.replace("\n", ", "))
        say(f"  v_external_match_rate (source|external|matched|rate): {rates}")
        say("  the same question on the real corpus, which is the number item 12 asks for:")
        say("      SELECT count(*) AS rows, count(value) AS non_null,")
        say("             max(fetched_at) AS newest FROM capstone.external_municipio;")
        say("      SELECT source_keys, external_keys, matched_keys, key_match_rate")
        say("        FROM capstone.v_external_match_rate;")
    except Exception as error:  # a harness that dies mid-way is worse than one that skips
        say(f"  not measured: {type(error).__name__}: {error}")
        failures += 1
    finally:
        drop_temp_config()
        if fetch_server is not None:
            fetch_server.shutdown()
            fetch_server.server_close()
        if database is not None:
            try:
                database.stop()
            except Exception:  # the cluster cleans itself up; nothing here is worth a false failure
                pass

    say()
    say(f"{'all checks passed' if failures == 0 else f'{failures} check(s) FAILED'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
