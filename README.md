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
- Timers are database rows, so a `kill -9` mid-countdown still fires on time after restart, and a timer whose transition errors is retried until it fires. The outbox is at-least-once with a dedupe key and keeps each recipient's messages in order.

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
backend/scripts/test_all.sh                                  # every suite in order, then a pass/fail table (about 12 min)
backend/scripts/test_all.sh unit property contract db        # only these suites
cd backend && ../.venv/bin/python -m pytest -q -m "not slow"  # fast suite, about 2 minutes
cd backend && ../.venv/bin/python -m pytest -q -m security    # any one marker
```

`scripts/test_all.sh` runs these suites, one pytest marker each (`backend/pytest.ini`; `tests/conftest.py`
applies them from the file and the fixtures a test uses):

| Suite | What it runs |
|---|---|
| `wording` | the banned-wording grep over every shipped file, the READMEs, `docs/` and `.github/` |
| `unit` | state machine, rules, parsers, care rules: no database |
| `property` | Hypothesis: random event sequences against the KAZE invariants, on the pure machine and through the API |
| `contract` | every route: status codes, auth failures, response shapes (a new route without a contract fails) |
| `db` | integration on plain PostgreSQL (pgserver) |
| `security` | the regression tests pinned to each review finding (`tests/security_findings.py`) |
| `load` | 50 device events and 10 contact taps at once: one transition, no lost or doubled outbox rows |
| `wiring` | the real app (`scripts/run.py`) with no keys: routers vs `docs/API.md`, pages, workers, heart rate, telemetry, PCC, bridge |
| `e2e` | `scripts/simulate.py`: all seven KAZE scenarios plus the PCC seed |
| `browser` | `c.html` and `hr_bridge.html` in the installed Google Chrome (Playwright `channel="chrome"`; never `playwright install`) |
| `chaos` | `kill -9` mid-REMINDED, the database cut under a running server, the bridge holding events across a restart |
| `tsdb`, `tsdb-db` | start `timescale/timescaledb:latest-pg16` as `stillhere-tsdb-matrix` on `127.0.0.1:55435`, run the migration and time-series tests, then the `db` suite against it, and remove the container. SKIP without Docker (`SKIP_DOCKER=1` skips them on purpose) |

- Unit tests need nothing.
- Integration tests start a private Postgres with pgserver in `../.pgdata-stillhere`, next to the repo.
  Set `STILLHERE_PGDATA=/path` for another data directory, or `STILLHERE_TEST_PG_URI=postgresql://...` for
  another server, such as the TimescaleDB container.
- `tests/test_chaos_kill9.py` starts a real server, `kill -9`s it mid-REMINDED, restarts it and checks that
  L1 fires once, within 2 s of the stored deadline.
- `tests/test_ops_live.py` runs the demo-night paths for real, each against its own server and database:
  `scripts/simulate.py` (all seven KAZE scenarios plus the PCC seed, `ops`), `scripts/bridge.py --stdin`
  forwarding `R` lines into `scrunchie_telemetry` (`ops`), and `scripts/serve.sh` with
  `scripts/chaos.sh mid-reminded` (`chaos`: the restart loop brings the killed server back and L1 fires once).
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

then the PointClickCare seed (`pcc`): it seeds the synthetic cohort and checks `/v1/care/alerts` lists every
scripted change. It prints PASS/FAIL per step and exits 1 on any failure (about 4 minutes in demo timing). It changes the
demo wearer's session and contacts and refuses to run while the demo wearer has approved contacts it did not
create, so give it its own server and database. Start that server with an empty `TELEGRAM_BOT_TOKEN`: on the
team's bot it would run a second poller and take contacts' button taps away from the real server
(`simulate.py` refuses to run against a server that has a bot token):

```bash
# Second database "stillhere_sim" (same pgserver snippet as above, another name), then:
TELEGRAM_BOT_TOKEN= PORT=8001 DATABASE_URL='<stillhere_sim URL>' backend/scripts/serve.sh &
STILLHERE_URL=http://127.0.0.1:8001 .venv/bin/python backend/scripts/simulate.py          # all seven + pcc
STILLHERE_URL=http://127.0.0.1:8001 .venv/bin/python backend/scripts/simulate.py 2 5 pcc  # a subset
```

