# StillHere backend (KAZE core)

FastAPI + PostgreSQL. A server-side countdown escalates through trusted contacts
when the wearer stops answering check-ins, until a person takes responsibility.
Sensors and heart rate only ever bring the next check-in forward.

## Safety rules (enforced in code, covered by tests)

| Rule | Where | Test |
|---|---|---|
| Sensor / heart-rate input can only output `PULL_FORWARD` (ARMED → PROMPTED with a counted challenge N ∈ {1,3,4}); it never reaches `ESCALATED_*` | `app/rules.py` returns only `PullForward`; `app/states.py` `_check_invariants` raises otherwise | `test_states.py::test_sensor_input_only_ever_pulls_forward`, random walks, `test_rules.py` |
| Only a contact's "she's safe" or the wearer's real PIN closes an alert | `states._check_invariants` | `test_only_safe_or_real_pin_resolves`, `test_ack_followup_timeout_then_safe_resolves` |
| A late device squeeze never stands down an escalation | `states._late_response` | `test_late_squeeze_never_stands_down` (unit + DB) |
| Real-PIN and duress-PIN replies are byte-identical (HTTP body, device `cmd`, wearer message, visible timeline); duress is masked (`state_public` reads ARMED) | `states._on_pin_duress`, `_duress_squeeze`, `deps.canonical_json` | `test_duress_is_indistinguishable_to_the_wearer`, `test_duress_squeeze_is_masked_and_byte_identical`, `test_pin_cancel_real_and_duress_are_byte_identical` |
| Contacts see nothing before an alert and must be approved by the wearer | `routes/contacts.py` (`{"alert": null}`), escalation uses approved contacts only | `test_contacts_see_nothing_before_alert_and_must_be_approved` |
| FINAL shows "Emergency services: SIMULATED" and never calls anyone | `states._enter_final` (logged `placed: false`; no calling code exists) | `test_arm_no_response_l1_l2_final` |
| LOST_CONTACT has its own wording; pending deadlines keep running | `states._on_lease_expired` | `test_lost_contact_*` |
| Timers are rows claimed with `FOR UPDATE SKIP LOCKED` and survive `kill -9` | `app/timers.py` | `test_skip_locked_lets_one_worker_take_a_timer`, `test_chaos_kill9.py` |
| Outbox is at-least-once with a dedupe key; Telegram 429 `retry_after` honoured | `app/outbox.py` | `test_outbox_telegram_db.py` |
| Banned wording never appears in shipped text | `app/messages.py` | `test_wording.py` |
| No model (LLM) in any safety decision | the state machine is plain code | — |

The personal timeline (`audit_log`) is append-only (trigger) and hash-chained per
session; `GET /v1/me/export` recomputes the chain.

## Run it

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp backend/.env.example backend/.env      # fill in; never commit it
cd backend && ../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

On start the app applies `migrations/*.sql` (`AUTO_MIGRATE=1`), seeds the demo
wearer and device (`DEMO_SEED=1`) and starts three asyncio tasks: `timer_worker`
(1 s), `outbox_sender` and `telegram_poller`.

Postgres options:
- Docker: `docker run -d --name stillhere-db -e POSTGRES_PASSWORD=<pw> -p 5432:5432 timescale/timescaledb:latest-pg16`
- Tiger Cloud: put the service URL in `DATABASE_URL`. Consider
  `ALTER ROLE <user> SET idle_in_transaction_session_timeout = '10s'`; the app
  also sets it per connection.
- Migrations alone: `DATABASE_URL=... python scripts/migrate.py`

`001_core.sql` needs plain PostgreSQL only. `hr_samples` is a plain table so a
later migration can turn it into a hypertable when TimescaleDB is present.

## Tests

```bash
cd backend && ../.venv/bin/python -m pytest -q
```

Unit tests need no database. Integration tests start a private local Postgres
with the `pgserver` pip package; set `STILLHERE_TEST_PG_URI` to use another
server (for example the TimescaleDB container). `tests/test_chaos_kill9.py`
starts a real server, `kill -9`s it mid-REMINDED, restarts it and checks that L1
still fires within 2 s of the stored deadline, exactly once.

## Timing profiles

