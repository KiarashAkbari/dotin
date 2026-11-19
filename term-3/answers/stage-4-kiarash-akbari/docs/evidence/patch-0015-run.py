#!/usr/bin/env python3
"""Produce the log that patch 0015 ships with: the real IBGE shape, through the real code path.

Why this script exists and the offline suite is not enough. Patch 0015 changed a parser because the
endpoint disagreed with the documentation it had been written from: the body is a top-level **list**,
the indicator is ``variavel``, the year is a key *inside* ``serie``, and the number is a string. Four
things are recorded here, in the order a reviewer needs them:

1. the offline suite on the plain interpreter -- the gate every patch in this set passes;
2. the recorded body against **the tree as it was before the patch** and against the tree as shipped,
   same probe, same file, so the refusal the student saw is on the page beside the rows that replaced
   it. The red half is not a mutation of the new code: it is the previous commit's ``src/``, extracted
   with ``git archive``. A red built by editing the fix out of the fixed file proves only that the
   edit worked; this one proves the patch is what changed the answer;
3. the shipped CLI against a local one-request server that answers with that recorded body -- the
   fetch path with no dependency on an external host being reachable, and with the request count
   printed, because "one request per run" is a policy this module claims and had never counted;
4. the load and the read-back on a disposable server, which is the only place that shows a string
   from the response arriving as ``3.04`` rather than ``304`` inside ``capstone.external_municipio``.

What is deliberately *not* here: no call to ``servicodados.ibge.gov.br``. The sandbox this was run in
has no route to that host (four attempts at ``…/4714/metadados``: one HTTP 500, three nothing), so
any verdict about the live endpoint belongs to the student's own run and is recorded as one in
decisions.md item 8 -- not invented here. The response file this script serves is the transcription of
*their* measurement, with the fields they did not transcribe labelled in the file's own comment in
this script's header.

Run it from ``stage-4/``::

    python3 docs/evidence/patch-0015-run.py | tee docs/evidence/patch-0015-run.txt

Optional: ``--rev <git rev>`` names the pre-patch tree (default ``HEAD~1``; in this repository the
number that worked was the commit before the one being proved), and the fourth section needs
``pip install pgserver 'psycopg[binary]'``.
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


def repo_root() -> Path:
    """Where `git` has to be run, which is not the stage's parent in the mirror tree.

    The same file lives at ``stage-4/docs/evidence/`` here and at
    ``term-3/answers/stage-4-kiarash-akbari/docs/evidence/`` there; only the first has the repository
    two levels above the stage. A harness that guesses the root either fails quietly or archives the
    wrong tree, so it asks git.
    """
    found = subprocess.run(
        ["git", "-C", str(STAGE), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(found.stdout.strip()) if found.returncode == 0 else ROOT


REPO = repo_root()
SAMPLE = STAGE / "docs" / "evidence" / "ibge-4714-614-sample.json"
MIGRATIONS = (
    "001_capstone_schema.sql",
    "002_time_guard.sql",
    "003_feature_views.sql",
    "004_as_of_functions.sql",
    "005_external_signal.sql",
)
#: what the probe answers when the payload does not parse, so a red run is a sentence and not a stack
PROBE = '''"""One payload, one parser, one line per row -- the same probe against two trees."""
import json
import pathlib
import sys

from capstone import external

body = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
try:
    rows = external.normalize(json.loads(body))
except Exception as error:  # the refusal is the result being measured, not a crash
    print(f"refused: {type(error).__name__}: {error}")
    raise SystemExit(1)
print(f"{len(rows)} row(s)")
for row in rows:
    print(f"  {row.ibge_code}  {row.city_state}  {row.period}  {row.value!r}  {row.indicator}")
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
#: the two things in this log that cannot repeat and must not look like findings: the scratch
#: directory `git archive` unpacked into, and the ephemeral port the local server happened to get
TMPDIR = re.compile(r"/tmp/capstone-0015-[a-z0-9_-]+")
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
    command = f"$ {(interpreter or 'python3')} {' '.join(script)}"
    if len(command) > 96:  # the echo is part of the log, so it wraps like a shell would
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
    say(f"  check[{name}]: {'ok' if ok else 'FAILED'}{(' -- ' + detail) if detail else ''}")
    return ok


def psql(uri: str, statement: str) -> str:
    """One statement against the disposable server, answer only."""
    proc = subprocess.run(
        [str(PSQL), uri, "-v", "ON_ERROR_STOP=1", "-q", "-t", "-A", "-c", statement],
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout + proc.stderr).strip()


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False
    )


def pre_patch_src(dest: Path, rev: str) -> Path | None:
    """The ``src/`` of ``rev``, extracted whole -- the red half, with no edit to the fixed tree.

    Located by asking git which path in that commit ends in ``src/capstone/external.py``, because this
    script has to work in the mirror tree as well, where the stage lives under a answers/ prefix.
    """
    listed = git("ls-tree", "-r", "--name-only", rev, "--", ".")
    if listed.returncode:
        return None
    hits = [line for line in listed.stdout.split("\n") if line.endswith("src/capstone/external.py")]
    if len(hits) != 1:
        return None
    src_dir = str(Path(hits[0]).parent.parent)  # .../src
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


#: inside the stage's own ``config/`` on purpose: ``_load_external`` looks for ``decisions.md`` two
#: levels above the config file, and a copy in /tmp would be refused for a reason that has nothing to
#: do with what is being measured. It is ignored in ``stage-4/.gitignore`` so a crash cannot leave a
#: committable config behind, and the run below removes it in a ``finally``.
TEMP_CONFIG = "patch-0015-evidence.toml"


def find_ddl() -> Path | None:
    """stage-2's Olist DDL, in either layout: beside the stage there, beside the root here.

    Bounded globs, one directory level deep -- a harness that walks the repository looking for a file
    is a harness that behaves differently on a checkout with a home directory in it.
    """
    name = "001_create_olist_schema.sql"
    for base in (STAGE.parent, REPO, ROOT):
        for pattern in (f"stage-2*/sql/{name}", f"*/stage-2*/sql/{name}"):
            for candidate in sorted(base.glob(pattern)):
                if candidate.is_file():
                    return candidate
    return None


def example_config(dest: Path, url: str) -> Path:
    """The tracked example, with a decided ``[external]`` block bolted on -- never written back.

    Truncating at ``[external]`` rather than editing four lines: that section is the last one, and a
    rewrite that has to survive a re-flowed file is a harness that breaks on someone else's comment.
    """
    text = (STAGE / "config" / "capstone.example.toml").read_text(encoding="utf-8")
    head = text[: text.index("[external]")]
    dest.write_text(
        head
        + textwrap.dedent(
            f'''\
            [external]
            # the block a student's config/capstone.toml would carry, pointed at the local
            # one-request server this harness started -- see the docstring
            name = "ibge_agregados_municipio"
            url = "{url}"
            token_env = ""
            join_on = "city_state"
            '''
        ),
        encoding="utf-8",
    )
    return dest


class Counting(http.server.SimpleHTTPRequestHandler):
    """Answers with one recorded body, and keeps a count: the module claims one request per run.

    One file at a time, chosen by the harness, because the point of the second and third calls is
    that a *different* body is refused; a server that walked the directory would let a typo in the
    config read as a successful fetch of something else.
    """

    requests: list[str] = []
    body: str = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # the harness reports, not the server
        type(self).requests.append(fmt % args)

    def do_GET(self) -> None:  # noqa: N802 - http.server's own spelling
        if self.path.rstrip("/") != "/sample":
            self.send_error(404, "the harness serves one path")
            return
        body = Path(type(self).body).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(directory: Path, name: str) -> Any:
    """Bind on 127.0.0.1 at a free port and hand back the server; the path is always ``/sample``."""
    Counting.body = str(directory / name)
    Counting.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Counting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def variants(directory: Path) -> dict[str, Path]:
    """The recorded body and the two shapes that must be refused, written side by side.

    The refusals are *derived* from the recorded file rather than written out by hand, so the only
    difference between the accepted body and a refused one is the field under test -- which is what
    makes the exit code the finding.
    """
    payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
    out: dict[str, Path] = {}
    for name, mutate in (
        ("sample.json", lambda data: data),
        (
            "no-uf.json",
            lambda data: [
                {
                    **element,
                    "resultados": [
                        {
                            **block,
                            "series": [
                                {**record, "localidade": {
                                    key: value
                                    for key, value in record["localidade"].items()
                                    if key != "uf"
                                }}
                                for record in block["series"]
                            ],
                        }
                        for block in element["resultados"]
                    ],
                }
                for element in data
            ],
        ),
        (
            "wrong-level.json",
            lambda data: [
                {
                    **element,
                    "resultados": [
                        {
                            **block,
                            "series": [
                                {
                                    **record,
                                    "localidade": {
                                        **record["localidade"],
                                        "nivel": {"id": "N7"},
                                    },
                                }
                                for record in block["series"]
                            ],
                        }
                        for block in element["resultados"]
                    ],
                }
                for element in data
            ],
        ),
    ):
        path = directory / name
        path.write_text(json.dumps(mutate(payload), ensure_ascii=False), encoding="utf-8")
        out[name] = path
    return out


def main(argv: list[str]) -> int:
    plain = shutil.which("python3") or sys.executable
    rev = argv[argv.index("--rev") + 1] if "--rev" in argv else "HEAD~1"

    def drop_temp_config() -> None:
        (STAGE / "config" / TEMP_CONFIG).unlink(missing_ok=True)

    failures = 0
    work = Path(tempfile.mkdtemp(prefix="capstone-0015-"))

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
        label="the whole suite, with IbgeDataShapeTests in it",
        tail=8,
    )
    failures += not check("offline suite green", code == 0, f"exit {code}")
    tally = re.search(r"Ran (\d+) tests", text)
    verdict = re.search(r"^(OK\b.*|FAILED.*)$", text, re.M)
    say(f"  tally: {tally.group(1)} tests -- {verdict.group(1).strip()}" if tally and verdict else "  tally: unreadable")

    rule("2. the recorded response, against the tree before the patch and after it")
    (work / "probe.py").write_text(PROBE, encoding="utf-8")
    scratch = work / "pre"
    scratch.mkdir()
    old_src = pre_patch_src(scratch, rev)
    if old_src is None:
        say(
            f"  not measured: `git archive {rev}` gave no src/capstone/external.py, so the red half"
            " cannot be run here. In the mirror tree, pass --rev of the commit before the patch."
        )
    else:
        # the scratch path is random, so the log says which commit was extracted and where it sits
        # inside the scratch tree -- enough to reproduce the line, nothing that changes per run
        say(f"  pre-patch src: `{rev}`'s own `src/` tree, unpacked into a scratch directory")
        code_red, text_red = step(
            ["probe.py", str(SAMPLE)],
            env=dict(base_env, PYTHONPATH=str(old_src)),
            label="expected RED: this is the sentence the live --fetch answered with",
            tail=6,
            cwd=work,
        )
        failures += not check(
            "the pre-patch tree refuses the real body",
            code_red != 0 and "no 'result' key" in text_red,
            f"exit {code_red}",
        )
        failures += not check(
            "and it refuses with a sentence, not a traceback",
            "Traceback (most recent call last)" not in text_red,
        )
    code_green, text_green = step(
        ["probe.py", str(SAMPLE)],
        env=base_env,
        label="GREEN on the tree as shipped",
        tail=6,
        cwd=work,
    )
    failures += not check("the shipped parser reads it into rows", code_green == 0, f"exit {code_green}")
    density = next((line for line in text_green.splitlines() if "1100015" in line), "")
    failures += not check(
        "the dot-decimal string is 3.04 on its own row, not 304",
        " 3.04 " in density,
        density.strip() or "no row for the measured code",
    )

    rule("3. the shipped CLI, one request, a local server serving the recorded body")
    served = work / "http"
    served.mkdir()
    files = variants(served)
    server = serve(served, "sample.json")
    port = int(server.server_address[1])
    try:
        temp = STAGE / "config" / TEMP_CONFIG
        if temp.exists():
            say(f"  refusing to overwrite an existing {temp}; remove it and re-run")
            raise SystemExit(1)
        config = example_config(temp, f"http://127.0.0.1:{port}/sample")
        cli_env = dict(base_env, PYTHONPATH=str(STAGE / "src"))

        code, text = step(
            ["-m", "capstone", "external", "--config", str(config)],
            env=cli_env,
            label="status only: no request, no database",
            tail=8,
        )
        failures += not check("status exits 0", code == 0, f"exit {code}")
        failures += not check("it says the source is configured", "configured: True" in text)
        failures += not check("status asked the server for nothing", not Counting.requests)

        Counting.requests = []
        code, text = step(
            ["-m", "capstone", "external", "--fetch", "--config", str(config)],
            env=cli_env,
            label="the fetch path, end to end",
            tail=6,
        )
        failures += not check("--fetch exits 0", code == 0, f"exit {code}")
        failures += not check("it counted the rows", "fetched: 3 row(s)" in text, text.strip()[-120:])
        failures += not check(
            "exactly one request left the process",
            len(Counting.requests) == 1,
            f"{len(Counting.requests)} request(s)",
        )

        for name, expect in (
            ("no-uf.json", "name or uf"),
            ("wrong-level.json", "N7"),
        ):
            Counting.requests = []
            Counting.body = str(files[name])
            code, text = step(
                ["-m", "capstone", "external", "--fetch", "--config", str(config)],
                env=cli_env,
                label=f"the {name} variant, which must be refused",
                tail=6,
            )
            failures += not check(
                f"{name} exits 69 with its own sentence",
                code == 69 and expect in text and "Traceback" not in text,
                f"exit {code}",
            )
        Counting.body = str(files["sample.json"])
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

    try:
        database = pgserver.get_server((work / "pgdata").as_posix(), cleanup_mode="stop")
        uri = str(database.get_uri())
        ddl = find_ddl()
        scripts = ([ddl] if ddl else []) + [STAGE / "sql" / name for name in MIGRATIONS]
        if ddl is None:
            say(f"  no `001_create_olist_schema.sql` under {STAGE.parent.name}/ or the repo root, so")
            say("  `olist` and the views over it cannot be created; the table and the upsert function")
            say("  below are still exercised.")
            scripts = [STAGE / "sql" / "001_capstone_schema.sql", STAGE / "sql" / "005_external_signal.sql"]
        for script in scripts:
            proc = subprocess.run(
                [str(PSQL), uri, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(script)],
                capture_output=True,
                text=True,
                check=False,
            )
            failures += not check(
                f"applied {script.name}", proc.returncode == 0, proc.stderr.strip()[:160]
            )
        # a server of its own, because the one above is closed: the load fetches, so the transport
        # has to be alive at that moment, and reusing the section-3 socket would make the two
        # sections depend on each other's cleanup
        fetch_server = serve(served, "sample.json")
        config = example_config(
            STAGE / "config" / TEMP_CONFIG, f"http://127.0.0.1:{fetch_server.server_address[1]}/sample"
        )
        live_env = dict(cli_env, CAPSTONE_DATABASE_URI=uri, XDG_RUNTIME_DIR=str(work))
        code, text = step(
            ["-m", "capstone", "external", "--load", "--config", str(config)],
            env=live_env,
            label="fetch and store, through the function that owns this table",
            tail=8,
        )
        failures += not check("--load exits 0", code == 0, f"exit {code}")
        stored = psql(uri, "SELECT count(*) FROM capstone.external_municipio")
        non_null = psql(uri, "SELECT count(value) FROM capstone.external_municipio")
        density = psql(
            uri, "SELECT value FROM capstone.external_municipio WHERE ibge_code = '1100015'"
        )
        pivot = psql(
            uri, "SELECT indicator || '@' || period FROM capstone.external_municipio ORDER BY 1"
        )
        say(f"  rows stored: {stored}, non-null values: {non_null}")
        say(f"  the density, read back as numeric: {density}")
        say("  pivot keys: " + pivot.replace("\n", ", "))
        failures += not check("three rows stored", stored == "3", f"count {stored}")
        failures += not check(
            "the null stays a null, not a zero", non_null == "2", f"non-null {non_null}"
        )
        failures += not check("and 3.04 stored as 3.04", density in {"3.04", "3.0400"}, f"value {density}")
    except Exception as error:  # a harness that dies in the middle is worse than one that skips
        say(f"  not measured: {type(error).__name__}: {error}")
        failures += 1
    finally:
        drop_temp_config()
        try:
            fetch_server.shutdown()
            fetch_server.server_close()
        except NameError:  # the server was never started: this tree could not reach §4's setup
            pass
        try:
            database.stop()
        except Exception:  # the server cleans itself up; nothing here is worth a false failure
            pass

    say()
    say(f"{'all checks passed' if failures == 0 else f'{failures} check(s) FAILED'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
