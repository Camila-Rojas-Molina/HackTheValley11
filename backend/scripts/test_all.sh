#!/usr/bin/env bash
# Run every test suite in order and print a pass/fail table.
#
#   backend/scripts/test_all.sh                  # all suites (about 12 minutes)
#   backend/scripts/test_all.sh unit property    # only the named suites, in this order
#   SKIP_DOCKER=1 backend/scripts/test_all.sh    # everything except the TimescaleDB container suites
#
# Suites (pytest markers from backend/pytest.ini):
#   wording  unit  property  contract  db  security  load  wiring  e2e  browser  chaos  tsdb  tsdb-db
# "tsdb" and "tsdb-db" start a TimescaleDB container (stillhere-tsdb-matrix on 127.0.0.1:55435, image
# timescale/timescaledb:latest-pg16), run the TimescaleDB tests and then the database suite against it,
# and remove the container afterwards. Without a working `docker` they are reported as SKIP.
#
# Needs the project venv (python3.12 -m venv .venv; .venv/bin/pip install -r backend/requirements.txt), or
# PYTHON=/path/to/python. No Telegram, Tiger Cloud or Vultr key is used or needed. Exit code 1 if a suite fails.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$(dirname "$HERE")"
REPO="$(dirname "$BACKEND")"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in "$REPO/.venv/bin/python" "$BACKEND/.venv/bin/python"; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "test_all.sh: no virtualenv found; create one (python3.12 -m venv .venv) or set PYTHON" >&2
  exit 2
fi

DOCKER="${DOCKER:-}"
if [ -z "$DOCKER" ]; then
  if command -v docker >/dev/null 2>&1; then DOCKER=docker
  elif [ -x "$HOME/.docker/bin/docker" ]; then DOCKER="$HOME/.docker/bin/docker"
  fi
fi
CONTAINER=stillhere-tsdb-matrix
TSDB_PORT="${TSDB_PORT:-55435}"
TSDB_IMAGE="${TSDB_IMAGE:-timescale/timescaledb:latest-pg16}"

LOGS="$(mktemp -d "${TMPDIR:-/tmp}/stillhere-tests.XXXXXX")"
ALL_SUITES="wording unit property contract db security load wiring e2e browser chaos tsdb tsdb-db"
SUITES="${*:-$ALL_SUITES}"

# name|marker expression|what it covers
suite_def() {
  case "$1" in
    wording)  echo 'wording|banned-wording lint (BUILD_PLAN 7.3)' ;;
    unit)     echo 'unit and not wording|state machine, rules, parsers, care rules (no database)' ;;
    property) echo 'property|Hypothesis: KAZE invariants over random event sequences' ;;
    contract) echo 'contract|every route: status codes, auth failures, response shapes' ;;
    db)       echo 'db and not slow and not property and not contract and not load|integration on plain PostgreSQL' ;;
    security) echo 'security and not slow|regression tests for every review finding' ;;
    load)     echo 'load|50 device events + 10 contact taps at once' ;;
    wiring)   echo 'wiring|the real app (scripts/run.py), every path end to end' ;;
    e2e)      echo 'e2e|scripts/simulate.py: all 7 KAZE scenarios + PCC seed' ;;
    browser)  echo 'browser|c.html and hr_bridge.html in Google Chrome' ;;
    chaos)    echo 'chaos|kill -9 mid-REMINDED, database outage, bridge buffering' ;;
    tsdb)     echo 'docker|migrations and time series on Docker TimescaleDB' ;;
    tsdb-db)  echo 'db and not slow|the database suite against Docker TimescaleDB' ;;
    *)        return 1 ;;
  esac
}

ROWS=()
FAILED=0
TSDB_URI=""

