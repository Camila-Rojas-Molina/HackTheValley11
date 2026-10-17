#!/usr/bin/env bash
# Local dev servers for this worktree, detached (PLAN 2: the DONE CHECK rules; PLAN 3.4: isolation).
#
#   scripts/dev.sh [all]       throwaway DB + API + web app
#   scripts/dev.sh api         throwaway DB + API
#   scripts/dev.sh web         web app only (pointed at this worktree's API when it runs)
#   scripts/dev.sh status      what runs here: PIDs, ports, URLs, logs
#   scripts/dev.sh url api|web the base URL of a running server (exit 1 if it is not running)
#   scripts/dev.sh stop        stop everything and remove the throwaway DB
#   scripts/dev.sh stop-api    stop the API only (the web app falls back to its fixtures)
#   scripts/dev.sh stop-db     stop and remove the DB only (the API keeps running and answers 503)
#
# A start prints the PIDs and ports and returns. It (re)starts the API and the web app and keeps a DB that
# already runs. Logs and state are in .rb-dev/ (gitignored).
#
# DB: a TimescaleDB container (the image the CI tsdb job uses), rb-dev-<hash of the state dir>, published on
# 127.0.0.1 only, on a free port, trust auth (local only, so no password exists). It is throwaway: stop and
# stop-db remove it. The API gets its URL explicitly, and explicit env wins over api/.env (python-dotenv does
# not override), so a dev run never migrates Tiger Cloud; after start-up dev.sh also checks that the API
# migrated this DB.
#
# Ports: API 8000 and web 3000 when free, else free ports, so two worktrees never collide. Overrides:
#   RB_API_PORT, RB_WEB_PORT   exact ports (fail if taken)
#   RB_DEV_STATE               state dir (default <worktree>/.rb-dev); the DB container name follows it
#   RB_DEV_DB_IMAGE            default timescale/timescaledb:latest-pg16
#   DOCKER                     default $HOME/.docker/bin/docker
#
# Env given to the API (api/app/config.py): RB_DATABASE_URL (and DATABASE_URL, which the pre-Push-2 API reads),
#   RB_AUTO_MIGRATE=1, RB_DEMO_SEED=1 (the API seeds the demo patient at start-up), RB_CORS_ORIGINS=<the web
#   origin>, HOST=127.0.0.1, PORT, and AI_CHAIN=template (Galit's gate: no live AI before G1). RB_ADMIN_KEY is
#   passed through only if you set it, so admin writes answer 503 by default.
# Env given to the web app: PORT, and RB_API_URL plus NEXT_PUBLIC_RB_API_URL when this worktree's API runs.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
D="${DOCKER:-$HOME/.docker/bin/docker}"
IMAGE="${RB_DEV_DB_IMAGE:-timescale/timescaledb:latest-pg16}"
DB_NAME="rb_dev"
API_RUN="$REPO/api/scripts/run.py"
NEXT_BIN="$REPO/web/node_modules/.bin/next"

say() { printf 'dev.sh: %s\n' "$*"; }
die() { printf 'dev.sh: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; }

PY=""
for cand in "$REPO/.venv/bin/python" "$(command -v python3.12 2>/dev/null || true)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then PY="$cand"; break; fi
done
[ -n "$PY" ] || die "no .venv here: run scripts/bootstrap.sh"

STATE="${RB_DEV_STATE:-$REPO/.rb-dev}"
mkdir -p "$STATE" || die "cannot create $STATE"
STATE="$(cd "$STATE" && pwd -P)"
HASH="$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:8])' "$STATE")"
DBC="rb-dev-$HASH"

port_in_use() { "$PY" -I "$REPO/scripts/rb_env.py" --port-in-use "$1"; }
pick_port() { "$PY" -I "$REPO/scripts/rb_env.py" --pick-port "$1"; }
get() { [ -f "$STATE/$1" ] && cat "$STATE/$1"; }
put() { printf '%s\n' "$2" > "$STATE/$1"; }

# --- one dev.sh at a time per state dir (a second runner may share the worktree) ----------------------------
LOCK="$STATE/.lock"
lock() {
  local i
  for i in $(seq 1 100); do
    if mkdir "$LOCK" 2>/dev/null; then
      echo $$ > "$LOCK/pid"
      trap 'rm -rf "$LOCK"' EXIT
      return 0
    fi
    local holder; holder="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then rm -rf "$LOCK"; continue; fi
    sleep 0.2
  done
  die "another dev.sh holds $LOCK"
}

# --- processes ---------------------------------------------------------------------------------------------
# A pid file is trusted only while that pid's command line contains this worktree's own entry point, so a
# stale file never makes dev.sh signal an unrelated process.
pattern_for() { case "$1" in api) printf '%s' "$API_RUN" ;; web) printf '%s' "$NEXT_BIN" ;; esac; }