Both read `ADMIN_KEY`, `DEVICE_KEY`, `DEMO_DEVICE_ID`, `WEARER_KEY`, `DEMO_PIN` and `DEMO_DURESS_PIN` from
`backend/.env`.

## Demo steps

Set `URL` and `ADMIN_KEY` (and `WEARER_KEY` for the export) in your shell first. Every operator action below
is labelled as demo in the timeline.

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
| 7 Privacy | Open the alert's location link after resolution | `410 Gone` after 15 s; contacts' pages are blank again. The printable export shows "Chain verified ✓" per session: `curl -s "$URL/v1/me/export?format=html" -H "Authorization: Bearer $WEARER_KEY" > export.html`, then open the file in the browser (JSON without `format`). |

**After:** `op /v1/demo/reset`, and `op /v1/demo/hr-source '{"mode":"live"}'`.

To rehearse the restart beat on its own: `ADMIN_KEY=... backend/scripts/chaos.sh mid-reminded` (needs
`serve.sh`). The `kill -9` beat is meant for the laptop with `serve.sh`.

There is no wearer web page (`/me`) in this repo; the wearer actions above are the HTTP calls in
[docs/API.md](docs/API.md#5-wearer).

## 10-minute checklist: Telegram, Tiger Cloud, Vultr

The app runs without any of these: Telegram falls back to a log channel, the database to the local one,
Vultr to the template. Plug them in one at a time and restart the server after each.

Not yet run against the live services: the Telegram Bot API (tests use a fake one), Tiger Cloud, Vultr
Serverless Inference (tests use a mocked endpoint) and a Vultr VM. Run this checklist before the demo, not
during it. The "tunnel down" chaos check (stop `cloudflared` for 30 s; the device and Telegram should not
notice, since the bridge talks to `localhost` and the bot long-polls out) has not been run either.

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
| Durable countdown: L1 → L2, ACKED follow-up, `kill -9` resume | **Demonstrated** (`test_chaos_kill9.py`, `chaos.sh`) | At-least-once delivery: a crash between Telegram accepting a message and the row being marked sent sends it once more (tested); a mark that keeps failing does not re-send. Tested with one worker. |
| ESCALATED_FINAL ("would contact 911 / campus safety") | **Simulated, never placed** | A real escalation partner and protocol |
| Telegram nudges, alerts and buttons; contact web page | **Demonstrated** once a bot token is set; log channel without one. The contact page runs end to end in headless Chrome (`test_ops_live.py`: consent tap, muted looping alarm, red ringing alert, "I'm on it", "she's safe") | Contacts must have started the bot. Bot chats are not end-to-end encrypted, so alert text carries no health data. Not yet run against the live Bot API, and not on an iPhone (mute switch, audio session, screen lock). |
| Wearer approves contacts; single-use 10-min invites (web and Telegram, both tested); silent removal | **Demonstrated** | Coercive-control scenarios need expert review |
| Squeeze OK / duress / counted challenge | **Demonstrated** over the line protocol (`bridge.py --stdin`, `simulate.py`) | On the real squeeze sensor it depends on the firmware; learnability is untested |
| Lost contact, distinct from no response | **Demonstrated** (unplug; heartbeats stop) | Lease length and grace period in real use |
| Live heart rate from the wearer's Apple Watch (HeartCast → `/hr-bridge` → `/v1/hr`) | **Demonstrated** (data path: `parseHRM` on byte vectors, `/v1/hr`, and the page's SIM fallback in headless Chrome) | About 5 s updates; the iPhone relay must stay connected. The Web Bluetooth connect and reconnect need HeartCast on an iPhone and were not run here |
| Heart rate different from her usual → early check-in | **Simulated** (SIM source, `simulated: true`, badged) | Thresholds and false-prompt rate are untested assumptions |
| HRV / RMSSD | **Not built** | RR intervals are stored when present |
| Head position change → early check-in | **Demonstrated** as a device event; operator injection as fallback | Resting the head looks the same; thresholds are not validated |
| Fall-like motion | **Simulated** (operator injection, logged `actor: demo`, `simulated: true`) | Wrist and hair placements are weak for this |
| Location | **Simulated** by default (fixed UTSC coordinates, labelled); last-known from a wearer check-in (`POST /v1/sessions/{session_id}/checkin`) | A web page cannot read position while hidden; needs a native app |
| Timers in seconds | **Simulated compression** (demo profile) | The real profile uses minutes |
| Transport | **Demonstrated tethered** over USB serial | Production would use BLE to the phone |
| Device auth: static bearer key | **Assumed / disclosed** | HMAC and certificate pinning are roadmap |
| Duress replies byte-identical | **Demonstrated** (tests) | Response-timing side channel not addressed |
| No data before an alert; 410 link expiry; export / delete | **Demonstrated** | Formal privacy review |
| Personal timeline hash chain | **Demonstrated** (export recomputes the chain; `?format=html` prints it with "Chain verified ✓" per session) | The server itself could recompute a chain before export; it is a personal record, not proof against the operator |
| Personal timeline retention: deleted 30 days after the session ended unless pinned | **Configured** (hourly job in the timer worker; tested with backdated sessions) | Same erase flag and tombstone as "Delete my data" |
| Response windows adjustable up to 10× | **Demonstrated** (`window_multiplier`) | Usability with real users |
| Hosting on a Vultr Compute VM (compose + Caddy HTTPS) | **Configured**; the compose stack was smoke-tested locally in an earlier environment with Docker, and not rebuilt since | Not yet deployed from this repo |
| Vultr Serverless Inference wording of PCC briefings | **Configured** and wired into `GET /v1/care/alerts`; tested against a mocked endpoint, and the template fallback is covered by tests | A live call needs a key. Before display the text is checked against the banned terms and the senior-mode wording rules, and every number in it must be on the card |
| Tiger Data: hypertables, 1 min → 1 h → 1 d real-time rollups, z-score view, compression, per-profile retention | **Demonstrated** on local TimescaleDB 2.30.2 (PostgreSQL 17.11 built from source, no Toolkit; earlier PostgreSQL 16, and 18 with Toolkit, in Docker) and on plain PostgreSQL | Not yet run on Tiger Cloud. The z-score is display only |
| Debug telemetry: RECORD-mode `R` lines → `scrunchie_telemetry` | **Configured**: `bridge.py` forwards them while the heartbeat reply says `"telemetry": "debug"` (demo profile); the real profile sends heartbeats and events only | Not yet run with the scrunchie's firmware; `feats` in heartbeats is accepted and not stored |
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
| `telemetry_heartbeat_1h` | Toolkit 1.15+ only: hourly real-time continuous aggregate of `heartbeat_agg` over care-profile telemetry, read with `rollup()` + `live_ranges()` | not created; `GET /v1/telemetry/gaps` uses `lead()` over raw rows |
| `timeseries_setup` | one row: `mode` (`timescaledb` or `plain`), `zscore_method`, `gaps_method`, and `note` (why setup took the plain path, if it did) | same |

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
  ordered by `time DESC`. The policy runs daily on chunks past the 1-day interval, so a weekend demo
  compresses nothing on its own: `POST /v1/telemetry/compression-demo` (operator) writes 3 days of SYNTHETIC
  care-profile telemetry at 1 Hz ending 25 h ago, converts those chunks with `convert_to_columnstore` and
  returns the bytes from `hypertable_columnstore_stats`. Measured on TimescaleDB 2.30.2 (Docker): telemetry
  46.1 MB → 4.9 MB (9.4×), heart rate 8.6 MB → 0.25 MB (33.9×). The data is synthetic.
