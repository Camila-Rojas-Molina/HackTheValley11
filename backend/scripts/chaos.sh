#!/usr/bin/env bash
# Chaos helpers (BUILD_PLAN §6.9). Only the LISTENING process is killed:
# `lsof -tiTCP:<port>` without -sTCP:LISTEN would also return the Chrome tabs
# connected to the port (contact page, HR bridge) and kill them.
#
#   scripts/chaos.sh kill [port]           kill -9 the listener on the port (default 8000)
#   scripts/chaos.sh mid-reminded [port]   drive the demo session to REMINDED, kill -9 the listener,
#                                          wait for serve.sh to restart it, then check that the stored
#                                          deadline fired L1 within 2 s, exactly once
#
# mid-reminded needs serve.sh running and ADMIN_KEY set (the server's operator key).
# STILLHERE_URL overrides the base URL (default http://127.0.0.1:<port>).
set -euo pipefail

CMD="${1:-kill}"
PORT="${2:-${PORT:-8000}}"
URL="${STILLHERE_URL:-http://127.0.0.1:$PORT}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$(dirname "$HERE")")"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in "$REPO/.venv/bin/python" "$REPO/.venv/Scripts/python.exe"; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
  done
fi

kill_listener() {
  local pids
  pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN || true)"
  if [ -z "$pids" ]; then
    echo "chaos: nothing is listening on :$PORT" >&2
    return 1
  fi
  echo "chaos: kill -9 $(echo "$pids" | tr '\n' ' ')(listener on :$PORT)"
  # shellcheck disable=SC2086
  kill -9 $pids
}

api() {  # api METHOD PATH [JSON]
  curl -fsS -X "$1" "$URL$2" -H "X-Admin-Key: ${ADMIN_KEY:?set ADMIN_KEY}" -H "Content-Type: application/json" \
       ${3:+--data "$3"}
}

py() { "$PY" -c "$1"; }

session_state() {  # session_state SID
  api GET /v1/admin/overview | SID="$1" py 'import json,os,sys
d=json.load(sys.stdin)
print(next((s["state"] for s in d["sessions"] if s["id"]==os.environ["SID"]), "GONE"))'
}

wait_healthy() {  # wait_healthy SECONDS
  local deadline=$(( $(date +%s) + $1 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS -m 1 "$URL/v1/health" >/dev/null 2>&1; then return 0; fi
    sleep 0.2
  done
  return 1
}

mid_reminded() {
  [ -n "$PY" ] || { echo "chaos: no .venv python found; set PYTHON" >&2; exit 2; }
  wait_healthy 5 || { echo "chaos: $URL is not healthy; start scripts/serve.sh first" >&2; exit 2; }

  local sid st
  sid="$(api POST /v1/demo/reset '{}' | py 'import json,sys; print(json.load(sys.stdin)["armed"]["session_id"])')"
  st="$(api POST /v1/demo/checkin-now | py 'import json,sys; print(json.load(sys.stdin)["state"])')"
  [ "$st" = "PROMPTED" ] || { echo "chaos: check-in did not prompt ($st)" >&2; exit 1; }
  api POST /v1/demo/fast-forward >/dev/null   # PROMPT_TIMEOUT now -> REMINDED (labelled demo)
  for _ in $(seq 1 50); do
    [ "$(session_state "$sid")" = "REMINDED" ] && break
    sleep 0.2
  done
  [ "$(session_state "$sid")" = "REMINDED" ] || { echo "chaos: session did not reach REMINDED" >&2; exit 1; }

  local timer
  timer="$(api GET /v1/admin/timers | SID="$sid" py 'import json,os,sys
d=json.load(sys.stdin)
t=[t for t in d["timers"] if t["session_id"]==os.environ["SID"] and t["kind"]=="REMINDER_TIMEOUT"]
print(t[0]["id"], t[0]["in_s"], t[0]["fire_at"]) if t else print("")')"
  [ -n "$timer" ] || { echo "chaos: no pending REMINDER_TIMEOUT" >&2; exit 1; }
  local tid in_s fire_at
  read -r tid in_s fire_at <<<"$timer"
  echo "chaos: session $sid is REMINDED; REMINDER_TIMEOUT timer $tid due at $fire_at (in ${in_s} s)"

  kill_listener
  local killed_at; killed_at="$(date +%s)"
  sleep 0.5
  if curl -fsS -m 1 "$URL/v1/health" >/dev/null 2>&1; then
    echo "chaos: (server already back)"
  else
    echo "chaos: server is down; waiting for serve.sh to restart it"
  fi
  wait_healthy 60 || { echo "chaos: server did not come back within 60 s (is serve.sh running?)" >&2; exit 1; }
  echo "chaos: server back after $(( $(date +%s) - killed_at )) s"

  local result=""
  for _ in $(seq 1 150); do
    result="$(api GET '/v1/admin/timers?recent=100' | TID="$tid" py 'import json,os,sys
d=json.load(sys.stdin)
r=[t for t in d.get("recent_fired",[]) if t["id"]==int(os.environ["TID"])]
print(r[0]["fire_at"], r[0]["fired_at"], r[0]["lag_s"], r[0]["result"]) if r else print("")')"
    [ -n "$result" ] && break
    sleep 0.2
  done
  [ -n "$result" ] || { echo "chaos: FAIL timer $tid never fired after restart" >&2; exit 1; }
  local f_at fired_at lag res
  read -r f_at fired_at lag res <<<"$result"

  local state msgs
  state="$(session_state "$sid")"
  msgs="$(api GET "/v1/admin/outbox?session_id=$sid&limit=1000" | TID="$tid" py 'import json,os,sys
d=json.load(sys.stdin)
p="timer:"+os.environ["TID"]+"|"
rows=[o for o in d["outbox"] if o["dedupe_key"].startswith(p) and o["template"] in ("alert_no_response","alert_final")]
print(len(rows))')"
  echo "chaos: timer $tid fire_at=$f_at fired_at=$fired_at lag=${lag}s result=$res state=$state contact_messages_for_this_deadline=$msgs"
  # With an approved contact the deadline must land on L1 with exactly one alert message; with none,
  # the ladder is empty and it goes straight to FINAL (no message to send).
  if "$PY" -c "import sys; sys.exit(0 if 0 <= float('$lag') <= 2.0 else 1)" && [ "$res" = "escalated" ] \
     && { { [ "$state" = "ESCALATED_L1" ] && [ "$msgs" -eq 1 ]; } \
          || { [ "$state" = "ESCALATED_FINAL" ] && [ "$msgs" -eq 0 ]; }; }; then
    echo "chaos: PASS kill -9 mid-REMINDED: the stored deadline fired ${lag} s late after restart -> $state, $msgs message(s)"
  else
    echo "chaos: FAIL (want lag 0-2 s, result escalated, ESCALATED_L1 with exactly one message)" >&2
    exit 1
  fi
}

case "$CMD" in
  kill) kill_listener ;;
  mid-reminded) mid_reminded ;;
  *) echo "usage: $0 kill|mid-reminded [port]" >&2; exit 2 ;;
esac
