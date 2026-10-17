#!/usr/bin/env bash
# Land one push (PLAN 3.4): every step except the adversarial review (step 4) runs mechanically.
#
#   scripts/integrate.sh m1                 # steps 1-3 in this worktree: integration branch, squash-merge each
#                                           #   stream branch after its ownership check, full suite; then stop for review
#   scripts/integrate.sh m1 --land          # steps 5-7: one squash commit on a fresh branch from origin/main, tags
#                                           #   landed/S<N>/m1, time vs deadline, push HEAD:main (ff only), CI, donecheck
#   scripts/integrate.sh m1 --dry-run       # steps 1-3 and 5-6 in a temporary clone; never pushes, changes nothing here
#
# Options: --branches "rb/s0/m1 rb/s4-api/m1"  (default: every local rb/s<N>[-topic]/<m> branch, landing order
#          S0 > S4 > S5 > S2 > S3 > S1 > S6) · --base <ref> (default origin/main) · --message-file <file>
#          (landing commit message) · --bootstrap (dry run: build a fresh .venv instead of linking this one)
#          · --no-ci (--land: skip step 7, for a remote without GitHub Actions).
# Run steps 1-3 and --land from the integration worktree (rb-wt/integrate), with a clean tree. Integration
# branches are local only; nothing but main is ever pushed, and never with --force.
set -euo pipefail

if [ -z "${RB_INTEGRATE_COPY:-}" ]; then
  # Run from a private copy of the tools: checking out other branches below must not change this script mid-run.
  SRC_SCRIPTS="$(cd "$(dirname "$0")" && pwd)"
  TOOLS="$(mktemp -d "${TMPDIR:-/tmp}/rb-integrate-tools.XXXXXX")"
  cp "$SRC_SCRIPTS/integrate.sh" "$SRC_SCRIPTS/check_ownership.py" "$TOOLS/"
  cp "$SRC_SCRIPTS/../contracts/ownership.json" "$TOOLS/ownership.json"
  RB_INTEGRATE_COPY="$TOOLS" RB_INTEGRATE_SRC="$(cd "$SRC_SCRIPTS/.." && pwd)" exec bash "$TOOLS/integrate.sh" "$@"
fi
TOOLS="$RB_INTEGRATE_COPY"
SRC="$RB_INTEGRATE_SRC"
unset RB_INTEGRATE_COPY RB_INTEGRATE_SRC   # never leak into child processes (a nested integrate.sh must start fresh)
trap 'rm -rf "$TOOLS"' EXIT

M="${1:-}"
[ -n "$M" ] && [ "${M#-}" = "$M" ] || { sed -n '2,16p' "$0"; exit 2; }
M="$(printf '%s' "$M" | tr 'A-Z' 'a-z')"
shift
LAND=0; DRY=0; BRANCHES=""; BASE_OPT=""; MSG_FILE=""; BOOTSTRAP=0; NO_CI=0
while [ $# -gt 0 ]; do
  case "$1" in
    --land) LAND=1; shift ;;
    --dry-run) DRY=1; shift ;;
    --branches) BRANCHES="$2"; shift 2 ;;
    --base) BASE_OPT="$2"; shift 2 ;;
    --message-file) MSG_FILE="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; shift 2 ;;
    --bootstrap) BOOTSTRAP=1; shift ;;
    --no-ci) NO_CI=1; shift ;;
    *) echo "integrate.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done
INT="rb/integrate/$M"
LANDB="rb/land/$M"
PYBIN="${PYTHON:-}"
[ -z "$PYBIN" ] && [ -x "$SRC/.venv/bin/python" ] && PYBIN="$SRC/.venv/bin/python"
[ -z "$PYBIN" ] && PYBIN="$(command -v python3.12 || true)"
[ -n "$PYBIN" ] || { echo "integrate.sh: no python3.12 (run scripts/bootstrap.sh)" >&2; exit 2; }

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nintegrate.sh: STOP: %s\n' "$*" >&2; exit 1; }
g() { git -C "$WORK" "$@"; }

