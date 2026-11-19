#!/usr/bin/env python3
"""Produce the log that patch 0016 ships with: the state the signal omits, fetched and joined.

0015 taught the parser the response's shape; 0016 gives it the one field the response does not carry.
IBGE's agregados answer names each municipality and does not say which state it is in -- the student's
own ``curl`` confirmed the locality object holds ``id``, ``nivel`` and ``nome`` and nothing else -- while
``005`` keys the join on ``<city>|<uf>`` because 163 city names recur in two states. The patch reads the
state from IBGE's administrative list (``api/v1/localidades/municipios``), municipality by municipality,
instead of assuming the leading two digits of a code mean anything.

Four things are recorded, in the order a reviewer needs them:

1. the offline suite on the plain interpreter -- the gate every patch in this set passes;
2. the same no-``uf`` body against the tree before the patch and this one. As in ``patch-0015-run.py``,
   the red half is a ``git archive`` of the previous commit's ``src/``, not an edited-out fix. The
   probe asks the parser it is pointed at whether it can take a reference at all, and says so on the
   page, because "the old code has no such parameter" is a fact worth printing rather than a
   ``TypeError`` worth squinting at;
3. the shipped CLI against a local server that answers two paths: the signal without its states, and the
   administrative list. The request count is the finding here -- two for that pair, and *one* when the
   response already names the states, which is what keeps "one request per run" true instead of merely
   restated;
4. ``--load`` on a disposable cluster, read back from the table: the join key that did not exist before
   this patch, as ``municipio um|ro``, beside the NULL that stays a NULL.

What is deliberately *not* measured: any claim about the live IBGE host from this machine. The two
reference files under ``docs/evidence/`` were fetched through the platform's HTTP route, which has a
path to ``servicodados.ibge.gov.br`` that this sandbox does not; the student's ``--fetch`` is the run
that closes checklist 12.5, and nothing here pre-empts its answer.

Run it from ``stage-4/``::

    python3 docs/evidence/patch-0016-run.py | tee docs/evidence/patch-0016-run.txt

``--rev`` names the pre-patch tree (default ``HEAD~1``, which is right after applying 0015 in the
mirror tree too). Section 4 needs ``pip install pgserver 'psycopg[binary]'``.
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
ROOT = STAGE.parent
EVIDENCE = STAGE / "docs" / "evidence"
MUNICIPALITIES = EVIDENCE / "ibge-localidades-municipios-sample.json"
#: the codes the fixture payloads use, which the four verbatim entries do not cover; authored, and
#: named as authored in the log's header rather than blended into the fetched sample
EXTRA_CODES = (
    {"id": 3550308, "nome": "Município Dois", "microrregiao": {"mesorregiao": {"UF": {"sigla": "SP"}}}},
    {"id": 3304557, "nome": "Município Três", "microrregiao": {"mesorregiao": {"UF": {"sigla": "RJ"}}}},
)
MIGRATIONS = (
    "001_capstone_schema.sql",
    "002_time_guard.sql",
    "003_feature_views.sql",
    "004_as_of_functions.sql",
    "005_external_signal.sql",
)
#: the tracked example, copied and decided at run time; ignored in ``stage-4/.gitignore``
TEMP_CONFIG = "patch-0016-evidence.toml"
#: the same file as seen from ``STAGE``, which is every child's cwd: an absolute path here would
#: put this machine's clone location into a log that is meant to be diffable on any other
CONFIG_REL = f"config/{TEMP_CONFIG}"

PROBE = '''"""The no-uf response, and the list that supplies what it omits -- against whichever parser is given.