- **Device gaps.** `GET /v1/telemetry/gaps` lists where the scrunchie sent no care-profile data ("device sent
  no data", 60 s liveness), from Toolkit `heartbeat_agg` → `live_ranges` when Toolkit 1.15+ is installed (the
  window minus the live ranges, so the silence before the first and after the last hour with data counts).
  Read-only: the server's check-in countdown stays the authority.
- **Query times.** Each senior-mode card carries `query_ms`, the measured time of its own query.
- **Connections.** A free Tiger service has 25 connections and no pooler. One API process holds at most
  `DB_POOL_MAX` (default 8) + 1 (Telegram poller lock) + 1 (start-up migration); `GET /v1/telemetry/info`
  shows the budget next to the server's `max_connections`.
- **Retention per profile.** TimescaleDB retention policies work per hypertable, so it has two parts:
  - a 30-day chunk retention policy on both raw tables (the `care` limit);
  - `stillhere_retention_job`, scheduled hourly, deletes `night_out` raw rows older than 48 h from both raw
    tables, compressed chunks included.

  | `profile` | Raw rows | 1-min rollups | Hourly rollups | Daily rollups |
  |---|---|---|---|---|
  | `night_out` (KAZE, the default; `POST /v1/hr` stores this) | 48 h | 8 days | 8 days | 8 days |
  | `care` (senior mode) | 30 days | 8 days | 90 days | kept |

  Continuous aggregates have no per-profile retention policy, so the same hourly job also deletes
  `night_out` rows older than 8 days (the z-score baseline needs 7) from every rollup's materialized
  hypertable (`stillhere_prune_rollups`, migration `018`).
  `DELETE /v1/me/data` erases the wearer's raw heart rate and raw telemetry and, on TimescaleDB, every rollup
  row of theirs (`stillhere_erase_user_rollups`) in the same transaction (`erased.rollup_rows`).
