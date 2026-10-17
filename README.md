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
- [Tiger Data](#tiger-data) (TODO)
- [PointClickCare senior mode](#pointclickcare-senior-mode) (TODO)

API contract for firmware and frontends: [docs/API.md](docs/API.md). Backend internals, timing profiles and
the safety-rule table with the tests behind each rule: [backend/README.md](backend/README.md).

## Architecture

```
 scrunchie (squeeze sensor, accelerometer)        Apple Watch -> HeartCast (iPhone) -> BLE Heart Rate 0x180D
          | USB serial, 115200 baud, line protocol                                 |
          v                                                                        v
 backend/scripts/bridge.py ---- HTTP ---->  FastAPI  backend/app  <---- /hr-bridge in Chrome (Web Bluetooth)
                                            |  routes/   device, hr, sessions, contacts, demo, health
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
5. `cd backend && ../.venv/bin/python -m app.vultr` prints the config (never the key), `source: vultr` and a
   sample briefing. `source: template` means the key, the model or the network is wrong; the log line
   says which.
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
| Vultr Serverless Inference wording of PCC briefings | **Configured**; the template fallback is covered by tests | A live call needs a key. Before display the text is checked against the banned terms, and every number in it must be on the card |
| Full-night battery (watch, iPhone relay, scrunchie) | **Needs validation** | |
| Wearable form factor | **Not demonstrated** | Hardware iteration |

## Tiger Data

> **TODO (merge stage):**
> - the hypertables (`scrunchie_telemetry`, `hr_samples`);
> - continuous aggregates 1 min → 1 h → 1 d with real-time aggregation;
> - the 5-min vs 7-day z-score view, compression after 1 day;
> - retention per profile (night-out 48 h raw, senior mode longer);
> - how the migrations skip the Timescale-only statements on plain Postgres;
> - how to check each feature on Tiger Cloud.
>
> The bundled `timescale/timescaledb:latest-pg16` container has TimescaleDB 2.30.2, the same version as the
> Tiger Cloud service.

## PointClickCare senior mode

> **TODO (merge stage):**
> - the synthetic generator (20 residents × 14 days, all labelled SYNTHETIC);
> - the deterministic rules: sway drift, rise-to-settle, night restlessness;
> - `GET /v1/care/residents` and `GET /v1/care/alerts`, which also go into `docs/API.md`.
>
> The alert card contract shared with the Vultr layer:
> `{resident_id, display_name, synthetic: true, score: 0-100, level: "low"|"watch"|"check today",
> rules: [{rule, observed, baseline, unit, contribution, explanation}], suggested_check, briefing}`.
> Fill `briefing` with `app.vultr.briefing_text(card)`, or `await app.vultr.abriefing_text(card)` inside an
> async route. The KAZE timer worker shares the event loop, so a blocking call there could delay a deadline.