| | demo | real |
|---|---|---|
| check-in interval | 60 s | 30 min (or preset) |
| prompt window (+30 s until delivered) | 12 s | 2 min |
| ack window / duress ack | 15 s / 10 s | 5 min / 2 min |
| ACKED follow-up | 20 s | 10 min |
| renotify (cap 10 per level) | 20 s | 60 s |
| device lease (heartbeat) | 10 s (2 s) | 60 s (15 s) |
| lost-contact grace before contacts hear | 10 s | 5 min |
| location link after resolution | 15 s | 30 min |

Response windows can be stretched up to 10× per session (`window_multiplier`).
Prompt deadlines start at delivery: `now + window + 30 s`, lowered to
`sent_at + window` when the wearer's nudge is sent or the device reports `prompt_shown`.

## HTTP API

Device (`Authorization: Bearer <device key>`, `X-Device-Id`, `X-Boot-Id`, `X-Seq`):
- `POST /v1/device/heartbeat` `{wear_mode, enmo60_mg, offbody}` → `{state_public, server_time, cmd:{led, blink_n, challenge_id}}`
- `POST /v1/device/events` `{event_id: "<boot_id>:<seq>", type: squeeze|head_position_change|offbody|fall_like|prompt_shown, payload, device_ms}`.
  Squeeze payload: `{pattern: plain|duress|unrecognized, k, buffered?}`. A replayed `event_id` returns the cached reply byte for byte.

Heart rate (any source; device key or wearer key):
- `POST /v1/hr` `{source, simulated, samples:[{t, bpm, rr_ms, rr_synthetic}]}` or one sample `{ts, bpm, ...}`
- `POST /v1/hr/calibrate` `{window_s}`: seated baseline (median) before going out

Wearer (`Authorization: Bearer <wearer key>`):
- `POST /v1/sessions` `{preset, mode, profile, interval_s, duration_s, window_multiplier}` → includes `who_can_be_alerted`
- `GET /v1/sessions/{id}`, `GET /v1/sessions/{id}/timeline` (duress entries hidden until resolved)
- `POST /v1/sessions/{id}/checkin` `{lat, lon, accuracy_m}` ("I'm OK"; refreshes last-known location)
- `POST /v1/sessions/{id}/cancel {pin}`, `POST /v1/sessions/{id}/end {pin}`: identical bytes for real and duress PINs
- `POST /v1/contacts {name}` → `{t_me_link, web_link, qr_png}` (single use, 10 min); `GET /v1/contacts`;
  `POST /v1/contacts/{id}/approve`; `DELETE /v1/contacts/{id}` (silent)
- `GET /v1/me/export`, `DELETE /v1/me/data`, `POST /v1/me/telegram-link`

Contact (capability token; cannot arm, see session status or ask for location):
- `GET /c/{token}` page; `POST /v1/contact/{token}/join`; `GET /v1/contact/{token}/status` (`{"alert": null}` before an alert)
- `POST /v1/contact/{token}/ack`; `POST /v1/contact/{token}/followup {action: safe|cant_reach|pass}`
- `GET /l/{share_token}`: last-known location, `410 Gone` after expiry

Operator (`X-Admin-Key`; changes are logged with `actor = demo`):
`GET /v1/health`, `GET /v1/admin/timers`, `GET /v1/admin/overview`, `POST /v1/demo/checkin-now`,
`POST /v1/demo/inject {kind}`, `POST /v1/demo/hr-source {mode}`, `POST /v1/demo/profile {profile}`,
`POST /v1/demo/fast-forward`, `POST /v1/demo/judge-invite`, `POST /v1/demo/reset`.

## Telegram ("Reminders" bot)

Without `TELEGRAM_BOT_TOKEN` nothing is sent: wearer messages are logged and
contacts read alerts on the live page. To plug it in:
1. Create a bot with @BotFather, name it "Reminders"; put the token in
   `TELEGRAM_BOT_TOKEN` and its username in `TELEGRAM_BOT_USERNAME`.
2. Restart. The poller clears any webhook and long-polls `getUpdates`; only one
   process polls (Postgres advisory lock).
3. Wearer: `POST /v1/me/telegram-link`, open the `t_me_link` and press Start.
   "Reminder" nudges then arrive in that chat, which the Apple Watch mirrors as a silent tap.
4. Contacts: open the invite's `t_me_link` and press Start, then the wearer approves them.
   A bot cannot message someone who has never started it.

Alert messages use `protect_content`, carry no health data, and offer
"I'm on it" / "Can't help – pass it on"; after an ack the message gains
"I'm with her – she's safe" / "Can't reach her".