The probe is deliberately dumb about which tree it is in: it asks the module whether the reference
exists, prints the answer, and calls the parser the only way that tree allows. That is what makes the
two runs comparable -- one file, one body, one reference, two checkouts.
"""
import json
import pathlib
import sys

from capstone import external

body = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
reference = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))

can = hasattr(external, "parse_uf_map") and "uf_by_code" in external.normalize.__code__.co_varnames
print(f"parser accepts a municipality reference: {can}")
if not can:
    try:
        external.normalize(body)
    except Exception as error:
        print(f"refused: {type(error).__name__}: {error}")
        raise SystemExit(1)
    print("unexpectedly accepted the body -- the refusal this patch replaces did not happen")
    raise SystemExit(2)

mapping = external.parse_uf_map(reference)[0]
try:
    rows = external.normalize(body, uf_by_code=mapping)
except Exception as error:  # a refusal is still a result worth reading, not a stack to hide
    print(f"refused: {type(error).__name__}: {error}")
    raise SystemExit(1)
print(f"{len(rows)} row(s), {len(mapping)} codes consulted")
for row in rows:
    print(f"  {row.ibge_code}  {row.city_state}  {row.period}  {row.value!r}")
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
TMPDIR = re.compile(r"/tmp/capstone-001[56]-[a-z0-9_-]+")
PORT = re.compile(r"127\.0\.0\.1:[0-9]+")


def say(text: str = "") -> None:
    print(text, flush=True)


def rule(title: str) -> None:
    say()
    say("-" * 72)
    say(title)
    say("-" * 72)


def scrub(lines: list[str]) -> list[str]:
    """Three per-run accidents, flattened, so two runs of this file compare byte for byte."""
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
    # the interpreter's own name, not the word "python3": this file is usually run from a venv, and
    # a log that renames the binary it used is a log nobody can reproduce from
    command = scrub([f"$ {(interpreter or Path(sys.executable).name)} {' '.join(script)}"])[0]
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
    # the detail is scrubbed too: it is usually a tail of the child's output, and a port or a scratch
    # path leaking into it is exactly what makes two honest runs of this file look like different ones
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
    return Path(found.stdout.strip()) if found.returncode == 0 else ROOT


REPO = repo_root()


def find_ddl() -> Path | None:
    """stage-2's Olist DDL, in either layout, searched one directory level deep."""
    name = "001_create_olist_schema.sql"
    for base in (STAGE.parent, REPO, ROOT):
        for pattern in (f"stage-2*/sql/{name}", f"*/stage-2*/sql/{name}"):
            for candidate in sorted(base.glob(pattern)):
                if candidate.is_file():
                    return candidate
    return None


def pre_patch_src(dest: Path, rev: str) -> Path | None:
    """The ``src/`` of ``rev``, whole -- the red half, with nothing edited out of the fixed tree."""
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


def payload_without_uf() -> list:
    """0015's recorded response with every ``uf`` removed -- the body the student actually receives.

    The names stay the scrubbed placeholders the fixture already holds ("Município Um" and friends);
    inventing prettier ones would make the log's rows untraceable to a file in the same directory.
    Note what the printed keys look like: ``_fold`` strips the accents, so the stored key is
    ``municipio um|ro`` -- which is the same normalisation ``005`` applies to the orders' city, and the
    reason the join works at all.
    """
    data = json.loads((EVIDENCE / "ibge-4714-614-sample.json").read_text(encoding="utf-8"))
    for element in data:
        for block in element.get("resultados", []):
            for record in block.get("series", []):
                record["localidade"].pop("uf", None)
    return data


def reference_body() -> list:
    """The fetched five entries plus the two the fixture needs -- the additions are labelled."""
    entries = json.loads(MUNICIPALITIES.read_text(encoding="utf-8"))
    return entries + list(EXTRA_CODES)


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
    """Two paths, one body each, and a count per path -- the request budget is the thing under test."""

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


