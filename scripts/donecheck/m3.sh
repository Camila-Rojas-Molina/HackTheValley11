#!/usr/bin/env bash
# M3 DONE CHECK (PLAN 2, M3: the click path, this script plus eyes). Runs from the repo root on the integrated
# commit. Each step of the PLAN's click path gets the rows a script can check (PASS/FAIL) and, where only a
# person can judge, an EYES row naming what to look at. Exit 1 if any PASS/FAIL row fails.
#
#   scripts/donecheck/m3.sh          # checks, then leaves the DB, the API and the web app up for the eyes pass
#   scripts/donecheck/m3.sh --stop   # checks, then stops everything (scripts/dev.sh stop)
#
# The PLAN lines name http://localhost:3000 and :8000. dev.sh uses those ports when they are free and otherwise
# free ports (PLAN 3.4), so the URLs are read back with `scripts/dev.sh url web|api`.
set -u
cd "$(dirname "$0")/../.."
STOP=0
[ "${1:-}" = "--stop" ] && STOP=1
FAILS=0
PY=".venv/bin/python"
EYES=()

row() {  # status, check, observed
  printf '%-5s %-66s %s\n' "$1" "$2" "$3"
  [ "$1" = "FAIL" ] && FAILS=$((FAILS + 1))
  return 0
}
eyes() { EYES+=("$1"); printf '%-5s %s\n' "EYES" "$1"; }
one_line() { tr '\n' ' ' | sed 's/  */ /g' | cut -c1-160; }
http_code() { curl -s -o /dev/null -w '%{http_code}' --max-time "${2:-120}" "$1"; }
page() {  # path -> row for the web page answering 200 (the first load compiles it, hence the long timeout)
  local code; code="$(http_code "$WEB$1" 180)"
  [ "$code" = "200" ] && row PASS "GET $1 (web)  -> 200" "$code" || row FAIL "GET $1 (web)  -> 200" "$code"
}
manifest() {  # scan id, then one query: metric <key> <grid|*> <field> | registration | mock_assets
  local id="$1"; shift
  curl -s --max-time 15 "$API/api/v1/scans/$id" | "$PY" -c '
import json, sys
m, q = json.load(sys.stdin), sys.argv[1:]
if q[0] == "metric":
    hit = [x for x in m.get("metrics", []) if x.get("key") == q[1] and q[2] in ("*", x.get("grid"))]
    print(hit[0].get(q[3]) if hit else "")
elif q[0] == "registration":
    print((m.get("registration") or {}).get("status", ""))
elif q[0] == "mock_assets":
    print(" ".join(sorted(a.get("name", "") for a in m.get("assets", []) if ".mock." in a.get("name", ""))))' "$@" 2>/dev/null
}
if [ "$STOP" = 1 ]; then trap 'scripts/dev.sh stop >/dev/null 2>&1' EXIT; fi

# 1. scripts/dev.sh  -> the throwaway DB, the API and the web app, detached
out="$(scripts/dev.sh 2>&1)"
rc=$?
up="$(printf '%s\n' "$out" | grep -cE '^(db +rb-dev-|api +pid [0-9]+|web +pid [0-9]+)')"
if [ "$rc" = 0 ] && [ "$up" = 3 ]; then
  row PASS "scripts/dev.sh  -> db, api and web up, detached" "$(printf '%s\n' "$out" | grep -E '^(api|web) +pid' | awk '{print $1, $3, "port", $5}' | one_line)"
else
  row FAIL "scripts/dev.sh  -> db, api and web up, detached" "exit $rc"
  printf '%s\n' "$out" | tail -n 20 | sed 's/^/      /'
fi
# No fallback to :3000/:8000 when dev.sh failed: those ports may be another worktree's, a false PASS.
WEB="$(scripts/dev.sh url web 2>/dev/null || true)"
API="$(scripts/dev.sh url api 2>/dev/null || true)"

# 2. /scan/myel018-L1-d273: 2x2 view, red lesion outline in L1, lesion volume 3.24 cm3 (original scan grid),
#    stiffness tile "pending FE (M5)"
page "/scan/myel018-L1-d273"
v="$(manifest myel018-L1-d273 metric lesion_volume_cm3 original value)"
[ "$v" = "3.24" ] && row PASS "API d273 lesion_volume_cm3 (original grid)  -> 3.24" "$v" \
                  || row FAIL "API d273 lesion_volume_cm3 (original grid)  -> 3.24" "${v:-no value}"
v="$(manifest myel018-L1-d273 metric stiffness_change_axial_pct '*' available_in)"
[ "$v" = "M5" ] && row PASS "API d273 stiffness_change_axial_pct  -> pending, available_in M5" "$v" \
                || row FAIL "API d273 stiffness_change_axial_pct  -> pending, available_in M5" "${v:-no value}"
eyes "$WEB/scan/myel018-L1-d273: a 2x2 view with a red lesion outline in L1; the tile reads \"Lesion volume 3.24 cm³ (original scan grid)\"; the stiffness tile reads \"pending FE (M5)\""

