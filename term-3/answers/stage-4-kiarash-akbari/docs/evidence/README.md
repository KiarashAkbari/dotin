# phase-0 evidence: the empty materialized views

Four files, one defect, and the smallest amount of interpretation possible.

| file | what it is | who ran it |
| --- | --- | --- |
| `phase0-baseline.txt` | the baseline write-up: the student's two PostgreSQL 16 runs (P1/P2), the agent's reproduction, and what neither proves | student + agent |
| `phase0-red-run.txt` | raw output of the new tests **before** the fix: 5 failures, and a scenario where a second `--reset` leaves all four views at 0 rows | agent sandbox |
| `phase0-green-run.txt` | the same command **after** the fix: `OK`, and the same scenario leaves every view populated | agent sandbox |
| `phase0-run-red-green.py` | the harness that produced both logs, including the scenario replay | agent |
| `phase0-build-fixtures.py` | builds the nine one-row fixture CSVs by reflecting the real `olist` schema | agent |

## Later evidence (phases 1 and 2)

The table above is the phase-0 defect only. Everything after it, in the order a reader
should take it:

| file | what it is | who ran it |
| --- | --- | --- |
| `phase1-decision-counts.txt` | the three model decisions (D1 no approval stamp, D2 no review, D3 many reviews per order) measured record by record, and the mirror disagreement that had to be settled first | student + agent sandbox |
| `phase1-full-load.txt` | how the sandbox database was built (versioned DDL, repo loader, nine tables) and the count-by-count reconciliation to the phase-0 baseline | agent sandbox |
| `column_audit.csv` | the audit table itself: every column of the schema, when it becomes available, whether it exists at approval, its source, and the verdict | student, from agent witnesses |
| `column-audit-run.txt` | the numbers behind each verdict, printed by the queries `scripts/audit_columns.py` emits, plus the wiring check in `doctor` | agent sandbox |
| `fake-model-gap.txt` | the deliberate leak, measured: 0.7383 AUC on the allowed columns against 1.0000 once `review_score` joins them -- plus what the number does not license | agent sandbox |
| `leak-red-run.txt` / `leak-green-run.txt` | the anti-leak test refusing to pass when the guard is broken, and passing when it is not | agent sandbox |
| `leak-run-red-green.py` | the harness that produced both logs; it patches a scratch copy of `src/`, never the repository | agent |
| `sql-checksums.txt` | sha256 of every file in `stage-4/sql`, written by `scripts/write_sql_checksums.py` and compared by a test | agent |
| `sql-guards-red-run.txt` / `sql-guards-green-run.txt` | the phase-4 SQL contract refusing to pass on three forbidden files, and passing on the shipped ones | agent sandbox |
| `sql-guards-run-red-green.py` | that harness; unlike the leak harness it edits the real `sql/` directory and restores it, with the checksum manifest as the proof of the restore | agent |
| `postgres-smoke-run.txt` | ten tests against a real PostgreSQL, and the number that matters: with either broken `proargtypes` predicate spliced into `004` and this directory's checksum manifest regenerated to match, the offline gates stay green while the server refuses -- six failures and three. Scrubbed of elapsed times and headed with the *repository* and commit it ran at (`tree: <repo> @ HEAD <sha>`, because a bare sha of the mirror repo is a dead link in both trees), so a reader can re-run it and diff | agent sandbox |
| `postgres-smoke-run.py` | that harness: it starts a temp cluster with `pgserver`, runs the smoke module both ways (its own cluster, and `CAPSTONE_SMOKE_DATABASE_URI`), splices the two predicates in and out, and refuses to print a green when no server was available -- it says the server half was not measured and exits 0 | agent |
| `frame-performance-run.py` | the checklist 5.3 harness, and the only script here that measures a database you did not build: cold/warm timings over `--runs N`, `EXPLAIN (ANALYZE, BUFFERS)` in text and JSON, hottest nodes ranked by *self* time, temp-space spill read off the root's inclusive counters, and a verdict against `features.feature_query_ms`. It times `FRAME_TIMING_SQL` from `capstone.features` rather than its own copy of the query, and a test pins that. No `CAPSTONE_DATABASE_URI` is a sentence plus exit 0, not a traceback | agent |
| `frame-performance-synthetic.py` | fills an *empty* scratch `olist` with a production-sized corpus (default 100,000 orders) so a plan can be discussed at all: the fixture CSVs are nine rows each, and a duration measured on nine rows is a fact about the planner starting up. Refuses without `CAPSTONE_SMOKE_ALLOW_DESTROY=1`, refuses a target that already holds rows, never truncates or deletes, prints the counts it wrote, then `ANALYZE`s | agent |
| `frame-performance-external-demo.py` | the item 12 round trip on a scratch cluster: `capstone.refresh_external_municipio` for its own source, then twenty rows stored through `capstone.upsert_external_municipio`, then the match rate read back from `capstone.v_external_match_rate`. The values are invented and labelled so; what is real is that the join path works and that an empty table prints `0 of N` instead of failing | agent |
| `frame-performance-sandbox.py` | the driver that turns those three into one log where no server exists: a `pgserver` cluster, stage 2's DDL found by the same glob the smoke module uses (so it works under `term-3/answers/`), this stage's `sql/001..005`, the audit through the shipped loader, the corpus, the measurement, the external report before and after, and the live guards with the URI set. Without the dev extra it says the server half was not measured and exits 0 | agent |
| `patch-0014-run.py` | the gate log of the patch that refuses to traceback on a closed port: it runs the offline suite, then runs the six new tests twice -- once against a scratch copy of this tree with the `try`/`except` removed from `features.connect`, where four of six must fail and the driver's exception must escape, once against the tree as shipped -- then, if the dev extra is installed, a `pgserver` cluster through the real `audit --load` and `external --report`, and the two touched modules with the URI set. Section 3 says out loud which halves it could not measure | agent |
| `patch-0014-run.txt` | that script's output: `Ran 278 ... OK (skipped=15)` for this directory's own suite, `FAILED (failures=2, errors=2)` on the tree with the fix removed (`'sup3rs3cr3t' unexpectedly found`, `1260 not less than 700`), `Ran 6 ... OK` on the shipped tree, `external --report` exit 0 with `0 of 200` keys against a live socket and exit 67 with one sentence against a closed one, then `Ran 90 ... OK (skipped=1)` live. A header above the log states which sections are synthetic and which are measured | agent sandbox |
| `patch-0015-run.py` / `patch-0015-run.txt` | the gate log of the parser patch: section 1 this directory's own suite (`Ran 292 ... OK (skipped=15)`, fourteen cases more than the same tree measured before the patch), section 2 the recorded IBGE body run against the *previous commit's* `src/` -- which prints the `returned no 'result' key (top level: list)` refusal the live `--fetch` answered with -- and then against this tree (three rows, `3.04`), section 3 the shipped `external --fetch` against a local server with the request count (one) and the two derived refusals (`uf` removed, level `N7`, both exit 69), section 4 a real `--load` into a disposable cluster read back as 3 rows / 2 non-null / `3.04` numeric. Requires `pip install pgserver 'psycopg[binary]'` for section 4 only; without it the section says so and exits 0 | agent sandbox |
| `ibge-4714-614-sample.json` | the response transcription those two sections run on: top-level list, `resultados`/`series`/`localidade`, `nivel.id` as the level, `{"2022": "3.04"}` as the value. The codes, the year, the variable, the unit and the number are the student's measurement; the locality names are placeholders and the `uf` objects are fields their transcription did not carry -- labelled here as well as in `decisions.md` §۸, because a fixture that hides its own inventions is how an invention becomes evidence | student measurement, fields labelled |
| `ibge-4714-metadados.json` | the metadata of agregado 4714 as it answered on 2026-10-26: its name is «População Residente, Área territorial e Densidade demográfica» and it carries exactly three variables -- `93` population, `6318` area, `614` density. This is why no income variable was ever configured and why item 12's hypothesis is density: the number `93` in the SIDRA mirror means *population* here, not income. Retrieved through the platform's HTTP route, since this sandbox has no path to that host -- said out loud so nobody reads a proxy as a measurement | agent, through the platform HTTP route (this sandbox has no egress) |
| `ibge-localidades-municipios-sample.json` | the first five entries of `api/v1/localidades/municipios`, which is the list `0016` consults when a signal response names a municipality but does not say which state it is in: `1100015` → `microrregiao.mesorregiao.UF.sigla = "RO"`. `id`, `nome`, `microrregiao` and `regiao-imediata` are verbatim; the only edit is re-flowed whitespace, and that is written here rather than left to be assumed | agent, through the platform HTTP route |
| `patch-0016-run.py` / `patch-0016-run.txt` | the gate log of the reference patch: the offline suite, the no-`uf` body run against the previous commit's `src/` and against this one (the red half is a `git archive`, not an edited-out fix), the shipped CLI measured by request count -- two when a state is missing, one when the response already carries them -- and `--load` read back from a disposable cluster as `3` rows / `2` non-null with `…|ro`, `…|sp`, `…|rj` in the join key. After `git am`, run it as `PYTHONPATH=src:tests python3 docs/evidence/patch-0016-run.py --rev HEAD~1` so section 2 compares this tree against the one it replaced; with `pgserver` installed, section 4 also loads the rows on a real server, and without it the harness says so and exits 0 rather than pretending — *(این لاگ پیش از `0017` گرفته شده و کلیدهایش `municipio um|ro` و `sao paulo - sp|sp`‌اند: نام از رشتهٔ نمایشیِ پاسخ، نه از رکوردِ فهرست؛ لاگ به عنوانِ یک رکورد حاشیه‌نویسی می‌شود و دوباره تولید نمی‌شود)* | agent sandbox |
| `patch-0016-run-local.txt` | his run of `patch-0016-run.py`. What each part of that script can and cannot witness on his machine: the offline-suite section is his tree's own gate; the no-`uf` body section has no red half in this file, because the pre-`0016` capture he tried first printed 0 lines and he deleted it, and the harness says in its own text that asking a tree which already carries the fix for the behaviour of a tree that does not is not a measurement; the CLI section counts requests against the throwaway HTTP server the harness starts, not against the live IBGE host; and the `--load` section is the one that needs `pgserver`, so on his machine nothing in this file is a load measurement. 62 lines as he reported them. The sandbox's `patch-0016-run.txt` is a different machine's record of the same script; neither one rewrites the other | student (Kiarash), his machine; reported against the checkout at `29cf01c` |
| `ibge-localidades-three-codes.json` | the three municipality records that `0017` keys its cases on, read from IBGE's own list for those codes: `1100015` → `Alta Floresta D'Oeste`/`RO`, `3550308` → `São Paulo`/`SP`, `3304557` → `Rio de Janeiro`/`RJ`. The first is a verbatim entry of the bulk list; the other two came from `api/v1/localidades/municipios/<code>`, because writing the *name* of the municipality whose name is the subject of the patch from memory is the same mistake the patch fixes. Collected into an array because that is what `parse_uf_map` reads | agent, through the platform HTTP route |
| `patch-0017-run.py` / `patch-0017-run.txt` | the gate log of the key-format patch: the offline suite (`Ran 308` on this tree), the three codes above run through the previous commit's `src/` and through this one -- `sao paulo - sp|sp` there, `sao paulo|sp` here -- the shipped CLI with its request budget counted and a report line that names the list as the source of the *names*, and `--load` twice on a disposable cluster seeded with three `olist.customers` rows: `match rate: 0 of 3` through the old key, `3 of 3` with `key_match_rate = 1.0` through the new one. The customers rows are written by the harness and labelled as such; the project's own rate is what `--load`/`--report` on `olist_db` will say | agent sandbox |
| `patch-0017-student-run.txt` | the student's own run, transcribed from his report and the witness that closes item 12.5: `git am` clean at local commit `5daba61`, `Ran 308 … OK (skipped=14)` on his machine with section 4 skipped for want of `pgserver`, `fetched: 5570 row(s)` / `uf map: 5570 of 5571 municipality codes and their names` / `stored: 5570 row(s)` on `olist_phase0_test`, `match rate: 4014 of 4310 (city, uf) keys = 0.9313`, the three sample keys read back out of the table clean, and 296 unmatched keys recorded as a limitation with his three examples. Nothing in it was produced or recomputed in the sandbox, and it claims no lift: that is 12.6 | student (Kiarash), transcribed by the agent |
| `patch-0017-run-local.txt` | his run of the 0017 harness, and the part of it no sandbox log can stand in for: the section that puts the three municipality codes through the previous commit's `src/` and through his own is the key-format witness measured on his checkout, not on a cluster the agent seeded. Its first section is his suite; the CLI section carries the request budget and the `uf map:` line that names the administrative list as the source of the *names*; the last section is the `pgserver` one, and his machine has no `pgserver`, so the rate on his corpus is witnessed by `patch-0017-student-run.txt` and not by this file. 73 lines as he reported them | student (Kiarash), his machine; reported against the checkout at `29cf01c` |
| `frame-performance-student-run.txt` | the capture that closes 5.3, transcribed from the student's own run of `frame-performance-run.py` on `olist_phase0_test`: timed mean 488.6 ms over five runs against the 500 ms ceiling, a frame of 99,281 rows and 32 columns (29 features), the two Hash Joins at 173.1 and 166.3 ms of self time, a HashAggregate spilling over five batches, two tables never ANALYZEd, and `external --report` at 0 of 4310 keys with no claim. It says in its own first section that it is a transcription, and that the raw `*-real-run*.txt` wins if the two disagree | student run, transcribed |
| `frame-performance-real-run.txt` | the raw capture the ignore rule reserved a negation for — `!docs/evidence/frame-performance-real-run.txt` is in `.gitignore` exactly so this file could be tracked — produced by `python3 docs/evidence/frame-performance-run.py --runs 5` against `olist_phase0_test`. Which part of that script's output is the witness for what: the header block and the tree-state line say which checkout ran it; the `database:` / `source tables` lines say what the frame's inputs actually hold, which is the scale claim; the frame-shape line is the rows-and-features count (and its `SHORT of the minimum` branch is the failure mode); the timing block is the number checklist 5.3 is judged against; section `1. EXPLAIN (ANALYZE, BUFFERS)` is the plan behind that number, with the hottest nodes by *self* time; section `2. the same frame read whole` is what a person waits for; the `verdict` block is 5.3's answer, and the `statistics:` line is what makes the plan worth reading at all. 137 lines as he reported them. This is the file to believe where it disagrees with `frame-performance-student-run.txt`, which states in its own opening that it is a transcription and that the raw capture wins; nobody in the sandbox has diffed the two, and this index quotes no figure out of either one | student (Kiarash), `olist_phase0_test`; reported against the checkout at `29cf01c` |
| `frame-performance-dotin-run.txt` | the same procedure captured a second time, run from this tree after the port: every step exits 0, the seeder's refusal is in section 3, and the timed mean is 397.7 ms against the sibling capture's 366.5 ms -- 31.2 ms of drift from running one thing twice, which is why 5.3 is judged on your server and not on either file. Synthetic corpus, sandbox machine, `unittest`'s elapsed clause scrubbed | agent sandbox |
| `frame-performance-sandbox-run.txt` | its output: 97,500 frame rows, 32 columns, 29 features against the minimum of 10, a timed mean of 366.5 ms against the 500 ms ceiling, with this capture's five timed runs between 357.7 and 371.3 ms, a `Hash` node at 2 batches with a few thousand temp blocks, and the external table moving from `0 of 96744` to `20 of 96744` keys. Synthetic corpus, sandbox machine, `unittest`'s elapsed clause scrubbed -- the numbers describe a plan and a procedure, and 5.3 stays open until the same harness runs on the project's own PostgreSQL | agent sandbox |
| `decision-impact.txt` | the queries behind every number in `decisions.md`, including the two denominators that disagree (see its section 4c) | agent sandbox |

Two rules the later files add to the ones below. Sandbox timings are labelled «سندباکس»
and judge no ceiling in the checklist. And where a number was not produced by an execution
here, the file says «بررسی نشده» rather than estimating it -- the student's own database
figures are marked as his, never re-labelled as the agent's.

## Reading order

1. `phase0-baseline.txt` section A - what was measured on the real delivery database.
2. section B - the same shape reproduced here, plus B.3, the one thing the sandbox
   caught that code reading did not: the refresh has to be committed as well, or
   `lock_connection.close()` rolls it back and the view is empty again.
3. `phase0-red-run.txt` / `phase0-green-run.txt` - the raw logs behind both claims.

## Rules these files follow

* No number in here was typed by hand except the two fixture counts (`1` row per table)
  and the student's own figures, which are marked as his.
* Anything not executed is labelled NOT RUN. The factory sink path (`009` +
  `load_stage1_sink`) is NOT RUN in every log above, because both runs passed
  `--skip-factory-sink`.
* Nothing here is a performance statement. There is no timing in these files on purpose.

## Reproducing

From the repository root, with a **disposable** PostgreSQL database — on this delivery
that is `olist_phase0_test`, never `olist_db`, because `load-database --reset` drops the
whole `olist` schema:

```
createdb olist_phase0_test
cd stage-1
PYTHONPATH=src:tests \
TEST_DATABASE_URL=postgresql+psycopg://<user>@localhost:5432/olist_phase0_test \
    python3 -m unittest tests.test_load_database_snapshots
