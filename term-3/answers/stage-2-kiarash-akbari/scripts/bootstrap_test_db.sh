#!/usr/bin/env bash
#
# bootstrap_test_db.sh -- prepare a disposable PostgreSQL database and run the
# tests that need a real server: the Olist integration file and the factory
# sink file. Neither runs without it, and a run that skipped everything is not
# a pass.
#
# What this script does:
#   * creates the database if it is missing, using an administrative role
#   * refuses to use a database that already holds olist/factory objects
#   * exports TEST_DATABASE_URL for the test process
#   * runs both PostgreSQL suites through tee and reports the real exit codes
#
# What it deliberately does not do:
#   * apply SQL. The tests own the schema; 001 and 009 are executed inside a
#     transaction each test rolls back. A bootstrap that created tables first
#     would make "the DDL works" depend on how this script was last run.
#   * drop or reset anything. There is no --reset here on purpose: dropping a
#     schema is the loader's job (`data-factory load-database --reset`), and a
#     test helper that can delete a schema is how a wrong -d argument becomes
#     data loss. To throw the database away, run dropdb yourself.
#   * write the password anywhere durable. It is read with `read -s` (no echo,
#     no history) into a 0600 libpq password file inside a private temp
#     directory that a trap deletes on exit, so the secret never lands in a
#     repository file, a log, or the environment of a long-lived process.
#
# Usage:
#   ./scripts/bootstrap_test_db.sh [-h host] [-p port] [-d dbname] [-U user]
#                                  [-A admin_role] [--force] [--run-only] [-n]
#
# Options:
#   -h HOST   server host (default: localhost)
#   -p PORT   server port (default: 5432)
#   -d NAME   database to use (default: olist_stage2_test)
#   -U USER   role that owns the database and runs the tests
#             (default: the current OS user)
#   -A ROLE   administrative role allowed to create the database
#             (default: postgres)
#   --force   run even if the database already has olist or factory objects
#   --run-only
#             do not create anything and do not prompt for an admin password;
#             the database must already exist
#   -n        dry run: print the commands, execute none of them
#   --help    print this header and exit
#
# Environment overrides:
#   PYTHON            interpreter to run unittest with (default:
#                     ../stage-1-kiarash-akbari/.venv/bin/python, else python3)
#   TEST_DATABASE_URL used as-is when set; otherwise built from the flags above
#   PGPASSWORD        honoured for every connection, and never re-exported here
#
set -Eeuo pipefail

HOST="localhost"
PORT="5432"
DBNAME="olist_stage2_test"
DBUSER="${USER:-$(id -un)}"
ADMIN_ROLE="postgres"
FORCE=0
RUN_ONLY=0
DRY=0

usage() {
    awk 'NR > 2 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

die() {
    echo "bootstrap: $1" >&2
    exit "${2:-1}"
}

run() {
    if [[ "$DRY" == 1 ]]; then
        printf ' +'; printf ' %q' "$@"; printf '\n'
        return 0
    fi
    "$@"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h) HOST="${2:?-h needs a host}"; shift 2 ;;
        -p) PORT="${2:?-p needs a port}"; shift 2 ;;
        -d) DBNAME="${2:?-d needs a database name}"; shift 2 ;;
        -U) DBUSER="${2:?-U needs a role}"; shift 2 ;;
        -A) ADMIN_ROLE="${2:?-A needs a role}"; shift 2 ;;
        -n) DRY=1; shift ;;
        --force) FORCE=1; shift ;;
        --run-only) RUN_ONLY=1; shift ;;
        --help) usage; exit 0 ;;
        *) die "unknown argument: $1 (try --help)" 2 ;;
    esac
done

STAGE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE1_DIR="$STAGE_DIR/../stage-1-kiarash-akbari"
SQL_DIR="$STAGE_DIR/sql"
TESTS_DIR="$STAGE_DIR/tests"
WORKDIR=""
PASSFILE=""

# shellcheck disable=SC2329  # invoked by the trap below, never by name
cleanup() {
    # Only the secret file is deleted here. The log directory is kept, because
    # a summary line saying "the run failed" is not evidence anyone can read.
    if [[ -n "$PASSFILE" && -f "$PASSFILE" ]]; then
        rm -f "$PASSFILE"
    fi
    if [[ -n "$WORKDIR" && -d "$WORKDIR" && -z "${KEEP_LOGS:-}" ]]; then
        rm -rf "$WORKDIR"
    fi
    return 0
}
trap cleanup EXIT INT TERM

for tool in psql createdb; do
    command -v "$tool" >/dev/null 2>&1 || die \
        "$tool is not on PATH. Install the PostgreSQL client tools: the tests below cannot run against a server that has no way to be reached." 127
done

if [[ -z "${PYTHON:-}" ]]; then
    if [[ -x "$STAGE1_DIR/.venv/bin/python" ]]; then
        PYTHON="$STAGE1_DIR/.venv/bin/python"
    else
        PYTHON="python3"
    fi
fi
command -v "$PYTHON" >/dev/null 2>&1 || [[ -x "$PYTHON" ]] \
    || die "python interpreter not found: $PYTHON (set PYTHON=/path/to/python)"

# The stage-2 requirements file lists sqlalchemy, but the sink tests import the
# stage-1 package itself. Pointing PYTHONPATH at its src tree means the tests
# exercise the code under review, not whatever copy happens to be installed.
if [[ -d "$STAGE1_DIR/src/data_factory" ]]; then
    export PYTHONPATH="$STAGE1_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
fi

WORKDIR="$(mktemp -d 2>/dev/null || mktemp -d -t factory-sink)"
chmod 700 "$WORKDIR"