- **Plain PostgreSQL.** Extensions are created only if `pg_available_extensions` lists them, inside a
  `DO` block that catches errors, and `timescaledb` only when its library is preloaded
  (`shared_preload_libraries`): installed but not preloaded, `CREATE EXTENSION` would end the session, so the
  plain objects are used and `timeseries_setup.note` says why. Every TimescaleDB call sits in one `DO` block behind a `pg_extension`
  check and runs as one sub-transaction: if any part fails, it is rolled back, the plain tables and views
  are created instead and `timeseries_setup.note` says why. With no scheduler, the app prunes raw rows
  itself, at most every 10 minutes, from the timer worker. The plain rollups are views, so they only cover raw
  rows still inside retention.

**Check it on Tiger Cloud** (after the 10-minute checklist above, with `ADMIN_KEY` set in your shell):

```bash
curl -s "$URL/v1/telemetry/info" -H "X-Admin-Key: $ADMIN_KEY"
```

- `info` should show `"mode":"timescaledb"`, the `timescaledb` and `timescaledb_toolkit` versions,
  `zscore_method` `toolkit_stats_agg`, `gaps_method` `toolkit_heartbeat_agg`, 2 hypertables, 7 continuous
  aggregates and 17 jobs (6 and 15 without Toolkit).
- `POST /v1/telemetry/synthetic` (operator) seeds a labelled 7-day synthetic baseline for the demo wearer.
  Then post live heart rate, and `GET /v1/telemetry/zscore` (wearer key) shows a `baseline_minutes` near
  10,080.
- In the Tiger SQL editor:
  - `SELECT * FROM timescaledb_information.jobs;`
  - `SELECT * FROM hypertable_columnstore_stats('scrunchie_telemetry');`
  - `CALL run_job(<job_id of stillhere_retention_job>);`

Tested on TimescaleDB 2.30.2 with PostgreSQL 17.11 built from source (no Docker on that machine; no
Toolkit, so the 3 Toolkit tests skip): `tests/test_timeseries_tsdb.py` and the whole suite with
`STILLHERE_TEST_PG_URI` pointing at it. Earlier also with PostgreSQL 16.15 (the bundled
`timescale/timescaledb:latest-pg16`) and PostgreSQL 18.6 + Toolkit 1.26.0 (`timescale/timescaledb-ha:pg18`),
and always on plain PostgreSQL 16 (pgserver). Tiger Cloud itself has not been tested from this repo. Tiger's docs say new services run
PostgreSQL 18 with `timescaledb` and `timescaledb_toolkit` enabled; `info` shows what yours runs.

## PointClickCare senior mode