```

Two things the command does not say and the tests do:

* The driver comes from the URL. `+psycopg` is what the project's lock ships
  (`psycopg==3.3.6` in stage 2 and 3, `psycopg[binary]>=3.1` in `stage-1/pyproject.toml`);
  `+psycopg2` also works, and a `psycopg2-binary` on the path is an **environment
  deviation**, not a dependency of this repository.
* The database-backed tests need `stage-2*/sql` to exist exactly once beside the answers
  root. Zero or two matches is a failure with the counts in the message — not a skip —
  so an unusable layout cannot hide as "nothing to check".

The class skips with a reason when `TEST_DATABASE_URL` is unset, so the offline suite is
unaffected. The two scripts in this directory were written for the agent sandbox (they
hardcode `/tmp` paths and use `pgserver`); on a normal machine the test module above is
the whole procedure.

## What the student still needs to run

`phase0-red-run.txt` and `phase0-green-run.txt` are agent evidence. What is still open,
and where it is recorded:

1. **the test module on the student's own PostgreSQL** — done for `olist_phase0_test`
   (`Ran 7 tests OK` at the time of `8843439`; the module is 8 tests now, so re-run it).
   Open: running it here as well, so both checkouts show the same green.
2. **`load-database --reset --skip-factory-sink` on a disposable database**, confirming
   the four views come out populated (the check that P1 failed). `olist_db` is not the
   target of this step: `--reset` deletes it, and the standing rule for this project is
   that `--reset` runs only against a throwaway copy.
3. **P2 (the run *without* `--reset`, before the fix)**: consistent with `p2.log`, and
   recorded that way - student-reported, confirmed by a log kept off this repository, whose
   sha256 is written in `phase0-baseline.txt` section A.2 and in PHASE0_REPORT.md §3. The
   later "that run did not finish" account stays as an unreconciled counter-report, neither
   confirmed nor refuted. Dropping the file into this directory turns the transcription into a
   byte copy that the recorded hash can verify.
4. **raw logs** (`p1.log`, `p2.log`, the post-fix run): they stay on the author's disk by
   choice — `stage-4/.gitignore` ignores `docs/evidence/*-real-run*.txt` and `*.log`. The
   measured numbers live in the report and in `phase0-baseline.txt` instead; that is a
   documented decision, not a gap.