# 3. Day 0: 0.00 cm3, no outline, labelled "not yet registered (M5)"
page "/scan/myel018-L1-d0"
v="$(manifest myel018-L1-d0 metric lesion_volume_cm3 original value)"
[ "$v" = "0.0" ] && row PASS "API d0 lesion_volume_cm3 (original grid)  -> 0" "$v" \
                 || row FAIL "API d0 lesion_volume_cm3 (original grid)  -> 0" "${v:-no value}"
v="$(manifest myel018-L1-d0 registration)"
[ "$v" = "not_registered" ] && row PASS "API d0 registration.status  -> not_registered" "$v" \
                            || row FAIL "API d0 registration.status  -> not_registered" "${v:-no value}"
eyes "the Day 0 panel shows 0.00 cm³ and no outline, labelled \"not yet registered (M5)\""

# 4. /scan/phantom: Turbo strain overlay with the legend "Compressive strain (µε) · MOCK"; a 3D cut-away
page "/scan/phantom"
v="$(manifest phantom mock_assets)"
case "$v" in
  *"strain_min_principal_ue.mock.nii.gz"*) row PASS "API phantom assets  -> strain_min_principal_ue.mock.nii.gz (MOCK)" "$v" ;;
  *) row FAIL "API phantom assets  -> strain_min_principal_ue.mock.nii.gz (MOCK)" "${v:-none}" ;;
esac
eyes "$WEB/scan/phantom: a Turbo strain overlay on the slices with the legend \"Compressive strain (µε) · MOCK\"; the 3D view shows a cut-away"

# 5. The summary card shows template text with a "template" badge
src="$(curl -s --max-time 15 "$API/api/v1/scans/myel018-L1-d273/summary" | grep -o '"source": *"[a-z]*"' | head -1)"
printf '%s' "$src" | grep -q '"source": *"template"' \
  && row PASS "API d273 summary  -> \"source\":\"template\"" "$src" \
  || row FAIL "API d273 summary  -> \"source\":\"template\"" "${src:-no source field}"
eyes "the summary card shows template text with a \"template\" badge"

# 7. node web/tests/smoke.mjs "$WEB"  -> 3 lines reading "status":"ready" with "errors":[]
#    (run before step 6, which stops the API)
if [ -f web/tests/smoke.mjs ]; then
  out="$(node web/tests/smoke.mjs "$WEB" 2>&1)"
  rc=$?
  ready="$(printf '%s\n' "$out" | grep -E '"status": *"ready"' | grep -cE '"errors": *\[\]')"
  if [ "$rc" = 0 ] && [ "$ready" = 3 ]; then
    row PASS "node web/tests/smoke.mjs \$WEB  -> 3 x status ready, errors []" "$ready lines"
  else
    row FAIL "node web/tests/smoke.mjs \$WEB  -> 3 x status ready, errors []" "exit $rc; $ready ready lines"
    printf '%s\n' "$out" | tail -n 20 | sed 's/^/      /'
  fi
else
  row FAIL "node web/tests/smoke.mjs \$WEB  -> 3 x status ready, errors []" "web/tests/smoke.mjs is missing"
fi

# 6. scripts/dev.sh stop-api, then reload: the same page, with an "offline · fixtures" badge
scripts/dev.sh stop-api >/dev/null 2>&1
code="$(http_code "$API/api/v1/health" 5)"
[ "$code" = "000" ] && row PASS "scripts/dev.sh stop-api  -> the API no longer answers" "$code" \
                    || row FAIL "scripts/dev.sh stop-api  -> the API no longer answers" "$code"
page "/scan/myel018-L1-d273"
eyes "after scripts/dev.sh stop-api, a reload of $WEB/scan/myel018-L1-d273 shows the same page with an \"offline · fixtures\" badge"

if [ "$STOP" = 1 ]; then
  scripts/dev.sh stop >/dev/null 2>&1
  echo
  echo "Stopped (--stop). For the eyes pass run scripts/donecheck/m3.sh without --stop."
else
  # Leave everything up for the eyes pass, the API included (step 6 is repeated by hand at the end).
  scripts/dev.sh api >/dev/null 2>&1 || row FAIL "scripts/dev.sh api  -> the API is back for the eyes pass" "restart failed"
  echo
  echo "Eyes pass, in order (the servers are up; scripts/dev.sh stop when done):"
  i=1
  for e in ${EYES[@]+"${EYES[@]}"}; do printf '  %d. %s\n' "$i" "$e"; i=$((i + 1)); done
fi

echo
[ "$FAILS" = 0 ] && echo "M3 DONE CHECK (scripted rows): PASS on $(git rev-parse --short HEAD); the EYES rows are for a person" \
                 || echo "M3 DONE CHECK (scripted rows): $FAILS FAIL on $(git rev-parse --short HEAD)"
[ "$FAILS" = 0 ]
