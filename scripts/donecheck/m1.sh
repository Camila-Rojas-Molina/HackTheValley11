#!/usr/bin/env bash
# M1 DONE CHECK (PLAN 2, M1). Runs from the repo root on the integrated commit; prints each check with its
# expected value and PASS/FAIL, so nobody pastes commands by hand. Exit 1 if any check fails.
#
#   scripts/donecheck/m1.sh            # all six checks
#   scripts/donecheck/m1.sh --local    # skip the GitHub Actions check (before the push), reported as SKIP
set -u
cd "$(dirname "$0")/../.."
LOCAL=0
[ "${1:-}" = "--local" ] && LOCAL=1
FAILS=0

row() {  # status, check, observed
  printf '%-5s %-52s %s\n' "$1" "$2" "$3"
  [ "$1" = "FAIL" ] && FAILS=$((FAILS + 1))
  return 0
}

n="$(git ls-files backend docs | wc -l | tr -d ' ')"
[ "$n" = "0" ] && row PASS "git ls-files backend docs | wc -l  -> 0" "$n" || row FAIL "git ls-files backend docs | wc -l  -> 0" "$n"

n="$(wc -l < README.md | tr -d ' ')"
[ "$n" -le 100 ] && row PASS "wc -l < README.md  -> 100 or fewer" "$n" || row FAIL "wc -l < README.md  -> 100 or fewer" "$n"

out="$(scripts/test_all.sh repo unit contract web 2>&1)"
rc=$?
summary="$(printf '%s\n' "$out" | grep -E '^PASS [0-9]+  FAIL' | tail -1)"
missing="$(printf '%s\n' "$out" | grep -E '^(repo|unit|contract|web) +- +FAIL' || true)"
if [ "$rc" = 0 ] && [ -z "$missing" ]; then
  row PASS "scripts/test_all.sh repo unit contract web  -> all PASS" "$summary"
else
  row FAIL "scripts/test_all.sh repo unit contract web  -> all PASS" "exit $rc; $summary"
  printf '%s\n' "$out" | sed -n '/^suite /,/^PASS /p' | sed 's/^/      /'
fi

git check-ignore -q web/lib/utils.ts
rc=$?
[ "$rc" = 1 ] && row PASS "git check-ignore -q web/lib/utils.ts; echo \$?  -> 1" "$rc" \
              || row FAIL "git check-ignore -q web/lib/utils.ts; echo \$?  -> 1" "$rc"

out="$(.venv/bin/python scripts/check_ownership.py --all 2>&1 | tail -1)"
case "$out" in
  "OK: 0 unowned, 0 overlapping paths"*) row PASS "check_ownership.py --all  -> OK: 0 unowned, 0 overlapping" "$out" ;;
  *) row FAIL "check_ownership.py --all  -> OK: 0 unowned, 0 overlapping" "$out" ;;
esac

if [ "$LOCAL" = 1 ]; then
  row SKIP "gh run list (HEAD)  -> every workflow completed success" "--local: before the push"
elif ! command -v gh >/dev/null 2>&1; then
  row FAIL "gh run list (HEAD)  -> every workflow completed success" "gh not on PATH"
else
  sha="$(git rev-parse HEAD)"
  runs="$(gh run list --commit "$sha" --json name,status,conclusion \
          --jq '.[] | "\(.name)=\(.status)/\(.conclusion)"' 2>&1 | sort | tr '\n' ' ')"
  if [ -z "$runs" ]; then
    row FAIL "gh run list (HEAD)  -> every workflow completed success" "no runs for $(git rev-parse --short HEAD)"
  elif printf '%s' "$runs" | tr ' ' '\n' | grep -v '^$' | grep -qv '=completed/success$'; then
    row FAIL "gh run list (HEAD)  -> every workflow completed success" "$runs"
  else
    row PASS "gh run list (HEAD)  -> every workflow completed success" "$runs"
  fi
fi

echo
[ "$FAILS" = 0 ] && echo "M1 DONE CHECK: PASS on $(git rev-parse --short HEAD)" || echo "M1 DONE CHECK: $FAILS FAIL on $(git rev-parse --short HEAD)"
[ "$FAILS" = 0 ]
