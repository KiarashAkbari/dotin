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
