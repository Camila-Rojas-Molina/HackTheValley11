# StillHere

StillHere is a smart scrunchie, worn on the wrist or in the hair: a check-in reaches the wearer as a silent Apple Watch tap (a Telegram "Reminders" message) and she answers with a squeeze, while a silent duress squeeze looks exactly like a cancel.
If she stops answering, a server-side countdown escalates through trusted contacts she approved, until one of them takes responsibility; the last step to emergency services is simulated and never dialled.
Sensors and heart rate never raise an alarm on their own: they can only bring the next check-in forward, and no language model takes part in any safety decision.

Built for Hack the Valley 11 (KAZE challenge). Backend only: FastAPI + PostgreSQL/TimescaleDB.

- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Tests](#tests)
- [simulate.py](#simulatepy)
- [Demo steps](#demo-steps)
- [10-minute checklist: Telegram, Tiger Cloud, Vultr](#10-minute-checklist-telegram-tiger-cloud-vultr)
- [Deploy on a Vultr Compute VM](#deploy-on-a-vultr-compute-vm)
- [Demonstrated / simulated](#demonstrated--simulated)
- [Tiger Data](#tiger-data)
- [PointClickCare senior mode](#pointclickcare-senior-mode)

API contract for firmware and frontends: [docs/API.md](docs/API.md). Backend internals, timing profiles and
the safety-rule table with the tests behind each rule: [backend/README.md](backend/README.md).

## Architecture

```
 scrunchie (squeeze sensor, accelerometer)        Apple Watch -> HeartCast (iPhone) -> BLE Heart Rate 0x180D
          | USB serial, 115200 baud, line protocol                                 |
          v                                                                        v
 backend/scripts/bridge.py ---- HTTP ---->  FastAPI  backend/app  <---- /hr-bridge in Chrome (Web Bluetooth)
                                            |  routes/   device, hr, sessions, contacts, demo, health,
                                            |            telemetry (Tiger Data), care (senior mode)
                                            |  states.py  pure state machine (no I/O)
                                            |  engine.py  applies one decision in one transaction
                                            |  workers in the same process:
                                            |    timer_worker | outbox_sender | telegram_poller
                                            v
                         PostgreSQL / TimescaleDB  (Tiger Cloud, or the bundled container)
                         timers: rows claimed FOR UPDATE SKIP LOCKED (survive kill -9)
                         outbox: at-least-once, dedupe_key | audit_log: append-only, hash-chained
                                            |
                 +--------------------------+---------------------------+
                 v                                                      v
   Telegram "Reminders" bot (long polling)                   /c/{token} contact and judge page
   wearer nudges -> mirrored to the watch as a tap           polls every second; on an alert the
   contact alerts with I'm on it / pass it on / she's safe   screen turns red and the phone rings

 Vultr: a Compute VM runs docker compose (api + Caddy for HTTPS). Serverless Inference only words the
 PointClickCare care-team briefing (backend/app/vultr.py); it is never on the escalation path.
```

States: `ARMED -> PROMPTED -> REMINDED -> ESCALATED_L1 -> L2 -> ... -> ESCALATED_FINAL`, with `ACKED`
(a contact said "I'm on it"), `RESOLVED` and `FALSE_ALARM`. `LOST_CONTACT` is a separate link state with its
own wording. Rules enforced in code and covered by tests:

- Sensor and heart-rate input only outputs `PULL_FORWARD` (an early check-in with a counted challenge); it never reaches `ESCALATED_*`.
- Only a contact's "she's safe" or the wearer's real PIN closes an alert. A late device squeeze never stands an escalation down.
- Real-PIN and duress-PIN replies are byte-identical (HTTP body, device command, wearer message); duress is masked as `ARMED`.
- Contacts must be approved by the wearer and see nothing before an alert.
- FINAL shows "Emergency services: SIMULATED" and never calls anyone.
- Timers are database rows, so a `kill -9` mid-countdown still fires on time after restart. The outbox is at-least-once with a dedupe key.

Heart rate is pluggable: `POST /v1/hr` takes samples from any source (HeartCast through `/hr-bridge`, a
streaming service, an Apple Health Shortcut, the simulator).

## Quick start

### A. Local: `.venv` + pgserver (no Docker, no system Postgres)

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt

# A private Postgres from pip. It keeps running after this exits; prints a DATABASE_URL.
.venv/bin/python - <<'EOF'
from pathlib import Path
import pgserver, psycopg
srv = pgserver.get_server(str(Path(".pgdata").resolve()), cleanup_mode=None)
with psycopg.connect(srv.get_uri(), autocommit=True) as c:
    if not c.execute("SELECT 1 FROM pg_database WHERE datname = 'stillhere'").fetchone():
        c.execute("CREATE DATABASE stillhere")
print(srv.get_uri("stillhere"))
EOF

cp backend/.env.example backend/.env
# In backend/.env: DATABASE_URL=<the printed URL>, then set ADMIN_KEY, DEVICE_KEY, WEARER_KEY,
# DEMO_PIN and DEMO_DURESS_PIN to values of your own. Never commit backend/.env.
backend/scripts/serve.sh                      # restart loop; or: cd backend && ../.venv/bin/python scripts/run.py
curl -s http://localhost:8000/v1/health
```

pgserver has no TimescaleDB extension; the KAZE core needs plain Postgres only. On start the app applies
`backend/migrations/*.sql`, seeds the demo wearer and device, and starts its three workers.

### B. Docker Compose (backend + TimescaleDB + migrate step)

```bash
docker compose up --build                     # http://127.0.0.1:8000/v1/health
docker compose exec db psql -U postgres stillhere
docker compose down -v                        # stop and delete the local database
```

- `db` is `timescale/timescaledb:latest-pg16`. `migrate` runs `scripts/migrate.py` once. `api` starts after `migrate` exits 0.
- `backend/.env` is read if it exists (Telegram, keys, PINs, Vultr). Inside the containers, `DATABASE_URL`
  is the bundled database unless you pass `--env-file backend/.env` with a cloud `DATABASE_URL` (see the Vultr
  section). `localhost` never works there: inside a container it is the container itself.
- The API is published on `127.0.0.1` only. Set `API_BIND=0.0.0.0` for the LAN, or `API_PORT` for another port.
- Phones need HTTPS from outside: run `cloudflared tunnel --url http://localhost:8000` and put its URL in
  `PUBLIC_BASE_URL`, or deploy on the VM below.

## Tests

```bash
cd backend && ../.venv/bin/python -m pytest -q
```

- Unit tests need nothing.
- Integration tests start a private Postgres with pgserver in `../.pgdata-stillhere`, next to the repo.
  Set `STILLHERE_PGDATA=/path` for another data directory, or `STILLHERE_TEST_PG_URI=postgresql://...` for
  another server, such as the TimescaleDB container.
- `tests/test_chaos_kill9.py` starts a real server, `kill -9`s it mid-REMINDED, restarts it and checks that
  L1 fires once, within 2 s of the stored deadline.
- `tests/test_pages_js.py` runs the pages' JavaScript in Node; it is skipped without Node.
- `tests/test_wording.py` greps every shipped file, this README and `docs/` for banned wording.
- `tests/test_vultr.py` checks:
  - the Vultr client against a mock transport (no network);
  - that no escalation module imports it;
  - that `docs/API.md` lists every route.
- `tests/test_timeseries.py` runs on whichever path the test database took (plain or TimescaleDB).
  `tests/test_timeseries_tsdb.py` needs TimescaleDB servers and is skipped without them:
  `STILLHERE_TSDB_URIS=postgresql://postgres:<pw>@127.0.0.1:55433/postgres` (comma-separated for several).
- `tests/test_care_*.py`: the senior-mode rules, wording, isolation from KAZE, the HTTP routes and the
  Vultr-worded briefing (mocked).

## simulate.py

Runs the seven KAZE scenarios over HTTP with a fake device and fake web contacts, no hardware:

1. safe
2. no response
3. duress
4. lost contact
5. unacknowledged
6. false alarm
7. privacy

It prints PASS/FAIL per scenario and exits 1 on any failure (about 4 minutes in demo timing). It changes the
demo wearer's session and contacts and refuses to run while the demo wearer has approved contacts it did not
create, so give it its own server and database:

```bash
# Second database "stillhere_sim" (same pgserver snippet as above, another name), then:
PORT=8001 DATABASE_URL='<stillhere_sim URL>' backend/scripts/serve.sh &
STILLHERE_URL=http://127.0.0.1:8001 .venv/bin/python backend/scripts/simulate.py        # all seven
STILLHERE_URL=http://127.0.0.1:8001 .venv/bin/python backend/scripts/simulate.py 2 5    # a subset
```

Both read `ADMIN_KEY`, `DEVICE_KEY`, `DEMO_DEVICE_ID`, `WEARER_KEY`, `DEMO_PIN` and `DEMO_DURESS_PIN` from
`backend/.env`.

## Demo steps

Set `URL` and `ADMIN_KEY` in your shell first. Every operator action below is labelled as demo in the
timeline.

```bash
op() { local body='{}'; [ -n "${2:-}" ] && body=$2; curl -s -X POST "$URL$1" -H "X-Admin-Key: $ADMIN_KEY" -H 'Content-Type: application/json' -d "$body"; echo; }
```

**Before (T-10 min, and between expo runs)**

1. `op /v1/demo/reset`. This gives a fresh armed demo session.
2. On the wearer's devices:
   - Apple Watch: automatic fall calling off (Watch app › Emergency SOS) and "Hold Side Button to Dial" off.
   - iPhone: Emergency SOS "Call with Hold and Release" and "Call with 5 Presses" off, on every team phone.
   - Fitness / Workout Focus off.
3. Heart rate:
   - Start a HeartCast workout with the iPhone unlocked and HeartCast in front.
   - Open `http://localhost:8000/hr-bridge` in Chrome, connect, and keep it in its own visible window.
   - Lock the iPhone.
4. Wearer's Telegram is linked (`POST /v1/me/telegram-link`, then Start). Run `op /v1/demo/checkin-now`:
   the watch should tap "Reminder". Answer it.
5. Server and device:
   - `backend/scripts/serve.sh` is running.
   - `bridge.py --port ... --wear-mode hair` shows heartbeats.
   - `PUBLIC_BASE_URL` opens from a phone on cellular.
6. Judge invite: `op /v1/demo/judge-invite` gives a QR. The spare judge phone has joined, been approved and
   has the page in front with the volume up.

**Run**

| KAZE scenario | Action | What judges see |
|---|---|---|
| 1 Safe | `op /v1/demo/checkin-now` | The watch taps "Reminder". Maya squeezes twice; the watch shows "Noted ✓"; PROMPTED → ARMED. |
| 3 Duress | Next check-in: Maya squeezes the duress pattern | The watch shows the same "Noted ✓". Contact #1's Telegram: "SILENT HELP REQUEST… Don't call her phone." Contact #1 taps "I'm on it", then "I'm with her – she's safe". An acknowledgement alone never closes an alert. |
| 2 No response, 5 Unacknowledged, 4 Lost contact | 1. Maya bows her head, held 5 s (fallback: `op /v1/demo/inject '{"kind":"head_position"}'`). 2. She squeezes a wrong count. 3. `backend/scripts/chaos.sh kill`. 4. Pull the USB cable. | 1. "Reminder 3" (head position change → early check-in). 2. "2 ≠ 3, still open": one retry with a new count; at the deadline, REMINDED and a new "Reminder N". 3. The server restarts and L1 fires on time; Contact #1 ignores it. 4. After 10 s: "We lost contact with Maya's device… Cause unknown." After the ack window, L2: the judge's page turns red and rings. Judge taps "I'm on it", then "I'm with her". Replug: "Contact restored". |
| 6 False alarm | `op /v1/demo/hr-source '{"mode":"sim:climb_still"}'` (badged SIMULATED), then `op /v1/demo/fast-forward` until L1 | Simulated heart rate only brings a check-in forward. Maya's real PIN (`POST /v1/sessions/{session_id}/cancel`): "False alarm: Maya cancelled with her PIN." |
| 7 Privacy | Open the alert's location link after resolution | `410 Gone` after 15 s; contacts' pages are blank again. `GET /v1/me/export` shows `chain_ok: true`. |

**After:** `op /v1/demo/reset`, and `op /v1/demo/hr-source '{"mode":"live"}'`.

To rehearse the restart beat on its own: `ADMIN_KEY=... backend/scripts/chaos.sh mid-reminded` (needs
`serve.sh`). The `kill -9` beat is meant for the laptop with `serve.sh`.

There is no wearer web page (`/me`) in this repo; the wearer actions above are the HTTP calls in
[docs/API.md](docs/API.md#5-wearer).

## 10-minute checklist: Telegram, Tiger Cloud, Vultr

The app runs without any of these: Telegram falls back to a log channel, the database to the local one,
Vultr to the template. Plug them in one at a time and restart the server after each.

**Secrets rule:**

- Never paste a token, password or key into a chat, a commit, an issue or a terminal command line.
- Use the team's env-key flow (`/env-key`), which moves the value from the clipboard into `backend/.env`.
- Confirm by shape only, with the commands below. They print a count or a length, never the value.
- Rotate any key that passed through a chat window.

**1. Telegram "Reminders" bot (about 4 min)**

1. In Telegram, open @BotFather, send `/newbot`, name it "Reminders" and pick a username ending in `bot`.
2. Put the token in `TELEGRAM_BOT_TOKEN` with the env-key flow. Put the username (not secret) in
   `TELEGRAM_BOT_USERNAME`.
3. Shape check: `grep -c '^TELEGRAM_BOT_TOKEN=.' backend/.env` prints `1`.
4. Restart. `curl -s localhost:8000/v1/health` shows `"telegram":"configured"`.
5. Wearer: `curl -s -X POST localhost:8000/v1/me/telegram-link -H "Authorization: Bearer $WEARER_KEY"`,
   open the `t_me_link` on her phone and press Start. Then run `op /v1/demo/checkin-now`: a "Reminder"
   arrives and her watch taps.
6. Contacts: open the invite's `t_me_link` and press Start; the wearer approves them. A bot cannot message
   someone who has never started it.

**2. Tiger Cloud `DATABASE_URL` (about 3 min)**

1. In the Tiger console, create a service in region ca-central-1 and copy its connection string. It must
   include `sslmode=require`.
2. Put it in `DATABASE_URL` with the env-key flow.
3. Shape check: `grep -c '^DATABASE_URL=postgres' backend/.env` prints `1`.
4. Restart. The app applies the migrations itself (`AUTO_MIGRATE=1`); `/v1/health` shows `"db":"up"`.
5. Versions only, no secrets printed:
   ```bash
   cd backend && ../.venv/bin/python -c "from dotenv import load_dotenv; load_dotenv(); import os, psycopg; c = psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=10); print(c.execute(\"select current_setting('server_version'), (select extversion from pg_extension where extname = 'timescaledb')\").fetchone())"
   ```
6. Once, in the Tiger SQL editor: `ALTER ROLE tsdbadmin SET idle_in_transaction_session_timeout = '10s';`.
   A connection lost mid-transaction then cannot keep timer rows locked.
7. Tiger uses a per-service high port; check it works from the venue Wi-Fi before the demo.

**3. Vultr Serverless Inference key (about 3 min)**

1. In the Vultr console go to Products › Serverless › Inference › Add, give it a label, and read the charges note.
2. Copy the key from the subscription's Overview. It is the inference key, not the account API key.
3. Put it in `VULTR_API_KEY` with the env-key flow.
4. Shape check: `awk -F= '/^VULTR_API_KEY=/{print length($2) " chars"}' backend/.env`.
5. `cd backend && ../.venv/bin/python -m app.vultr` prints the config (never the key), the top card the
   rule engine gives the synthetic cohort, `source: vultr` and its briefing. `source: template` means the
   key, the model or the network is wrong, or the model's text failed the wording or number check; the log
   line says which. Each card on `/v1/care/alerts` still carries its own `briefing_source`.
6. Live model list, no auth: `curl -s https://api.vultrinference.com/v1/models`. Change `VULTR_MODEL` if
   `qwen3.8-flash-next` is gone.

## Deploy on a Vultr Compute VM

Expect about 20–30 min once the image builds locally; budget an hour.

1. **Account.** MLH gives Vultr credit to hackers. Vultr needs a linked card or PayPal before it applies
   promo credit (a $0.00 card link is offered).
2. **Instance.**
   - Cloud Compute, region Toronto (`yto`), plan `vc2-1c-2gb`, image Ubuntu 24.04 LTS. A 1 GB plan may run
     out of memory while building the image.
   - Tick "Enable Cloud-Init User-Data" and paste the script below. It holds no secrets.
     ```bash
     #!/bin/bash
     set -euxo pipefail
     curl -fsSL https://get.docker.com | sh
     git clone --depth 1 https://github.com/Camila-Rojas-Molina/HackTheValley11.git /opt/stillhere
     ```
   - If anything fails, look at `/var/log/cloud-init-output.log` first.
3. **Firewall group** (in the Vultr console, so Docker cannot bypass it): allow TCP 22 (your IP), 80 and 443
   only. Compose publishes the API on `127.0.0.1` only; Caddy is the public way in.
4. **Settings.** Copy the env file over:
   `scp backend/.env root@<ip>:/opt/stillhere/backend/.env`. On the VM, edit it:
   - `PUBLIC_HOSTNAME=stillhere.<ip-with-dashes>.nip.io` (nip.io resolves that name to the IP, so Caddy
     can get a Let's Encrypt certificate without a domain);
   - `PUBLIC_BASE_URL=https://stillhere.<ip-with-dashes>.nip.io`;
   - `DATABASE_URL`: the Tiger URL, or delete the line to use the bundled database.
5. **Start.**
   ```bash
   cd /opt/stillhere
   # with Tiger Cloud (no local database container):
   docker compose --env-file backend/.env run --rm --no-deps migrate
   docker compose --env-file backend/.env --profile vultr up -d --build --no-deps api caddy
   # or with the bundled TimescaleDB:
   docker compose --env-file backend/.env --profile vultr up -d --build
   ```
6. **Check.**
   - `curl -s https://stillhere.<ip-with-dashes>.nip.io/v1/health`
   - `docker compose logs -f api caddy`
   - Web Bluetooth (`/hr-bridge`) needs HTTPS or `localhost`; this HTTPS host works.
7. **Update.** `git pull && docker compose --env-file backend/.env --profile vultr up -d --build --no-deps api caddy`.
8. **After judging.** Destroy the instance and the inference subscription so nothing bills once the credit
   runs out.

Notes:

- Compose reads `backend/.env` with `--env-file`. Single-quote a value that contains `$`.
- The default inference model is served from Seattle. Only rule findings for synthetic residents are sent:
  no names, ids, contacts or locations.

## Demonstrated / simulated

| Item | Status | Assumption or what still needs validation |
|---|---|---|
| Durable countdown: L1 → L2, ACKED follow-up, `kill -9` resume | **Demonstrated** (`test_chaos_kill9.py`, `chaos.sh`) | At-least-once delivery means possible duplicates. Tested with one worker. |
| ESCALATED_FINAL ("would contact 911 / campus safety") | **Simulated, never placed** | A real escalation partner and protocol |
| Telegram nudges, alerts and buttons; contact web page | **Demonstrated** once a bot token is set; log channel without one | Contacts must have started the bot. Bot chats are not end-to-end encrypted, so alert text carries no health data. |
| Wearer approves contacts; single-use 10-min invites; silent removal | **Demonstrated** | Coercive-control scenarios need expert review |
| Squeeze OK / duress / counted challenge | **Demonstrated** over the line protocol (`bridge.py --stdin`, `simulate.py`) | On the real squeeze sensor it depends on the firmware; learnability is untested |
| Lost contact, distinct from no response | **Demonstrated** (unplug; heartbeats stop) | Lease length and grace period in real use |
| Live heart rate from the wearer's Apple Watch (HeartCast → `/hr-bridge` → `/v1/hr`) | **Demonstrated** (data path) | About 5 s updates; the iPhone relay must stay connected |
| Heart rate different from her usual → early check-in | **Simulated** (SIM source, `simulated: true`, badged) | Thresholds and false-prompt rate are untested assumptions |
| HRV / RMSSD | **Not built** | RR intervals are stored when present |
| Head position change → early check-in | **Demonstrated** as a device event; operator injection as fallback | Resting the head looks the same; thresholds are not validated |
| Fall-like motion | **Simulated** (operator injection) | Wrist and hair placements are weak for this |
| Location | **Simulated** by default (fixed UTSC coordinates, labelled); last-known from a wearer check-in (`POST /v1/sessions/{session_id}/checkin`) | A web page cannot read position while hidden; needs a native app |
| Timers in seconds | **Simulated compression** (demo profile) | The real profile uses minutes |
| Transport | **Demonstrated tethered** over USB serial | Production would use BLE to the phone |
| Device auth: static bearer key | **Assumed / disclosed** | HMAC and certificate pinning are roadmap |
| Duress replies byte-identical | **Demonstrated** (tests) | Response-timing side channel not addressed |
| No data before an alert; 410 link expiry; export / delete | **Demonstrated** | Formal privacy review |
| Personal timeline hash chain | **Demonstrated** (export recomputes the chain) | The server itself could recompute a chain before export; it is a personal record, not proof against the operator |
| Response windows adjustable up to 10× | **Demonstrated** (`window_multiplier`) | Usability with real users |
| Hosting on a Vultr Compute VM (compose + Caddy HTTPS) | **Configured**; the same compose stack was smoke-tested locally | Not yet deployed from this repo |
| Vultr Serverless Inference wording of PCC briefings | **Configured** and wired into `GET /v1/care/alerts`; tested against a mocked endpoint, and the template fallback is covered by tests | A live call needs a key. Before display the text is checked against the banned terms and the senior-mode wording rules, and every number in it must be on the card |
| Tiger Data: hypertables, 1 min → 1 h → 1 d real-time rollups, z-score view, compression, per-profile retention | **Demonstrated** on local TimescaleDB 2.30.2 (PostgreSQL 16, and 18 with Toolkit) and on plain PostgreSQL | Not yet run on Tiger Cloud. The z-score is display only |
| PointClickCare senior mode: rules, cards, suggested checks | **Simulated**: 20 synthetic residents, all labelled | Thresholds are untested assumptions tuned on synthetic data; never checked against real recordings |
| Full-night battery (watch, iPhone relay, scrunchie) | **Needs validation** | |
| Wearable form factor | **Not demonstrated** | Hardware iteration |

## Tiger Data

Raw scrunchie telemetry and heart rate live in the same PostgreSQL database as the KAZE core
(`backend/migrations/002_timeseries.sql`, `backend/app/timeseries.py`, routes in `docs/API.md` section 8).
On TimescaleDB (Tiger Cloud, or the bundled container) it uses hypertables, continuous aggregates,
compression and scheduled jobs; on plain PostgreSQL the same names exist as plain tables and views.
**Nothing in this layer can change a check-in session, open an alert or move a timer**: `timeseries.py`
imports nothing from the state machine, and the z-score is informational only (tests check both).

| Object | TimescaleDB | Plain PostgreSQL |
|---|---|---|
| `scrunchie_telemetry`: accel x/y/z (mg), two squeeze-sensor channels (mV), `profile`, `simulated` | hypertable, 1-day chunks | table |
| `hr_samples` (from `001`; gains `profile`, default `night_out`) | hypertable, 1-day chunks, existing rows migrated | table |
| `hr_1m` → `hr_1h` → `hr_1d`, `telemetry_1m` → `telemetry_1h` → `telemetry_1d` | hierarchical continuous aggregates, real-time (`materialized_only = false`), refresh policies | views over the raw rows |
| `user_zscore_5m` | view; Toolkit `stats_agg` when `timescaledb_toolkit` is installed | view (avg / stddev) |
| `timeseries_setup` | one row: `mode` (`timescaledb` or `plain`), `zscore_method`, and `note` (why setup took the plain path, if it did) | same |

- **Ingest.** `POST /v1/telemetry/batch` takes up to 6000 rows (2 min at 50 Hz) and stores them with one
  `COPY`. Device millisecond counters are mapped onto server time; a repeated `batch_id` is stored once.
  3000 rows (60 s at 50 Hz): median 28 ms per HTTP round trip over 5 runs, against a local
  TimescaleDB 2.30.2 / PostgreSQL 18.6 container.
- **Rollups.** Each layer stores count, sum, sum of squares, min and max, so the hourly and daily layers give
  exactly the mean and SD a raw rescan would. Refresh policies: 1 min every minute (last 2 h), 1 h every
  30 min (last day), 1 d every hour (last 3 days). Newer buckets come from raw rows at query time.
- **Z-score.** `user_zscore_5m`, per user and metric (`hr_bpm`, and `motion_enmo_mg` from the
  accelerometer): the mean of the last 5 minutes against the user's own 7-day baseline of 1-minute means.
  `z` is null under 30 baseline minutes. `GET /v1/telemetry/zscore` reads it; nothing else does.
- **Compression.** Raw chunks older than 1 day go to the columnstore, segmented by `user_id, profile` and
  ordered by `time DESC`.
- **Retention per profile.** TimescaleDB retention policies work per hypertable, so it has two parts:
  - a 30-day chunk retention policy on both raw tables (the `care` limit);
  - `stillhere_retention_job`, scheduled hourly, deletes `night_out` raw rows older than 48 h from both raw
    tables, compressed chunks included.

  | `profile` | Raw rows | 1-min rollups | Hourly rollups | Daily rollups |
  |---|---|---|---|---|
  | `night_out` (KAZE, the default; `POST /v1/hr` stores this) | 48 h | 8 days | 90 days | kept |
  | `care` (senior mode) | 30 days | 8 days | 90 days | kept |

  `DELETE /v1/me/data` erases the wearer's raw heart rate and raw telemetry at once. On TimescaleDB their
  rollup rows are not erased; they age out on the schedule above (daily rows are kept).
- **Plain PostgreSQL.** Extensions are created only if `pg_available_extensions` lists them, inside a
  `DO` block that catches errors. Every TimescaleDB call sits in one `DO` block behind a `pg_extension`
  check and runs as one sub-transaction: if any part fails, it is rolled back, the plain tables and views
  are created instead and `timeseries_setup.note` says why. With no scheduler, the app prunes raw rows
  itself, at most every 10 minutes, from the timer worker. The plain rollups are views, so they only cover raw
  rows still inside retention.

**Check it on Tiger Cloud** (after the 10-minute checklist above, with `ADMIN_KEY` set in your shell):

```bash
curl -s "$URL/v1/telemetry/info" -H "X-Admin-Key: $ADMIN_KEY"
```

- `info` should show `"mode":"timescaledb"`, the `timescaledb` and `timescaledb_toolkit` versions,
  `zscore_method` `toolkit_stats_agg`, 2 hypertables, 6 continuous aggregates and 15 jobs.
- `POST /v1/telemetry/synthetic` (operator) seeds a labelled 7-day synthetic baseline for the demo wearer.
  Then post live heart rate, and `GET /v1/telemetry/zscore` (wearer key) shows a `baseline_minutes` near
  10,080.
- In the Tiger SQL editor:
  - `SELECT * FROM timescaledb_information.jobs;`
  - `SELECT * FROM hypertable_columnstore_stats('scrunchie_telemetry');`
  - `CALL run_job(<job_id of stillhere_retention_job>);`

Tested here on TimescaleDB 2.30.2 with PostgreSQL 16.15 (the bundled `timescale/timescaledb:latest-pg16`)
and with PostgreSQL 18.6 + Toolkit 1.26.0 (`timescale/timescaledb-ha:pg18`), and on plain PostgreSQL 16
(pgserver). Tiger Cloud itself has not been tested from this repo. Tiger's docs say new services run
PostgreSQL 18 with `timescaledb` and `timescaledb_toolkit` enabled; `info` shows what yours runs.

## PointClickCare senior mode

A small, separate senior-mode demo on synthetic data (`backend/app/care.py`,
`backend/app/routes/care.py`, `backend/migrations/003_care.sql`; routes in `docs/API.md` section 9).
**No real residents.** Every row is labelled synthetic and a CHECK constraint on all three tables
enforces it. Rule-based change flags only: not a medical device, not clinically validated, and every
threshold is an untested assumption that each card states.

- **Synthetic cohort.** `POST /v1/demo/care-seed` (operator) generates 20 residents × 14 days:
  - 7 with a scripted gradual change;
  - 13 with no change: 9 stable, 2 with one bad night, 2 whose usual values are naturally high.

  The script is kept for checking the rules (`GET /v1/care/residents` shows it as `read_by_rules: false`);
  the rules never read it.
- **Rules.** Fixed code, no model. Each compares the resident's last 48 h with their own first 48 h in the
  record, never with other residents:
  - `postural_sway_drift`: quiet-standing sway (accelerometer);
  - `rise_to_settle_time`: after standing up, the time until balance and heart rate have both settled;
  - `night_restlessness`: minutes moving at night; both of the last 2 nights must be above their usual,
    so one bad night scores nothing.
- **Card.** `score` 0–100 is the sum of the rule points (30 + 40 + 30 at most). `level` is `check today`
  from 45 points or when any rule reaches its top band, `watch` from 15, otherwise `low` - or
  `not enough data` when a rule could not score because the device data stopped, so missing data never
  reads as "continue routine care". Each rule carries `observed`, `baseline`, `unit`, `contribution`
  and a plain explanation; the card adds a `suggested_check` for a care aide. `GET /v1/care/alerts`
  lists `watch`, `check today` and `not enough data` cards, `check today` first.
- **Briefing (Vultr).** After the score and level are fixed, `GET /v1/care/alerts` sends each `watch` /
  `check today` card's rule findings (no names, no ids) to Vultr Serverless Inference through
  `app.vultr.abriefing`, the async form of `briefing_text`, so the KAZE timer worker never waits on it.
  The model only rewords the briefing. Its text is shown only if it passes the wording rules below and
  every number in it is on the card. Without `VULTR_API_KEY`, on any error or timeout, or when the text is
  refused, the card keeps the rule template. `briefing_source` says which one is shown.
- **Wording.** Cards restate measurements against the resident's own baseline. They never name a
  condition, never say what will happen or how soon, and claim no sensor beyond motion and heart rate.
  `tests/test_care_wording.py` checks every card for 8 seeds, the live HTTP responses and the care source
  files; `tests/test_care_vultr.py` checks that model text breaking any of these rules is refused.
- **Kept apart from KAZE.** The care code imports only `app.care`, `app.security` and `app.vultr`, and
  no KAZE module imports it; only `main.py` registers its router (`tests/test_care_isolation.py`). Seeding and reading change no KAZE table
  (`tests/test_care_db.py`).
- **Results with the default seed (11).** All 7 scripted changes are flagged (6 `check today`, 1 `watch`)
  and none of the 13 no-change residents are. A 200-seed sweep test holds this for every seed. The data
  were tuned for these rules; real recordings will be noisier, and the 48 h baseline is short.

```bash
op /v1/demo/care-seed '{"end_day":"2026-10-16"}'
curl -s "$URL/v1/care/alerts"
```