def main(argv: list[str]) -> int:
    plain = shutil.which("python3") or sys.executable
    rev = argv[argv.index("--rev") + 1] if "--rev" in argv else "HEAD~1"
    failures = 0
    work = Path(tempfile.mkdtemp(prefix="capstone-0016-"))

    def drop_temp_config() -> None:
        (STAGE / "config" / TEMP_CONFIG).unlink(missing_ok=True)

    (work / "no-uf.json").write_text(
        json.dumps(payload_without_uf(), ensure_ascii=False), encoding="utf-8"
    )
    (work / "reference.json").write_text(
        json.dumps(reference_body(), ensure_ascii=False), encoding="utf-8"
    )
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
        label="the whole suite, with UfReferenceTests in it",
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

    rule("2. the no-uf body, against the tree before the patch and after it")
    scratch = work / "pre"
    scratch.mkdir()
    old_src = pre_patch_src(scratch, rev)
    if old_src is None:
        say(f"  not measured: `git archive {rev}` gave no src/capstone/external.py here; in the")
        say("  mirror tree the default (HEAD~1) is the tree with 0015 applied, which is what this is.")
    else:
        say("  pre-patch src: the previous commit's own `src/`, unpacked into a scratch directory")
        code_red, text_red = step(
            ["probe.py", str(work / "no-uf.json"), str(work / "reference.json")],
            env=dict(base_env, PYTHONPATH=str(old_src)),
            label="expected RED: this tree cannot consult a reference, so it refuses",
            tail=6,
            cwd=work,
        )
        failures += not check(
            "the pre-patch tree refuses for want of a uf",
            code_red != 0 and "has no name or uf" in text_red,
            f"exit {code_red}",
        )
        failures += not check("no traceback escapes", "Traceback (most recent call last)" not in text_red)
    code_green, text_green = step(
        ["probe.py", str(work / "no-uf.json"), str(work / "reference.json")],
        env=base_env,
        label="GREEN: the state arrives from the administrative list",
        tail=8,
        cwd=work,
    )
    failures += not check("this tree builds the rows", code_green == 0, f"exit {code_green}")
    failures += not check(
        "and the join key has a state in it",
        "municipio um|ro" in text_green,
        text_green.strip().splitlines()[-1] if text_green.strip() else "no rows printed",
    )

    rule("3. the shipped CLI: one signal request, and the reference spent only when it must be")
    bodies = {
        "sample": str(work / "no-uf.json"),
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
        config = example_config(temp, f"http://127.0.0.1:{port}/sample")
        cli_env = dict(
            base_env,
            PYTHONPATH=str(STAGE / "src"),
            CAPSTONE_IBGE_LOCALIDADES_URL=f"http://127.0.0.1:{port}/municipios",
        )
        Local.hits = []
        code, text = step(
            ["-m", "capstone", "external", "--fetch", "--config", CONFIG_REL],
            env=cli_env,
            label="the response has no states, so the list is asked for them",
            tail=8,
        )
        failures += not check("--fetch exits 0", code == 0, f"exit {code}")
        counted = next((line for line in text.splitlines() if line.startswith("fetched:")), "(no count)")
        failures += not check("it counted the rows", "fetched: 3 row(s)" in text, counted)
        failures += not check("it said the map was used", "uf map:" in text)
        failures += not check(
            "two requests, no more",
            Local.hits == ["sample", "municipios"],
            ", ".join(Local.hits) or "none",
        )

        config_with = example_config(temp, f"http://127.0.0.1:{port}/with-uf")
        Local.hits = []
        code, text = step(
            ["-m", "capstone", "external", "--fetch", "--config", CONFIG_REL],
            env=cli_env,
            label="the same endpoint answering *with* its states: no second request",
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

    rule("4. the load and the read-back, on a disposable server")
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
            say("  no `001_create_olist_schema.sql` beside this checkout, so `olist` cannot exist;")
            say("  the table and the upsert function below are still exercised.")
            scripts = [STAGE / "sql" / "001_capstone_schema.sql", STAGE / "sql" / "005_external_signal.sql"]
        for script in scripts:
            proc = subprocess.run(
                [str(PSQL), uri, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(script)],
                capture_output=True,
                text=True,
                check=False,
            )
            failures += not check(f"applied {script.name}", proc.returncode == 0, proc.stderr.strip()[:160])
        fetch_server = serve(served, bodies)
        live_env = dict(cli_env, CAPSTONE_DATABASE_URI=uri, XDG_RUNTIME_DIR=str(work))
        live_env["CAPSTONE_IBGE_LOCALIDADES_URL"] = (
            f"http://127.0.0.1:{fetch_server.server_address[1]}/municipios"
        )
        config_load = example_config(
            temp, f"http://127.0.0.1:{fetch_server.server_address[1]}/sample"
        )
        code, text = step(
            ["-m", "capstone", "external", "--load", "--config", CONFIG_REL],
            env=live_env,
            label="fetch, consult the list, store -- the whole item-12 path",
            tail=8,
        )
        failures += not check("--load exits 0", code == 0, f"exit {code}")
        failures += not check("it says the map was consulted", "uf map:" in text)
        keys = psql(uri, "SELECT city_state FROM capstone.external_municipio ORDER BY 1")
        rows = psql(uri, "SELECT count(*), count(value) FROM capstone.external_municipio")
        say(f"  rows / non-null values: {rows}")
        say("  the join keys now in the table: " + keys.replace("\n", ", "))
        failures += not check(
            "the state arrived through the reference, in the table itself",
            "municipio um|ro" in keys,
            keys.replace("\n", ", "),
        )
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