running_pid() {  # name -> prints the pid and returns 0 if this worktree's server runs
  local pid; pid="$(get "$1.pid" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -ww -o command= -p "$pid" 2>/dev/null | grep -qF -- "$(pattern_for "$1")" || return 1
  printf '%s' "$pid"
}

stop_proc() {  # name
  local name="$1" pid pgid i
  if ! pid="$(running_pid "$name")"; then
    if [ -f "$STATE/$name.pid" ]; then
      say "$name: not running (stale pid file removed)"
    else
      say "$name: not running"
    fi
    rm -f "$STATE/$name.pid" "$STATE/$name.port" "$STATE/$name.web_port"
    return 0
  fi
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
  if [ "$pgid" = "$pid" ]; then kill -TERM -- "-$pid" 2>/dev/null; else kill -TERM "$pid" 2>/dev/null; fi
  for i in $(seq 1 100); do
    if [ "$pgid" = "$pid" ]; then kill -0 -- "-$pid" 2>/dev/null || break; else kill -0 "$pid" 2>/dev/null || break; fi
    sleep 0.1
  done
  if [ "$pgid" = "$pid" ]; then kill -KILL -- "-$pid" 2>/dev/null; else kill -KILL "$pid" 2>/dev/null; fi
  rm -f "$STATE/$name.pid" "$STATE/$name.port" "$STATE/$name.web_port"
  say "$name: stopped (pid $pid)"
}

spawn() {  # name dir command... ; detached in its own process group, output to $STATE/<name>.log
  local name="$1" dir="$2"; shift 2
  printf '\n=== %s %s\n' "$(date '+%F %T')" "$*" >> "$STATE/$name.log"
  set -m
  ( cd "$dir" && exec nohup "$@" ) >> "$STATE/$name.log" 2>&1 < /dev/null &
  local pid=$!
  set +m
  put "$name.pid" "$pid"
}

choose_port() {  # name override preferred -> port
  local name="$1" override="$2" preferred="$3"
  if [ -n "$override" ]; then
    case "$override" in *[!0-9]*) die "$name port '$override' is not a number" ;; esac
    port_in_use "$override" && die "$name port $override is taken (unset the override to pick a free one)"
    printf '%s' "$override"
  else
    pick_port "$preferred"
  fi
}

# --- database ----------------------------------------------------------------------------------------------
docker_ok() {
  [ -x "$D" ] || D="$(command -v docker 2>/dev/null || true)"
  [ -n "$D" ] && "$D" info >/dev/null 2>&1
}

db_running() { docker_ok && [ "$("$D" inspect -f '{{.State.Running}}' "$DBC" 2>/dev/null)" = "true" ] && [ -n "$(get db.port 2>/dev/null)" ]; }
db_url() { printf 'postgresql://postgres@127.0.0.1:%s/%s' "$(get db.port)" "$DB_NAME"; }
db_sql() { "$D" exec "$DBC" psql -h 127.0.0.1 -U postgres -d "$DB_NAME" -tAc "$1"; }

