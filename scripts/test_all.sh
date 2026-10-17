#!/usr/bin/env bash
# Run the registered test suites (suites/*.toml, one file per stream) and print a PASS/FAIL/SKIP table.
#
#   scripts/test_all.sh all                       # every registered suite
#   scripts/test_all.sh repo unit contract web    # every registration of these suite names, in this order
#   scripts/test_all.sh --list                    # what is registered, by stream
#   scripts/test_all.sh --stream S0 repo contract # only S0's registrations of these names (ci-core)
#
# Run it from any directory: commands in suites/*.toml run from the repo root. Exit 1 if a suite fails or a
# requested suite name is not registered; every SKIP prints its reason. The format of suites/*.toml is in
# scripts/test_all.py. Needs the worktree's .venv (scripts/bootstrap.sh), or PYTHON=/path/to/python3.12.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then PY="$REPO/.venv/bin/python"
  elif command -v python3.12 >/dev/null 2>&1; then PY="$(command -v python3.12)"
  fi
fi
if [ -z "$PY" ]; then
  echo "test_all.sh: no .venv here; run scripts/bootstrap.sh (or set PYTHON)" >&2
  exit 2
fi
exec "$PY" -I "$REPO/scripts/test_all.py" "$@"