A small, separate senior-mode demo on synthetic data (`backend/app/care.py`,
`backend/app/routes/care.py`, the `*care*` migrations; routes in `docs/API.md` section 9).
**No real residents.** Every row is labelled synthetic and CHECK constraints enforce it. Rule-based change
flags only: not a medical device, not clinically validated, and every threshold is an untested assumption
that each card states with its source.

- **Synthetic cohort.** `POST /v1/demo/care-seed` (operator) generates 20 residents × 28 days:
  - 5 with a scripted change on the last days (heart rate after standing smaller or bigger, restless nights,
    both together);
  - 15 with no scored change: stable residents, one bad night, naturally high values, a resident whose record
    notes a pacemaker (the heart-rate rule is off), and 2 whose sway drifts (shown, never scored).

  The script is kept for checking the rules (`GET /v1/care/residents` shows it as `read_by_rules: false`);
  the rules never read it.
- **Baseline.** Rolling: the resident's own 14 days just before the last 48 h, with 7 valid days of warm-up
  before any rule can score (Skubic 2015; Rispens 2015).
- **Rules.** Fixed code, no model:
  - `stand_hr_response`: heart-rate change after standing (mean 60–180 s after minus the 60 s before, when
    heart rate is available), both of the last 2 days at z ≤ −2.5 or ≥ +2.5; off for non-sinus rhythm or a
    pacemaker. The check it suggests is lying and standing vitals per the home's protocol.
  - `night_restlessness`: both of the last 2 nights at z ≥ +2.5, +15% and 15 min above the usual.
  - Shown greyed out, "experimental, not scored": standing sway and balance settling after standing.
- **Card.** A 0–100 **change score** (never a risk score) and a level that never follows the score:
  `worth a look this shift` for one rule; `review this shift` only with 2 different rules in 3 days, or one
  rule plus a synthetic medication change in the last 30 days (which never starts an alert alone). Data
  quality (hours worn, gaps, heart rate available or not), the suggested check, the line "Pattern change
  only - not a medical assessment." and Useful / Not useful / Already known / Snooze 3 days buttons.
- **Alert budget.** At most 1 push per resident per 24 h; an identical alert is muted for 3 days unless it
  gets worse; everything else goes to the shift-start digest (`GET /v1/care/digest`). Seeding the next day
  keeps the push history and the feedback buttons' snoozes, so the budget and "Snooze 3 days" show through the
  API (`"reset": true` starts over).
- **Tiger Data.** Each card shows `query_ms`, the measured time of its own query.
- **Briefing (Vultr).** After the score and level are fixed, `GET /v1/care/alerts` sends each flagged card's
  rule findings (no names, no ids) to Vultr Serverless Inference through `app.vultr.abriefing`, so the KAZE
  timer worker never waits on it. The model only rewords the briefing. Its text is shown only if it passes
  the wording rules below and every number in it is on the card. Without `VULTR_API_KEY`, on any error or
  timeout, or when the text is refused, the card keeps the rule template. `briefing_source` says which.
- **Wording.** Cards restate measurements against the resident's own baseline. They never name a
  condition, never say what will happen or how soon, never call the number a risk, and claim no sensor
  beyond motion and heart rate. `tests/test_care_wording.py` checks every card for 8 seeds, the live HTTP
  responses and the care source files; `tests/test_care_vultr.py` checks that model text breaking any of
  these rules is refused.
- **Kept apart from KAZE.** The care code imports only `app.care`, `app.security` and `app.vultr`, and
  no KAZE module imports it; only `main.py` registers its router (`tests/test_care_isolation.py`). Seeding
  and reading change no KAZE table (`tests/test_care_db.py`).
- **Results with the default seed (11).** All 5 scripted changes are flagged (2 `review this shift`, one of
  them raised by medication context, and 3 `worth a look this shift`) and none of the 15 no-change residents
  are. A 200-seed sweep test holds this for every seed. The data were tuned for these rules; real recordings
  will be noisier.

```bash
op /v1/demo/care-seed '{"end_day":"2026-10-16"}'
curl -s "$URL/v1/care/alerts"
```