ensure_db() {
  docker_ok || die "Docker is not running (or not at $D): start Docker Desktop, then rerun"
  if db_running; then
    say "db: reusing $DBC on 127.0.0.1:$(get db.port)"
    return 0
  fi
  "$D" rm -f "$DBC" >/dev/null 2>&1
  local port i
  port="$("$PY" -I "$REPO/scripts/rb_env.py" --port)"
  "$D" run -d --name "$DBC" --label "rb.dev.state=$STATE" \
    -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB="$DB_NAME" \
    -p "127.0.0.1:$port:5432" "$IMAGE" > "$STATE/db.log" 2>&1 \
    || { cat "$STATE/db.log" >&2; die "db: docker run failed"; }
  put db.port "$port"
  for i in $(seq 1 180); do
    if "$D" exec "$DBC" pg_isready -q -h 127.0.0.1 -U postgres -d "$DB_NAME" 2>/dev/null \
       && db_sql 'SELECT 1' >/dev/null 2>&1; then
      say "db: $DBC ($IMAGE) ready on 127.0.0.1:$port"
      return 0
    fi
    sleep 0.5
  done
  "$D" logs --tail 30 "$DBC" >&2 2>&1
  die "db: not ready after 90 s"
}

stop_db() {
  if docker_ok && "$D" inspect "$DBC" >/dev/null 2>&1; then
    "$D" rm -f "$DBC" >/dev/null 2>&1 && say "db: $DBC removed"
  else
    say "db: not running"
  fi
  rm -f "$STATE/db.port"
}

# --- API ---------------------------------------------------------------------------------------------------
api_env() {  # port web_port
  local url; url="$(db_url)"
  local origins="http://localhost:$2,http://127.0.0.1:$2"
  printf '%s\n' "HOST=127.0.0.1" "PORT=$1" "LOG_LEVEL=info" \
    "RB_DATABASE_URL=$url" "DATABASE_URL=$url" "RB_AUTO_MIGRATE=1" "RB_DEMO_SEED=1" \
    "RB_CORS_ORIGINS=$origins" "AI_CHAIN=template"
}