tsdb_up() {
  [ -n "$TSDB_URI" ] && return 0
  if [ -n "${SKIP_DOCKER:-}" ] || [ -z "$DOCKER" ] || ! "$DOCKER" info >/dev/null 2>&1; then
    return 1
  fi
  local pw
  pw="$("$PY" -c 'import secrets; print(secrets.token_hex(12))')"
  "$DOCKER" rm -f "$CONTAINER" >/dev/null 2>&1
  if ! "$DOCKER" run -d --name "$CONTAINER" -p "127.0.0.1:$TSDB_PORT:5432" -e POSTGRES_PASSWORD="$pw" \
       "$TSDB_IMAGE" >/dev/null; then
    return 1
  fi
  TSDB_URI="postgresql://postgres:$pw@127.0.0.1:$TSDB_PORT/postgres"
  # The image's init runs a socket-only server first; a TCP connection means the real one is up.
  if ! STILLHERE_WAIT_URI="$TSDB_URI" "$PY" - <<'PYEOF'
import os, time, psycopg
deadline = time.monotonic() + 90
while True:
    try:
        with psycopg.connect(os.environ["STILLHERE_WAIT_URI"], connect_timeout=3) as c:
            c.execute("SELECT 1")
        break
    except Exception:
        if time.monotonic() > deadline:
            raise SystemExit(1)
        time.sleep(1)
PYEOF
  then
    tsdb_down
    return 1
  fi
  return 0
}

tsdb_down() {
  if [ -n "$DOCKER" ] && "$DOCKER" ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
    "$DOCKER" rm -f "$CONTAINER" >/dev/null 2>&1 && echo "test_all.sh: removed container $CONTAINER"
  fi
  TSDB_URI=""
}
trap tsdb_down EXIT
trap 'echo "test_all.sh: interrupted" >&2; exit 130' INT TERM

run_suite() {
  local name="$1" def expr what log start rc counts status secs
  def="$(suite_def "$name")" || { echo "test_all.sh: unknown suite '$name' (one of: $ALL_SUITES)" >&2; exit 2; }
  expr="${def%%|*}"
  what="${def#*|}"
  log="$LOGS/$name.log"
  local -a env_extra=()
  if [ "$name" = tsdb ] || [ "$name" = tsdb-db ]; then
    if ! tsdb_up; then
      ROWS+=("$(printf '%-9s %-5s %-40s %6s  %s' "$name" SKIP "docker is not available" "-" "$what")")
      return
    fi
    if [ "$name" = tsdb ]; then env_extra=(STILLHERE_TSDB_URIS="$TSDB_URI"); else env_extra=(STILLHERE_TEST_PG_URI="$TSDB_URI"); fi
  fi
  echo "=== $name: $what"
  start=$(date +%s)
  (cd "$BACKEND" && env ${env_extra[@]+"${env_extra[@]}"} "$PY" -m pytest -q -p no:cacheprovider -m "$expr" -rfE) \
    >"$log" 2>&1
  rc=$?
  secs=$(( $(date +%s) - start ))
  counts="$(grep -E '^[0-9]+ (passed|failed|skipped|deselected|error)|^=+ .*(passed|failed|error|no tests ran).* =+$|^[0-9]+ (passed|failed).* in ' "$log" | tail -1 | sed -E 's/^=+ //; s/ =+$//; s/ in [0-9.]+s.*$//; s/, [0-9]+ deselected//')"
  case $rc in
    0) status=PASS ;;
    5) status=SKIP; counts="${counts:-no tests selected}" ;;
    *) status=FAIL; FAILED=1 ;;
  esac
  tail -3 "$log"
  ROWS+=("$(printf '%-9s %-5s %-40s %5ss  %s' "$name" "$status" "${counts:0:40}" "$secs" "$what")")
}

for s in $SUITES; do
  run_suite "$s"
  if [ "$s" = tsdb-db ] || { [ "$s" = tsdb ] && [[ " $SUITES " != *" tsdb-db "* ]]; }; then tsdb_down; fi
done
tsdb_down

echo
printf '%-9s %-5s %-40s %6s  %s\n' SUITE RESULT TESTS TIME COVERS
printf '%s\n' "------------------------------------------------------------------------------------------------------"
for r in "${ROWS[@]}"; do echo "$r"; done
echo
echo "logs: $LOGS"
if [ "$FAILED" -ne 0 ]; then
  echo "test_all.sh: FAIL"
  exit 1
fi
echo "test_all.sh: all suites passed (SKIP rows say why they did not run)"