order_key() {  # landing order: contracts (S0) > S4 > S5 > S2 > S3 > S1 > S6 > anything else
  case "$1" in
    rb/s0[-/]*) echo 0 ;; rb/s4[-/]*) echo 1 ;; rb/s5[-/]*) echo 2 ;; rb/s2[-/]*) echo 3 ;;
    rb/s3[-/]*) echo 4 ;; rb/s1[-/]*) echo 5 ;; rb/s6[-/]*) echo 6 ;; *) echo 9 ;;
  esac
}
stream_of() {  # rb/s1-web/m1 -> S1 (the stream id of contracts/ownership.json)
  local b="${1#rb/}"; b="${b%%/*}"; b="${b%%-*}"; printf 'S%s\n' "${b#s}"
}

discover() {  # $1 = ref prefix (refs/heads/ or refs/remotes/origin/)
  git -C "$SRC" for-each-ref --format='%(refname)' "$1rb/" | sed "s#^$1##" \
    | { grep -E "^rb/s[0-9]+(-[A-Za-z0-9._-]+)?/$M\$" || true; } \
    | while read -r b; do echo "$(order_key "$b") $b"; done | sort -n | cut -d' ' -f2 | tr '\n' ' '
}

deadline() {
  "$PYBIN" -I - <<'PYEOF'
from datetime import datetime, timezone, timedelta
tz = timezone(timedelta(hours=-4), "EDT")
now = datetime.now(tz)
# Deadlines as verified on Devpost at 15:45 EDT (rules and dates pages), plus the team's own targets.
marks = [("Devpost initial entry (team target)", "2026-10-17T20:00"), ("Devpost initial submission (hard)", "2026-10-17T21:00"),
         ("code freeze", "2026-10-18T06:15"), ("final submit (team target)", "2026-10-18T07:30"),
         ("Devpost lock (hard)", "2026-10-18T08:00")]
nxt = [(n, datetime.fromisoformat(t).replace(tzinfo=tz)) for n, t in marks]
nxt = [(n, t) for n, t in nxt if t > now]
if nxt:
    n, t = nxt[0]
    m = int((t - now).total_seconds() // 60)
    print(f"time: {now:%a %H:%M} EDT; next deadline: {n} at {t:%a %H:%M} EDT, in {m // 60} h {m % 60:02d} min")
else:
    print(f"time: {now:%a %H:%M} EDT; every deadline has passed")
PYEOF
}

# ------------------------------------------------------------------ where to work
if [ "$DRY" = 1 ]; then
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/rb-integrate-dry.XXXXXX")/repo"
  say "DRY RUN in a temporary clone: $WORK (nothing here is pushed; $SRC is not changed)"
  git clone --quiet --no-checkout "$SRC" "$WORK"
  if [ -n "$BASE_OPT" ]; then BASE="$(git -C "$SRC" rev-parse --verify "$BASE_OPT^{commit}")"
  elif git -C "$SRC" rev-parse --verify -q "origin/main^{commit}" >/dev/null; then
    BASE="$(git -C "$SRC" rev-parse "origin/main^{commit}")"
  else BASE="$(git -C "$SRC" rev-parse "main^{commit}")"; fi
  g fetch --quiet "$SRC" "$BASE" 2>/dev/null || true   # the base may be a remote-tracking ref of $SRC
  g checkout --quiet --force --detach "$BASE"            # a throwaway clone: nothing here to keep
  [ -n "$BRANCHES" ] || BRANCHES="$(discover refs/heads/)"
  for b in $BRANCHES; do g branch --quiet --force "$b" "$(git -C "$SRC" rev-parse --verify "$b^{commit}")"; done
  if git -C "$SRC" rev-parse --verify -q "$INT^{commit}" >/dev/null; then
    g switch --quiet -c "$INT" "$(git -C "$SRC" rev-parse "$INT^{commit}")"
  else
    g switch --quiet -c "$INT" "$BASE"
  fi
  if [ "$BOOTSTRAP" = 1 ]; then :; elif [ -d "$SRC/.venv" ]; then ln -s "$SRC/.venv" "$WORK/.venv"; fi
else
  WORK="$(git rev-parse --show-toplevel)"
  [ -z "$(g status --porcelain --untracked-files=no)" ] || die "uncommitted changes in $WORK"
  g fetch --quiet origin
  BASE="$(g rev-parse --verify "${BASE_OPT:-origin/main}^{commit}")"
  [ -n "$BRANCHES" ] || BRANCHES="$(discover refs/heads/)"
fi
[ -n "${BRANCHES// /}" ] || die "no stream branches for $M (expected rb/s<N>[-topic]/$M)"

# ------------------------------------------------------------------ steps 1-3
steps_1_to_3() {
  say "1. integration branch $INT (base $(g rev-parse --short "$BASE"))"
  if [ "$DRY" = 0 ]; then
    if g rev-parse --verify -q "refs/heads/$INT" >/dev/null; then
      [ "$(g rev-parse --abbrev-ref HEAD)" = "$INT" ] || g switch --quiet "$INT"
    else
      g switch --quiet -c "$INT" "$BASE"
    fi
  fi
  g merge-base --is-ancestor "$BASE" HEAD || die "$INT does not contain the base: merge $BASE into it first"

  say "2. ownership check, then squash-merge, per stream: $BRANCHES"
  for b in $BRANCHES; do
    tip="$(g rev-parse "$b^{commit}")"
    last="$(g log --format='%(trailers:key=Rb-Branch,valueonly,separator=)|%(trailers:key=Rb-Tip,valueonly,separator=)' \
              "$BASE..HEAD" | awk -F'|' -v b="$b" '$1 == b { print $2; exit }')"
    if [ "$last" = "$tip" ]; then echo "$b: already integrated at ${tip:0:12}"; continue; fi
    "$PYBIN" -I "$TOOLS/check_ownership.py" --repo "$WORK" --ownership "$TOOLS/ownership.json" \
      --branch "$b" --base HEAD || die "$b touched paths outside its stream (fix the branch or the map)"
    if [ -z "$last" ]; then
      g merge --squash --quiet "$b" >/dev/null || die "conflict merging $b: an ownership bug (git -C $WORK status)"
      how="squash"
    else
      g merge-base --is-ancestor "$last" "$tip" || die "$b was rewritten since ${last:0:12}; rebuild $INT"
      g cherry-pick --no-commit "$last..$tip" >/dev/null || die "conflict applying $b since ${last:0:12}"
      how="delta since ${last:0:12}"
    fi
    if g diff --cached --quiet; then echo "$b: no changes"; continue; fi
    g commit --quiet -m "integrate $M: $b ($how)" -m "Rb-Branch: $b
Rb-Tip: $tip"
    echo "$b: integrated ${tip:0:12} ($how)"
  done

  say "3. full suite on the merged tree ($(g rev-parse --short HEAD))"
  if [ "$DRY" = 0 ] || [ "$BOOTSTRAP" = 1 ]; then
    if [ -x "$WORK/scripts/bootstrap.sh" ]; then "$WORK/scripts/bootstrap.sh"; fi
  fi
  [ -x "$WORK/scripts/test_all.sh" ] || die "no scripts/test_all.sh on the merged tree (land S0 first)"
  "$WORK/scripts/test_all.sh" all || die "suites failed: fix on the owning branch, then rerun scripts/integrate.sh $M"
}

# ------------------------------------------------------------------ steps 5-7
integrated() {  # "<branch> <tip>" per stream branch on $INT: the tip its latest integration commit recorded
  g log --reverse --format='%(trailers:key=Rb-Branch,valueonly,separator=) %(trailers:key=Rb-Tip,valueonly,separator=)' \
    "$BASE..$INT" | awk 'NF == 2 { tip[$1] = $2; if (!($1 in seen)) { seen[$1] = 1; order[++n] = $1 } }
                         END { for (i = 1; i <= n; i++) print order[i], tip[order[i]] }'
}

steps_5_to_7() {
  local state landed
  landed="$(integrated)"
  [ -n "$landed" ] || die "no integration commits on $INT (run steps 1-3 first)"
  state="$(g rev-parse --absolute-git-dir)/rb-test-state.json"
  "$PYBIN" -I - "$state" "$(g rev-parse --short=12 "$INT")" <<'PYEOF' || die "no green test_all.sh run recorded for the integration head"
import json, sys
s = json.load(open(sys.argv[1]))
ok = s["head"] == sys.argv[2] and s["args"] == ["all"] and s["rows"] and not any(r["status"] == "FAIL" for r in s["rows"])
raise SystemExit(0 if ok else 1)
PYEOF
  g merge-base --is-ancestor "$BASE" "$INT" || die "main moved: merge $BASE into $INT and rerun steps 1-3"

  say "5. one squash commit on $LANDB from $(g rev-parse --short "$BASE")"
  g rev-parse --verify -q "refs/heads/$LANDB" >/dev/null && die "$LANDB exists already"
  g switch --quiet -c "$LANDB" "$BASE"
  g merge --squash --quiet "$INT" >/dev/null
  local msg
  msg="$(mktemp)"
  if [ -n "$MSG_FILE" ]; then cat "$MSG_FILE" > "$msg"
  else
    if [ "$M" = "m1" ]; then echo "RainbowBone: remove StillHere code; add skeleton, contracts and architecture"
    else echo "RainbowBone $(echo "$M" | tr 'a-z' 'A-Z'): land the stream branches"; fi > "$msg"
    { echo; echo "Streams landed (squashed; each tip tagged locally as landed/S<N>/$M):"
      printf '%s\n' "$landed" | while read -r b tip; do echo "- $b @ ${tip:0:12}"; done
      [ "$M" = "m1" ] && { echo; echo "StillHere stays at tag stillhere-final (11e028b)."; }
      echo; echo "Look at: README.md, ARCHITECTURE.md, contracts/, scripts/test_all.sh all."; } >> "$msg"
  fi
  g commit --quiet -F "$msg"
  g diff --quiet "$INT" HEAD || die "landing tree differs from the tested integration tree"
  echo "landing commit $(g rev-parse --short HEAD): $(head -1 "$msg")"
  while read -r b tip; do   # the integrated tip, not the branch's current tip (streams keep committing)
    local t="landed/$(stream_of "$b")/$M"
    if g rev-parse --verify -q "refs/tags/$t" >/dev/null; then
      [ "$(g rev-parse "$t^{commit}")" = "$tip" ] || die "tag $t exists at another commit"
    else
      g tag "$t" "$tip"
    fi
    echo "tagged $t -> ${tip:0:7}"
  done <<< "$landed"

  say "6. push $(g rev-parse --short HEAD) to main (fast-forward only)"
  deadline
  g merge-base --is-ancestor "$BASE" HEAD || die "not a fast-forward of $BASE"
  if [ "$DRY" = 1 ]; then
    echo "DRY RUN: not pushed. Would run: git push origin HEAD:main ($(g rev-parse --short "$BASE")..$(g rev-parse --short HEAD))"
    return 0
  fi
  g fetch --quiet origin
  [ "$(g rev-parse origin/main)" = "$(g rev-parse "$BASE")" ] || die "main moved since $BASE: merge it into $INT, rerun steps 1-3"
  g push origin HEAD:main
  if [ "$NO_CI" = 1 ]; then
    say "7. CI watch skipped (--no-ci)"
  else
    say "7. CI on $(g rev-parse --short HEAD), then the DONE CHECK"
    local sha ids="" i
    sha="$(g rev-parse HEAD)"
    for i in $(seq 1 40); do
      ids="$(gh run list --commit "$sha" --json databaseId --jq '.[].databaseId' 2>/dev/null || true)"
      [ -n "$ids" ] && break
      sleep 3
    done
    [ -n "$ids" ] || die "no workflow run appeared for $sha within 120 s"
    for id in $ids; do gh run watch "$id" --exit-status >/dev/null || die "workflow run $id failed"; done
    echo "CI: every workflow run for $sha succeeded"
    if [ -x "$WORK/scripts/donecheck/$M.sh" ]; then "$WORK/scripts/donecheck/$M.sh"; fi
  fi

  say "8. each stream now rebases onto main (replays only commits made after the landing)"
  while read -r b tip; do
    echo "git rebase --onto origin/main landed/$(stream_of "$b")/$M $b   # nothing new? cut a fresh branch from origin/main"
  done <<< "$landed"
}

if [ "$DRY" = 1 ]; then
  steps_1_to_3
  say "4. review: not mechanical; skipped in a dry run"
  steps_5_to_7
  say "DRY RUN OK: $(g rev-parse --short HEAD) on $LANDB in $WORK"
elif [ "$LAND" = 1 ]; then
  steps_5_to_7
else
  steps_1_to_3
  say "4. review (not mechanical): git -C $WORK diff --stat $(g rev-parse --short "$BASE")...$INT"
  echo "Review that diff (code review plus a red-team pass). Fixes go to the owning"
  echo "branch; then rerun: scripts/integrate.sh $M. When green and reviewed: scripts/integrate.sh $M --land"
fi