start_api() {  # port web_port
  local port="$1" pid i code line env_lines=()
  while IFS= read -r line; do env_lines+=("$line"); done < <(api_env "$port" "$2")
  spawn api "$REPO/api" env "${env_lines[@]}" "$PY" "$API_RUN"
  put api.port "$port"
  put api.web_port "$2"
  pid="$(get api.pid)"
  for i in $(seq 1 240); do
    if ! kill -0 "$pid" 2>/dev/null; then
      tail -n 30 "$STATE/api.log" >&2
      rm -f "$STATE/api.pid" "$STATE/api.port"
      die "api: exited during start-up (log above; $STATE/api.log)"
    fi
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$port/api/v1/health" 2>/dev/null)"
    [ "$code" = "200" ] && break
    sleep 0.25
  done
  [ "$code" = "200" ] || { tail -n 30 "$STATE/api.log" >&2; stop_proc api >/dev/null; die "api: /api/v1/health is $code after 60 s (API stopped)"; }
  # Galit's gate: the schema goes only to this throwaway DB. If the repo has migrations and this DB has no
  # tables, the API read another database URL (or did not migrate): stop it rather than keep it running.
  if ls "$REPO"/api/migrations/*.sql >/dev/null 2>&1; then
    local n; n="$(db_sql "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'" 2>/dev/null | tr -d ' ')"
    if [ -z "$n" ] || [ "$n" = "0" ]; then
      stop_proc api
      die "api: this worktree's DB has no tables after start-up, so the API used another database URL or did not migrate (dev.sh passes RB_DATABASE_URL, DATABASE_URL and RB_AUTO_MIGRATE=1)"
    fi
  fi
}

# --- web ---------------------------------------------------------------------------------------------------
start_web() {  # port
  local port="$1" pid i env_lines
  env_lines=("PORT=$port" "NEXT_TELEMETRY_DISABLED=1")
  if [ ! -x "$NEXT_BIN" ]; then
    say "web: installing web/node_modules (npm ci)"
    (cd "$REPO/web" && npm ci --no-audit --no-fund) >> "$STATE/web.log" 2>&1 \
      || { tail -n 30 "$STATE/web.log" >&2; die "web: npm ci failed"; }
  fi
  if running_pid api >/dev/null; then
    env_lines+=("RB_API_URL=http://127.0.0.1:$(get api.port)" "NEXT_PUBLIC_RB_API_URL=http://127.0.0.1:$(get api.port)")
  else
    say "web: no API runs in this worktree, so the web app gets no RB_API_URL (it uses its fixtures)"
  fi
  spawn web "$REPO/web" env "${env_lines[@]}" "$NEXT_BIN" dev -p "$port" -H 127.0.0.1
  put web.port "$port"
  pid="$(get web.pid)"
  for i in $(seq 1 480); do
    if ! kill -0 "$pid" 2>/dev/null; then
      tail -n 30 "$STATE/web.log" >&2
      rm -f "$STATE/web.pid" "$STATE/web.port"
      die "web: exited during start-up (log above; $STATE/web.log)"
    fi
    port_in_use "$port" && return 0
    sleep 0.25
  done
  tail -n 30 "$STATE/web.log" >&2
  stop_proc web >/dev/null
  die "web: not listening on $port after 120 s (web stopped)"
}

# --- status ------------------------------------------------------------------------------------------------
status() {
  local pid
  if db_running; then
    printf '%-4s %-22s %-28s %s\n' db "$DBC" "127.0.0.1:$(get db.port)" "$IMAGE (throwaway)"
  else
    printf '%-4s %s\n' db "not running (container $DBC)"
  fi
  if pid="$(running_pid api)"; then
    printf '%-4s %-22s %-28s %s\n' api "pid $pid port $(get api.port)" "http://localhost:$(get api.port)" "log $STATE/api.log"
  else
    printf '%-4s %s\n' api "not running"
  fi
  if pid="$(running_pid web)"; then
    printf '%-4s %-22s %-28s %s\n' web "pid $pid port $(get web.port)" "http://localhost:$(get web.port)" "log $STATE/web.log"
  else
    printf '%-4s %s\n' web "not running"
  fi
}

web_port_for_api() {  # the web origin the API should allow
  local p
  if running_pid web >/dev/null; then get web.port; return; fi
  p="${RB_WEB_PORT:-}"
  [ -n "$p" ] && { printf '%s' "$p"; return; }
  printf '3000'
}

cmd="${1:-all}"
[ $# -le 2 ] || { usage >&2; exit 2; }
case "$cmd" in
  -h|--help|help) usage; exit 0 ;;
  all)
    lock
    [ -f "$API_RUN" ] || die "api: $API_RUN is missing"
    [ -f "$REPO/web/package.json" ] || die "web: web/package.json is missing"
    stop_proc web >/dev/null; stop_proc api >/dev/null
    apip="$(choose_port api "${RB_API_PORT:-}" 8000)" || exit 1
    webp="$(choose_port web "${RB_WEB_PORT:-}" 3000)" || exit 1
    [ "$apip" != "$webp" ] || die "the API and the web app cannot share port $apip"
    ensure_db
    start_api "$apip" "$webp"
    start_web "$webp"
    status ;;
  api)
    lock
    [ -f "$API_RUN" ] || die "api: $API_RUN is missing"
    stop_proc api >/dev/null
    apip="$(choose_port api "${RB_API_PORT:-}" 8000)" || exit 1
    ensure_db
    start_api "$apip" "$(web_port_for_api)"
    status ;;
  web)
    lock
    [ -f "$REPO/web/package.json" ] || die "web: web/package.json is missing"
    stop_proc web >/dev/null
    if running_pid api >/dev/null && [ -z "${RB_WEB_PORT:-}" ]; then pref="$(get api.web_port)"; else pref=3000; fi
    webp="$(choose_port web "${RB_WEB_PORT:-}" "${pref:-3000}")" || exit 1
    start_web "$webp"
    status ;;
  status) status ;;
  url)
    case "${2:-}" in
      api|web) running_pid "$2" >/dev/null || exit 1; printf 'http://localhost:%s\n' "$(get "$2.port")" ;;
      *) usage >&2; exit 2 ;;
    esac ;;
  stop) lock; stop_proc web; stop_proc api; stop_db ;;
  stop-api) lock; stop_proc api ;;
  stop-db) lock; stop_db ;;
  *) usage >&2; exit 2 ;;
esac
