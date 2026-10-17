#!/usr/bin/env bash
# Restart loop for the StillHere server (BUILD_PLAN §6.1).
#
#   backend/scripts/serve.sh            # Ctrl-C stops the loop and the server
#
# `kill -9` the listener (scripts/chaos.sh) and this loop starts a fresh server
# about a second later; durable timers in Postgres fire from their stored deadlines.
# This file's name must not contain "uvicorn": `pkill -f uvicorn` matches full
# command lines and would kill the loop too. Kill only the listener:
#   kill -9 $(lsof -tiTCP:8000 -sTCP:LISTEN)
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
BACKEND="$(dirname "$HERE")"
REPO="$(dirname "$BACKEND")"

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in "$REPO/.venv/bin/python" "$REPO/.venv/Scripts/python.exe" "$BACKEND/.venv/bin/python"; do
    if [ -x "$cand" ]; then PY="$cand"; break; fi
  done
fi
if [ -z "$PY" ]; then
  echo "serve.sh: no virtualenv found; create one (python3.12 -m venv .venv) or set PYTHON=/path/to/python" >&2
  exit 2
fi

child=""
stop() {
  echo "serve.sh: stopping" >&2
  if [ -n "$child" ]; then kill "$child" 2>/dev/null; wait "$child" 2>/dev/null; fi
  exit 0
}
trap stop INT TERM

while true; do
  "$PY" "$HERE/run.py" &
  child=$!
  wait "$child"
  code=$?
  child=""
  echo "serve.sh: server exited with code $code; restarting in 1 s" >&2
  sleep 1
done
