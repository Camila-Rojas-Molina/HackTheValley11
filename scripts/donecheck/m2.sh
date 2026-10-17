#!/usr/bin/env bash
# M2 DONE CHECK (PLAN 2, M2). Runs from the repo root on the integrated commit; each PLAN line is one row with
# its expected value and PASS/FAIL, so nobody pastes commands by hand. Exit 1 if any check fails.
#
#   scripts/donecheck/m2.sh
#
# The PLAN lines name http://localhost:8000. dev.sh uses 8000 when it is free and otherwise a free port (PLAN
# 3.4), so the checks read the port back with `scripts/dev.sh url api`. The throwaway DB, the API and the web
# app of this worktree are stopped at the end (the PLAN's last line), also when a check fails.
set -u
cd "$(dirname "$0")/../.."
FAILS=0
PY=".venv/bin/python"

row() {  # status, check, observed
  printf '%-5s %-62s %s\n' "$1" "$2" "$3"
  [ "$1" = "FAIL" ] && FAILS=$((FAILS + 1))
  return 0
}
one_line() { tr '\n' ' ' | sed 's/  */ /g' | cut -c1-160; }
trap 'scripts/dev.sh stop >/dev/null 2>&1' EXIT

# 1. scripts/test_all.sh unit contract db tsdb  -> PASS for all four
out="$(scripts/test_all.sh unit contract db tsdb 2>&1)"
rc=$?
summary="$(printf '%s\n' "$out" | grep -E '^PASS [0-9]+  FAIL' | tail -1)"
bad=""
for s in unit contract db tsdb; do
  st="$(printf '%s\n' "$out" | awk -v n="$s" '$1 == n && $3 ~ /^(PASS|FAIL|SKIP)$/ {print $3}' | sort -u | tr '\n' ' ')"
  [ "$st" = "PASS " ] || bad="$bad $s:${st:-none}"
done
if [ "$rc" = 0 ] && [ -z "$bad" ]; then
  row PASS "scripts/test_all.sh unit contract db tsdb  -> all PASS" "$summary"
else
  row FAIL "scripts/test_all.sh unit contract db tsdb  -> all PASS" "exit $rc;$bad; $summary"
  printf '%s\n' "$out" | sed -n '/^suite /,/^PASS /p' | sed 's/^/      /'
fi

# 2. scripts/dev.sh api  -> detaches; prints PIDs and :PORT
out="$(scripts/dev.sh api 2>&1)"
rc=$?
api_line="$(printf '%s\n' "$out" | grep -E '^api +pid [0-9]+ port [0-9]+' | head -1)"
if [ "$rc" = 0 ] && [ -n "$api_line" ]; then
  row PASS "scripts/dev.sh api  -> detaches; prints PIDs and :PORT" "$(printf '%s' "$api_line" | awk '{print $1, $2, $3, $4, $5}')"
else
  row FAIL "scripts/dev.sh api  -> detaches; prints PIDs and :PORT" "exit $rc"
  printf '%s\n' "$out" | tail -n 20 | sed 's/^/      /'
fi
# No fallback to :8000 when dev.sh failed: that port may be another worktree's API, a false PASS.
API="$(scripts/dev.sh url api 2>/dev/null || true)"

# 3. curl -s "$API/api/v1/health"  -> {"status":"ok","db":"ok", ...}
body="$(curl -s --max-time 15 "$API/api/v1/health")"
if printf '%s' "$body" | grep -q '"status": *"ok"' && printf '%s' "$body" | grep -q '"db": *"ok"'; then
  row PASS "curl \$API/api/v1/health  -> {\"status\":\"ok\",\"db\":\"ok\", ...}" "$(printf '%s' "$body" | cut -c1-80)"
else
  row FAIL "curl \$API/api/v1/health  -> {\"status\":\"ok\",\"db\":\"ok\", ...}" "$(printf '%s' "$body" | one_line)"
fi

# 4. curl -s "$API/api/v1/tiger/info" | json.tool | grep -E '"name"|db_size'
#    -> the C3 hypertables and continuous aggregates by name, and db_size from pg_database_size()
lines="$(curl -s --max-time 30 "$API/api/v1/tiger/info" | "$PY" -m json.tool 2>&1 | grep -E '"name"|db_size')"
missing=""
for n in scan_metrics fe_element_results ai_generation scan_metrics_monthly fe_level_daily; do
  printf '%s\n' "$lines" | grep -q "\"name\": \"$n\"" || missing="$missing $n"
done
size="$(printf '%s\n' "$lines" | sed -n 's/.*"db_size_bytes": *\([0-9][0-9]*\).*/\1/p' | head -1)"
if [ -z "$missing" ] && [ -n "$size" ] && [ "$size" -gt 0 ]; then
  row PASS "tiger/info | grep '\"name\"|db_size'  -> 3 hypertables, 2 caggs, db_size" "db_size_bytes $size"
else
  row FAIL "tiger/info | grep '\"name\"|db_size'  -> 3 hypertables, 2 caggs, db_size" \
    "missing:${missing:- none}; db_size_bytes ${size:-not a positive number}"
fi
printf '%s\n' "$lines" | sed 's/^ */      /'

# 5. curl -s "$API/api/v1/scans/myel018-L1-d273/summary" | grep -o '"source": *"[a-z]*"'  -> "source":"template"
src="$(curl -s --max-time 15 "$API/api/v1/scans/myel018-L1-d273/summary" | grep -o '"source": *"[a-z]*"' | head -1)"
if printf '%s' "$src" | grep -q '"source": *"template"'; then
  row PASS "scans/myel018-L1-d273/summary  -> \"source\":\"template\"" "$src"
else
  row FAIL "scans/myel018-L1-d273/summary  -> \"source\":\"template\"" "${src:-no source field}"
fi

# 6. scripts/dev.sh stop-db; curl -s -o /dev/null -w '%{http_code}' "$API/api/v1/health"  -> 503
scripts/dev.sh stop-db >/dev/null 2>&1
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$API/api/v1/health")"
[ "$code" = "503" ] && row PASS "scripts/dev.sh stop-db; health  -> 503" "$code" \
                    || row FAIL "scripts/dev.sh stop-db; health  -> 503" "$code"

# 7. scripts/dev.sh stop  -> nothing of this worktree's left running
scripts/dev.sh stop >/dev/null 2>&1
rc=$?
left="$(scripts/dev.sh status 2>&1 | grep -v 'not running' | one_line)"
if [ "$rc" = 0 ] && [ -z "$left" ]; then
  row PASS "scripts/dev.sh stop  -> db, api and web stopped" "exit 0"
else
  row FAIL "scripts/dev.sh stop  -> db, api and web stopped" "exit $rc; still running: ${left:-none}"
fi

echo
[ "$FAILS" = 0 ] && echo "M2 DONE CHECK: PASS on $(git rev-parse --short HEAD)" || echo "M2 DONE CHECK: $FAILS FAIL on $(git rev-parse --short HEAD)"
[ "$FAILS" = 0 ]