# A password file, never a password in an argument: libpq reads PGPASSFILE and
# every tool inherits it, while the value stays out of ps output, shell history
# and the logs written below.
if [[ -z "${PGPASSWORD:-}" && ! -f "${HOME}/.pgpass" && "$DRY" == 0 && "$RUN_ONLY" == 0 ]]; then
    if [[ ! -t 0 ]]; then
        die "no PGPASSWORD, no ~/.pgpass, and stdin is not a terminal. Export PGPASSWORD in this shell, or pass --run-only if the database is already reachable." 2
    fi
    printf 'PostgreSQL password for %s (input hidden): ' "$ADMIN_ROLE"
    read -r -s SECRET
    printf '\n'
    if [[ -n "$SECRET" ]]; then
        PASSFILE="$WORKDIR/pgpass"
        printf '%s:%s:*:%s:%s\n' "$HOST" "$PORT" "$DBUSER" "$SECRET" >"$PASSFILE"
        chmod 600 "$PASSFILE"
        export PGPASSFILE="$PASSFILE"
        unset SECRET
        echo "bootstrap: password is in a temporary file that is deleted on exit"
    fi
fi

db_exists() {
    if [[ "$DRY" == 1 ]]; then
        return 1
    fi
    local found
    found="$(psql -h "$HOST" -p "$PORT" -U "$ADMIN_ROLE" -d postgres -X -q -t -A \
        -c "SELECT 1 FROM pg_database WHERE datname = '$DBNAME'")"
    [[ "$found" == "1" ]]
}

if ! db_exists; then
    if [[ "$RUN_ONLY" == 1 ]]; then
        die "database '$DBNAME' is not reachable and --run-only forbids creating it" 3
    fi
    echo "bootstrap: creating database '$DBNAME' owned by '$DBUSER'"
    run createdb -h "$HOST" -p "$PORT" -U "$ADMIN_ROLE" -O "$DBUSER" "$DBNAME" \
        || die "createdb failed; check that $ADMIN_ROLE may create databases" 4
    run psql -h "$HOST" -p "$PORT" -U "$ADMIN_ROLE" -d postgres -X -q -v ON_ERROR_STOP=1 \
        -c "GRANT ALL ON SCHEMA public TO \"$DBUSER\"" \
        || echo "bootstrap: warning: GRANT on public failed; the role may already own it" >&2
else
    echo "bootstrap: database '$DBNAME' already exists; nothing was created"
fi

# The empty-database guard, the same rule the integration tests apply to
# themselves: leftover olist or factory objects make a green run meaningless,
# because both suites create the schemas they assert on.
if [[ "$RUN_ONLY" != 1 && "$FORCE" != 1 && "$DRY" != 1 ]]; then
    LEFTOVER="$(psql -h "$HOST" -p "$PORT" -U "$DBUSER" -d "$DBNAME" -X -q -t -A -c \
        "SELECT count(*) FROM pg_namespace
          WHERE nspname IN ('olist', 'factory')
             OR (nspname = 'public' AND EXISTS (
                    SELECT 1 FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'm')))")"
    if [[ "${LEFTOVER:-0}" != "0" ]]; then
        cat >&2 <<MESSAGE
bootstrap: refusing to run against a database that already holds objects.
  database: $DBNAME on $HOST:$PORT
  found:    $LEFTOVER schema group(s) that these tests expect to create themselves
Use a separate empty database (-d NAME), or --force if you are sure the objects
there are the ones these tests should re-use. This script never drops schemas.
MESSAGE
        exit 5
    fi
fi

if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
    export TEST_DATABASE_URL="postgresql+psycopg://$DBUSER@$HOST:$PORT/$DBNAME"
fi
echo "bootstrap: TEST_DATABASE_URL=$TEST_DATABASE_URL"
echo "bootstrap: the URL carries no password; PGPASSWORD or PGPASSFILE supplies it"

for file in 001_create_olist_schema.sql 009_factory_sink_schema.sql; do
    [[ -f "$SQL_DIR/$file" ]] || die "missing $SQL_DIR/$file" 6
done
for pattern in test_postgres_integration.py test_factory_sink_postgres.py; do
    [[ -f "$TESTS_DIR/$pattern" ]] || die "missing $TESTS_DIR/$pattern" 6
done

if [[ "$DRY" == 1 ]]; then
    echo "bootstrap: dry run stops here; nothing was created and no test was executed"
    exit 0
fi

RESULTS=()
STATUS_ALL=0
for pattern in test_postgres_integration.py test_factory_sink_postgres.py; do
    echo
    echo "bootstrap: running $pattern with $PYTHON (verbose output below)"
    # tee shows the run live and leaves a file behind; PIPESTATUS[0] is
    # unittest's exit code, not tee's, which is what the summary must report.
    set +e
    (cd "$STAGE_DIR" && "$PYTHON" -m unittest discover -s tests -p "$pattern" -v) 2>&1 \
        | tee "$WORKDIR/$pattern.log"
    status="${PIPESTATUS[0]}"
    set -e
    RESULTS+=("$pattern: exit $status")
    if [[ "$status" != "0" ]]; then
        STATUS_ALL="$status"
    fi
done

echo
echo "bootstrap: summary"
for line in "${RESULTS[@]}"; do
    echo "  $line"
done
echo "  logs: $WORKDIR (kept for you to read; delete it with: rm -rf '$WORKDIR')"
export KEEP_LOGS=1
if [[ "$STATUS_ALL" == "0" ]]; then
    echo "bootstrap: both suites exited 0 -- read the skip counts above before calling that a pass"
else
    echo "bootstrap: at least one suite failed (exit $STATUS_ALL)"
fi
exit "$STATUS_ALL"
