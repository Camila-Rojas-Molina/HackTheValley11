#!/usr/bin/env bash
# Create (or update) this worktree's own .venv and install the Python dependencies that exist here.
#
#   scripts/bootstrap.sh              # .venv + every requirements file present in this worktree
#   scripts/bootstrap.sh --only repo  # .venv + scripts/requirements-repo.txt only (what ci-core needs)
#
# Python: $RB_PYTHON, else ~/.local/bin/python3.12, else python3.12 on PATH (CI). Never the system python3,
# never --user. Each worktree has its own .venv, so parallel streams never share installed packages.
# Large data is not installed here: it is read from RB_DATA_DIR (outside the repo).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "bootstrap.sh: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

PY=""
for cand in "${RB_PYTHON:-}" "$HOME/.local/bin/python3.12" "$(command -v python3.12 2>/dev/null || true)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "bootstrap.sh: no python3.12 found (set RB_PYTHON or install ~/.local/bin/python3.12)" >&2
  exit 2
fi
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  echo "bootstrap.sh: $PY is not Python 3.12" >&2
  exit 2
fi

if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "bootstrap.sh: creating $REPO/.venv with $PY"
  "$PY" -m venv "$REPO/.venv"
fi
VPY="$REPO/.venv/bin/python"
"$VPY" -m pip install --quiet --disable-pip-version-check --upgrade pip

REQS=("scripts/requirements-repo.txt")
if [ "$ONLY" != "repo" ]; then
  [ -n "$ONLY" ] && { echo "bootstrap.sh: --only takes 'repo'" >&2; exit 2; }
  for f in api/requirements.txt api/requirements-ai.txt pipeline/requirements-*.txt; do
    [ -f "$REPO/$f" ] && REQS+=("$f")
  done
fi
ARGS=()
for f in "${REQS[@]}"; do ARGS+=(-r "$REPO/$f"); done
echo "bootstrap.sh: installing ${REQS[*]}"
"$VPY" -m pip install --quiet --disable-pip-version-check "${ARGS[@]}"
echo "bootstrap.sh: OK ($("$VPY" --version) at .venv/bin/python)"
