#!/usr/bin/env bash
# One look at every worktree of this repo (PLAN 3.6): last-commit age, uncommitted files, the status note and
# the last test_all.sh result. Run it every 15 minutes during the build.
#
#   scripts/watchdog.sh            # all worktrees
#   STALE_MIN=30 scripts/watchdog.sh
#
# A worktree whose last commit is older than STALE_MIN minutes (default 30) is marked STALE: restart its stream
# from its .rb-status.md, in the same worktree. Exit 0 always; it only reports.
set -u
STALE_MIN="${STALE_MIN:-30}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
NOW="$(date +%s)"
PY="${PYTHON:-}"
[ -z "$PY" ] && [ -x "$REPO/.venv/bin/python" ] && PY="$REPO/.venv/bin/python"
[ -z "$PY" ] && PY="$(command -v python3.12 || echo false)"
echo "watchdog $(date '+%a %H:%M %Z'): stale after ${STALE_MIN} min"

git -C "$REPO" worktree list --porcelain | awk '/^worktree /{print substr($0, 10)}' | while read -r wt; do
  [ -d "$wt" ] || { echo; echo "## $wt: MISSING (git worktree prune?)"; continue; }
  branch="$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  ts="$(git -C "$wt" log -1 --format=%ct 2>/dev/null || echo "$NOW")"
  age=$(( (NOW - ts) / 60 ))
  dirty="$(git -C "$wt" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
  flag=""
  case "$branch" in rb/s[0-9]*) [ "$age" -gt "$STALE_MIN" ] && flag="  STALE" ;; esac
  echo
  echo "## $(basename "$wt")  [$branch]  last commit ${age} min ago ($(git -C "$wt" log -1 --format='%h %s' | cut -c1-70)); ${dirty} uncommitted${flag}"
  if [ -f "$wt/.rb-status.md" ]; then
    # the first line, any "Updated" line, then the Next and Blockers sections (6 lines each)
    awk 'NR == 1 || /^Updated/ { print; next }
         /^## / { sec = tolower($0); n = 0; if (sec ~ /next|blocker/) print; next }
         sec ~ /next|blocker/ && NF && n < 6 { print; n++ }' "$wt/.rb-status.md" | sed 's/^/   | /'
  else
    echo "   | no .rb-status.md"
  fi
  state="$(git -C "$wt" rev-parse --absolute-git-dir 2>/dev/null)/rb-test-state.json"
  if [ -f "$state" ]; then
    "$PY" -I - "$state" <<'PYEOF' 2>/dev/null || echo "   tests: unreadable $state"
import json, sys
s = json.load(open(sys.argv[1]))
rows = s.get("rows", [])
c = {k: sum(r["status"] == k for r in rows) for k in ("PASS", "FAIL", "SKIP")}
fails = ", ".join(f"{r['suite']}({r['stream']})" for r in rows if r["status"] == "FAIL")
print(f"   tests: {s.get('at')} on {s.get('head')} [{' '.join(s.get('args', []))}]: "
      f"PASS {c['PASS']} FAIL {c['FAIL']} SKIP {c['SKIP']}" + (f"; failing: {fails}" if fails else ""))
PYEOF
  else
    echo "   tests: no test_all.sh run recorded"
  fi
done
exit 0
