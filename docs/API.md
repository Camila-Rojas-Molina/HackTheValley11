# StillHere API

This is the contract for the scrunchie firmware, `scripts/bridge.py`, the heart-rate bridge, the contact
page and any frontend. The examples are real responses captured from the server on this branch, with
tokens and timestamps shortened.

- Base URL: `PUBLIC_BASE_URL`, for example `http://localhost:8000` or `https://stillhere.<ip>.nip.io`.
- Bodies are JSON. Timestamps are ISO 8601 UTC. The server's clock is the only clock that counts.
- Interactive schema: `GET /docs` (Swagger UI), `GET /redoc`, `GET /openapi.json`, and
  `GET /docs/oauth2-redirect` (a Swagger UI helper; nothing uses OAuth).

## Contents

1. [Auth and conventions](#1-auth-and-conventions)
2. [Device serial line protocol](#2-device-serial-line-protocol)
3. [Device HTTP](#3-device-http)
4. [Heart rate](#4-heart-rate)
5. [Wearer](#5-wearer)
6. [Contacts](#6-contacts)
7. [Operator and demo](#7-operator-and-demo)
8. [Telemetry (Tiger Data)](#8-telemetry-tiger-data)
9. [Senior mode (PointClickCare, synthetic)](#9-senior-mode-pointclickcare-synthetic)
10. [Health](#10-health)
11. [Outbound services](#11-outbound-services)

## 1. Auth and conventions

| Caller | Credential | Errors |
|---|---|---|
| Device / bridge | `Authorization: Bearer <DEVICE_KEY>` and `X-Device-Id: <id>`; also `X-Boot-Id` and `X-Seq` | `401 {"detail":"device key and X-Device-Id required"}`, `401 {"detail":"unknown device or wrong key"}` |
| Wearer (`/me`) | `Authorization: Bearer <WEARER_KEY>` | `401 {"detail":"wrong wearer key"}` |
| Contact | the capability token in the URL path; it cannot arm a session, see the session or ask for a location | `404`, `410` |
| Operator | `X-Admin-Key: <ADMIN_KEY>` | `401 {"detail":"wrong X-Admin-Key"}`; `503` when `ADMIN_KEY` is unset |
| Senior mode (`/v1/care/*`) | none: every row is synthetic, enforced by a CHECK constraint (feedback buttons included) | `404`, `422` |

- **Database down:** every endpoint that needs the database answers `503 {"ok":false,"error":"database unavailable"}`.
- **Canonical JSON:** device replies and the wearer's PIN and check-in replies are serialised with sorted keys
  and no spaces, so equal replies are equal bytes.
- **Byte-identical PINs:** the real PIN and the duress PIN produce the same status code and the same bytes on
  `cancel` and `end`. The duress alert is masked: the device and the wearer see `state_public` as `ARMED`,
  or `ENDED` after `end`.
- **Sensors only bring a check-in forward:** head position, fall-like motion, off-body and heart rate can only
  move `ARMED` to `PROMPTED` with a counted challenge (blink N, squeeze N). They never open an alert.
- **Closing an alert:** only a contact's "she's safe" or the wearer's real PIN closes one. A device squeeze
  during an alert is recorded and passed on to contacts; it never stands the alert down.

Public states (`state_public`): `DISARMED` (no session yet), `ARMED`, `PROMPTED`, `REMINDED`,
`ESCALATED_L1`, `ESCALATED_L2`, …, `ACKED`, `ESCALATED_FINAL`, `ENDED`. `link_state` is `OK` or
`LOST_CONTACT`.

## 2. Device serial line protocol

USB serial, 115200 baud, one ASCII line per message, `\n` terminated. `scripts/bridge.py` translates it to
the HTTP calls in section 3.

```
device -> bridge
  H,<boot_id>,<seq>,<enmo60_mg>,<tilt_deg>,<fsr_base_mv>,<offbody>     every 1 s
  E,<boot_id>:<seq>,<type>,<json_payload>                              immediately
  R,<ms>,<ax_mg>,<ay_mg>,<az_mg>,<fsr_a_mv>,<fsr_b_mv>                 50 Hz RECORD mode; counted, not forwarded
bridge -> device
  C,<led>,<blink_n>,<challenge_id>                                     after every heartbeat or event reply
```

| Field | Meaning |
|---|---|
| `boot_id` | Random id chosen at power-on. A new `boot_id` resets the sequence. |
| `seq` | Counter within one boot. The bridge sends it as `X-Seq`; an `H` line with an old `seq` does not extend the lease. |
| `enmo60_mg` | Mean ENMO over the last 60 s, in milli-g (≥ 0). Below 45 mg counts as still (an untested engineering assumption). |
| `tilt_deg`, `fsr_base_mv` | Head tilt and the squeeze sensor's resting level. Parsed; not sent to the server today. |
| `offbody` | `1`/`true`/`t`/`yes` when the scrunchie is off; anything else is false. |
| `type` | `squeeze`, `head_position_change`, `fall_like`, `offbody` or `prompt_shown`. |
| `json_payload` | A JSON object; it may contain commas. `device_ms` (integer) inside it is moved to the HTTP body. |
| `led` | `off`, `amber` (prompt) or `red` (alert open). |
| `blink_n` | Challenge count to blink: `0` for a routine prompt (answer with 2 squeezes), else 1, 3 or 4. |
| `challenge_id` | Echo of the current prompt; empty when there is none. |

Examples:

```
H,b7,120,12.5,4.0,812,0
E,b7:121,squeeze,{"pattern":"plain","k":2,"device_ms":81900}
E,b7:122,squeeze,{"pattern":"duress","k":3}
E,b7:123,head_position_change,{}
C,amber,0,7c6f9d
C,off,0,
```

Bridge behaviour:

- **Heartbeats:** `POST /v1/device/heartbeat` every `--heartbeat-s` (2 s demo, 15 s real), only while
  `H` lines keep arriving. Unplug the device and the server's lease runs out, which sets `LOST_CONTACT`.
- **Events:** posted at once with `event_id = <boot_id>:<seq>`, retried after 1, 2, 4 and 8 s (then every 8 s)
  with the same id.
- **Server down:** events wait in memory and are flushed duress first, then oldest first, marked
  `"buffered": true`. A buffered squeeze counts as late, and a late squeeze never stands an alert down.
- **Replug:** the serial port is reopened automatically.
- `--stdin` reads the same lines from stdin, with no hardware and no pyserial.

## 3. Device HTTP

Headers on both calls: `Authorization: Bearer <DEVICE_KEY>`, `X-Device-Id`, `X-Boot-Id`, `X-Seq`.

### `POST /v1/device/heartbeat`

Extends the device lease (10 s demo, 60 s real) and returns the command for the LED.

```json
{"wear_mode": "hair", "enmo60_mg": 12.5, "offbody": false}
```

All fields are optional. `wear_mode` is `wrist` or `hair`; `enmo60_mg` is 0–20000; `feats` is an optional
list (debug telemetry). Replies:

```json
{"cmd":{"blink_n":0,"challenge_id":null,"led":"off"},"server_time":"2026-10-17T07:33:53.077279+00:00","state_public":"ARMED"}
{"cmd":{"blink_n":0,"challenge_id":"7c6f9d","led":"amber"},"server_time":"2026-10-17T07:33:53.083048+00:00","state_public":"PROMPTED"}
{"cmd":{"blink_n":1,"challenge_id":"8a3f08","led":"amber"},"server_time":"2026-10-17T07:33:53.090821+00:00","state_public":"PROMPTED"}
```

With no session running, `state_public` is `DISARMED` (never armed) or `ENDED`, and the LED is off. The first
heartbeat after a `LOST_CONTACT` restores the link and tells the contacts who were told about the loss.

### `POST /v1/device/events`

```json
{"event_id": "b7:41", "type": "squeeze", "payload": {"pattern": "plain", "k": 2}, "device_ms": 81900}
```

| `type` | `payload` | Effect |
|---|---|---|
| `squeeze` | `{pattern: plain\|duress\|unrecognized, k, buffered?}` | Answers a prompt (`k` must equal the challenge count, 2 for a routine prompt). Wrong `k` gets one retry with a new count. `duress` opens a masked alert. During an alert it is recorded only. |
| `head_position_change`, `fall_like`, `offbody` | any object (not read) | `ARMED` → `PROMPTED` with a counted challenge, at most once per cooldown (60 s demo, 15 min real). |
| `prompt_shown` | `{}` | Lowers the prompt deadline to "shown + window". |

`device_ms` is 0 to 2^53 − 1, and payload numbers must be finite; otherwise `422` (the event is not stored).

Reply: `{cmd, ok, result, state_public}`. `result` is `noted`, `retry`, `prompted`, `ignored`, `cooldown`,
`deadline_set` or `no_session`.

```json
{"cmd":{"blink_n":0,"challenge_id":null,"led":"off"},"ok":true,"result":"noted","state_public":"ARMED"}
{"cmd":{"blink_n":4,"challenge_id":"9e992e","led":"amber"},"ok":true,"result":"retry","state_public":"PROMPTED"}
{"cmd":{"blink_n":4,"challenge_id":"9e992e","led":"amber"},"ok":true,"result":"cooldown","state_public":"PROMPTED"}
{"cmd":{"blink_n":0,"challenge_id":"7c6f9d","led":"amber"},"ok":true,"result":"deadline_set","state_public":"PROMPTED"}
```

A squeeze while an alert is open leaves the state alone; contacts are told the wearer responded on her
device and are asked to still check on her:

```json
{"cmd":{"blink_n":0,"challenge_id":null,"led":"red"},"ok":true,"result":"noted","state_public":"ESCALATED_L1"}
```

**Idempotent:** a replayed `event_id` (same device) returns the stored reply byte for byte and changes nothing.
A duress squeeze returns exactly the bytes a plain squeeze would have returned.

## 4. Heart rate

Heart rate is pluggable: any source can post here (HeartCast on the iPhone via `/hr-bridge` in Chrome, a
streaming-service API, an Apple Health Shortcut, the simulator). Heart rate can only bring a check-in
forward. The heart-rate rule fires when the 60 s median differs from the wearer's baseline by at least
max(30 bpm, 35 %) for 15 s (demo) or 90 s (real) while she is still. A gap of more than 120 s also brings
the check-in forward, once per gap. All of these numbers are untested engineering assumptions
(`app/rules.py`), not validated values.

### `POST /v1/hr`

Auth: the wearer key, or the device key plus `X-Device-Id`. `user_id` is optional and must match the
credential (`403` otherwise).

```json
{"source": "apple_watch_heartcast", "simulated": false,
 "samples": [{"t": 1792222433.1, "bpm": 71, "rr_ms": [845.7], "rr_synthetic": false}]}
```

or a single sample: `{"ts": "2026-10-17T07:33:53Z", "bpm": 72, "source": "apple_watch_heartcast", "simulated": false}`.

- `bpm` 20–250; up to 600 samples per call. Each `rr_ms` value is 0–65535 (the BLE Heart Rate Service range); otherwise `422`.
- `t`/`ts` is epoch seconds, epoch milliseconds or ISO 8601. A missing time, or one more than 5 min from server time, becomes server time.
- `rr_synthetic: true` marks RR intervals that are just 60000/bpm. They are stored; RMSSD is not computed.

```json
{"ok": true, "inserted": 6}
```

### `POST /v1/hr/calibrate`

Wearer key. Seated baseline before going out: the median of the last `window_s` seconds (10–3600,
default 300), real samples only unless `allow_simulated` is true. Needs at least 5 samples (`409` otherwise).

```json
{"window_s": 300}
```
```json
{"ok": true, "baseline_bpm": 71.0, "samples": 7, "simulated": false}
```

### `GET /hr-bridge`

HTML page. Chrome Web Bluetooth reads the standard Heart Rate Service (0x180D, characteristic 0x2A37)
that HeartCast broadcasts from the Apple Watch, and posts to `/v1/hr` from the notification handler. It has
a SIM button that streams `simulated: true`. Open it on `localhost` or over HTTPS (Web Bluetooth needs a
secure context).

## 5. Wearer

All calls need `Authorization: Bearer <WEARER_KEY>`.

### `POST /v1/sessions`

Arm a session.

```json
{"preset": "night_out", "mode": "hair", "profile": "demo"}
```

| Field | Values |
|---|---|
| `preset` | `night_out` (6 h), `walking_home` (45 min) or `demo` (no end); each ends with a "Home safe?" prompt |
| `mode` | `wrist` (default) or `hair` |
| `profile` | `demo` (seconds) or `real` (minutes); default `STILLHERE_PROFILE` |
| `interval_s` | 10–86400; default 60 s demo, 30 min real |
| `duration_s` | 30–86400; overrides the preset's length |
| `window_multiplier` | 1–10; stretches every response window |
| `device_id` | default: the wearer's first device |

```json
{"session_id": "9a09f145-…", "state_public": "ARMED", "profile": "demo", "interval_s": 60,
 "end_at": "2026-10-17T13:33:53.072794+00:00",
 "who_can_be_alerted": [{"n": 1, "name": "Sam", "contact_id": 1}, {"n": 2, "name": "Jo", "contact_id": 2}]}
```

`409 {"detail":{"error":"already_armed","session_id":"…"}}` if a session is already running.

### `GET /v1/sessions/{session_id}`

```json
{"session_id": "9a09f145-…", "state_public": "ARMED", "cmd": {"led": "off", "blink_n": 0, "challenge_id": null},
 "link_state": "OK", "profile": "demo", "mode": "hair", "who_can_be_alerted": [{"n": 1, "name": "Sam", "contact_id": 1}]}
```

### `POST /v1/sessions/{session_id}/checkin`

"I'm OK" from `/me`. Optional `{lat, lon, accuracy_m}` refreshes the last-known location, which is released
to contacts only when an alert opens. An `accuracy_m` that is not a number from 0 to 1 000 000 is dropped; the
check-in still counts.

```json
{"lat": 43.7845, "lon": -79.1867, "accuracy_m": 25}
```
```json
{"message":"Noted ✓","ok":true,"state_public":"ARMED"}
```

### `POST /v1/sessions/{session_id}/cancel`

```json
{"pin": "2468"}
```

Real PIN and duress PIN: identical status and bytes.

```json
{"message":"Cancelled ✓","ok":true,"state_public":"ARMED"}
```

Wrong PIN: `403 {"error":"wrong_pin","ok":false}`.

- **Real PIN during a prompt:** back to `ARMED`.
- **Real PIN during an alert:** `FALSE_ALARM`, and everyone notified is told the wearer cancelled with her PIN.
- **Real PIN during a duress alert:** the alert stays open; contacts are asked to confirm in person.
- **Duress PIN:** opens a masked duress alert (contacts get a silent help request) and answers as above.

### `POST /v1/sessions/{session_id}/end`

Same rules as `cancel`. Reply `{"message":"Ended ✓","ok":true,"state_public":"ENDED"}` for both PINs. With
the duress PIN the session only looks ended; its alert keeps escalating, and arming again starts a fresh
session, exactly as after a real end.

### `GET /v1/sessions/{session_id}/timeline`

The session's timeline. Duress entries stay hidden until that alert is closed.

```json
{"session_id": "9a09f145-…", "entries": [
  {"at": "2026-10-17T07:33:53.074223+00:00", "actor": "wearer", "kind": "armed", "from_state": null, "to_state": "ARMED",
   "detail": {"mode": "hair", "preset": "night_out", "profile": "demo", "interval_s": 60, "window_multiplier": 1.0}},
  {"at": "…", "actor": "demo", "kind": "pull_forward", "from_state": "ARMED", "to_state": "PROMPTED",
   "detail": {"n": 1, "level": "MED", "reason": "head_position_change", "simulated": false}}]}
```

### `GET /v1/me/export`

Every session's visible timeline. The server recomputes the per-session and account hash chains
(`chain_ok`). `head_hash` is the chain over the listed entries (each carries its `masked` flag), so it can be
recomputed from the export with `app/audit.py`; the "Ended ✓ Timeline head" message carries its first 16
characters.

```json
{"user": "Maya",
 "sessions": [{"session_id": "…", "created_at": "…", "ended_at": null, "chain_ok": true,
               "head_hash": "b8b6953c…", "entries": ["…"]}],
 "account_chain_ok": true, "account_head_hash": "51de3e74…",
 "note": "Hash-chained: any edit after export breaks the chain when it is recomputed."}
```

### `DELETE /v1/me/data`

Deletes the wearer's timeline, location snapshots, raw heart rate and raw scrunchie telemetry (with its
batch receipts), and leaves one tombstone entry. While an alert is open, the location already released to
her contacts and the alert's hidden entries stay until it closes (erase again after that);
`timeline_entries` counts the entries she could see. After an erase, `chain_ok` in the export checks each
chain from its first remaining entry.

```json
{"ok":true,"erased":{"timeline_entries":0,"hr_samples":60490,"telemetry_rows":607804}}
```

The rollups in section 8 are not erased on TimescaleDB: their 1-minute rows age out after 8 days and their
hourly rows after 90 days; daily rows are kept. On plain PostgreSQL the rollups are views over the raw rows,
so they go with them.

### `POST /v1/me/telegram-link`

One-time link (10 min) that binds the wearer's own Telegram chat for "Reminder" nudges, which the Apple
Watch mirrors as a silent tap. `t_me_link` is `null` when `TELEGRAM_BOT_USERNAME` is unset.

```json
{"t_me_link": "https://t.me/<bot>?start=s7j1qd9l…", "start_token": "s7j1qd9l…", "expires_in_s": 600}
```

### `POST /v1/contacts`

Creates a single-use invite valid for 10 minutes. `priority` (1–1000) is optional; the default is last
in line.

```json
{"name": "Sam"}
```
```json
{"contact_id": 1, "priority": 1, "web_link": "https://stillhere.example/c/wsLcGZsN…",
 "t_me_link": null, "qr_png": "data:image/png;base64,iVBORw0KGgo…", "expires_at": "2026-10-17T07:43:53.035258+00:00"}
```

### `GET /v1/contacts`

Every contact, with `status` `invited`, `pending_approval` or `approved`. `n` is the position in the
alert ladder for approved contacts.

```json
{"contacts": [{"contact_id": 1, "name": "Sam", "priority": 1, "channel": "web", "status": "pending_approval", "n": null}]}
```

### `POST /v1/contacts/{contact_id}/approve`

Contacts cannot add themselves; only the wearer approves. `404` if the contact has not joined.

```json
{"ok": true, "contact_id": 1, "n": 1, "name": "Sam"}
```

### `DELETE /v1/contacts/{contact_id}`

Silent removal: the contact is not told, and their pending messages are dropped. Reply `{"ok": true}`.

## 6. Contacts

The contact's capability token comes from `join`. Before an alert a contact sees nothing about the
wearer's session.

### `GET /c/{token}`

HTML page for contacts and judges. With an invite token it shows the consent screen ("Tap to ask to become
Contact #N"); after joining it polls `status` every second. On an alert the screen turns red and the alarm
plays; `ESCALATED_FINAL` shows the SIMULATED banner.

### `GET /v1/contact/{token}/invite`

Consent screen data for an invite token. No session data.

```json
{"valid": true, "wearer": "Maya", "n": 1, "expires_in_s": 599}
```

Used, expired and unknown invites all answer `410 {"valid": false}`.

### `POST /v1/contact/{token}/join`

Invite token → contact token. The contact is pending until the wearer approves. `410` once used or expired.

```json
{"name": "Sam"}
```
```json
{"ok": true, "status": "pending_approval", "contact_token": "w4g5m8tA…",
 "message": "Thanks. Maya needs to approve you before you become a trusted contact."}
```

### `GET /v1/contact/{token}/status`

Before an alert (or before approval), and for contacts not yet told about the current alert:

```json
{"alert": null, "approved": true}
```

During an alert this contact was told about:

```json
{"alert": {"id": 1, "kind": "NO_RESPONSE",
           "title": "Maya hasn't responded to 2 check-ins. This is not a confirmed emergency.",
           "wearer": "Maya", "level": 1, "final": false, "banner": null, "status": "OPEN",
           "handled_by": null, "you_acked": false,
           "location": {"as_of": "03:33", "age_min": 0, "link": "https://stillhere.example/l/CKEKcX1Y…", "simulated": false},
           "actions": ["ack", "pass"], "opened_at": "2026-10-17T07:33:56.059137+00:00"},
 "approved": true}
```

- `kind` is `NO_RESPONSE`, `DURESS` ("SILENT HELP REQUEST. She may not be able to talk. Don't call her
  phone.") or `LOST_CONTACT` ("We lost contact with Maya's device. Cause unknown. Her check-in schedule is
  still running.").
- `actions`:
  - `["ack", "pass"]` for the contact at the current level;
  - `["ack"]` for earlier contacts who are pinged again;
  - `["safe", "cant_reach"]` for the contact who acknowledged;
  - `[]` otherwise, and on lost-contact notices.
- At `ESCALATED_FINAL`, `final` is true and `banner` is
  "Emergency services: SIMULATED – would contact 911 / campus safety with last known location".
  Nothing is ever dialled.

### `POST /v1/contact/{token}/ack`

"I'm on it". An acknowledgement alone never closes the alert; the follow-up timer starts (20 s demo,
10 min real).

```json
{"ok": true, "result": "acked"}
```

`result` can also be `already_handled` (someone else took it on) or `ignored`; `ok` is false only for
`ignored`. `409` when there is no alert for this contact.

### `POST /v1/contact/{token}/followup`

```json
{"action": "safe"}
```

| `action` | Who | Effect | `result` |
|---|---|---|---|
| `safe` | the contact who acknowledged | Resolves the alert; everyone is told who resolved it; the location link expires 15 s (demo) or 30 min (real) later | `resolved` |
| `cant_reach` | the contact who acknowledged | Next level, or FINAL after the last contact | `escalated` |
| `pass` | the contact at the current level | "Can't help – pass it on": next level now | `passed` |

### `GET /l/{share_token}`

Last-known location released with an alert (the token is in `location.link`). Coordinates are stored
encrypted (AES-GCM) with a key derived from `APP_SECRET`. If `APP_SECRET` is unset, a random secret is kept
in the database itself, so anyone holding the database can read the coordinates and sign links: set
`APP_SECRET` whenever the database is not on this machine (the server logs a warning otherwise).

```json
{"lat": 43.7845, "lon": -79.1867, "accuracy_m": 25.0, "simulated": false, "as_of": "03:33", "age_min": 0,
 "map": "https://www.openstreetmap.org/?mlat=43.78450&mlon=-79.18670#map=17/43.78450/-79.18670",
 "expires_at": "2026-10-17T07:34:14.789116+00:00"}
```

- `expires_at` is null while the alert is open.
- After expiry: `410 {"error": "gone", "detail": "This location link has expired."}`.
- Sealed under an earlier `APP_SECRET`: `410 {"error": "gone", "detail": "This location is no longer available."}`.
- Unknown token: `404`.

## 7. Operator and demo

All need `X-Admin-Key`. Every change made here is logged with `actor = demo` and labelled as a demo
action. They act on the demo wearer (`DEMO_SEED=1`).

| Endpoint | Body | Reply |
|---|---|---|
| `GET /v1/admin/timers` | `?recent=N` (0–200) adds the last N fired timers with their lag | `{"timers":[{"id","session_id","kind","fire_at","in_s","expected_version"}],"recent_fired":[{"kind":"REMINDER_TIMEOUT","lag_s":0.331,"result":"escalated",…}]}` |
| `GET /v1/admin/overview` | — | True states, including masked alerts: `{"sessions":[{"state","public_state","link_state","version",…}],"alerts":[…],"outbox":[…]}` |
| `GET /v1/admin/outbox` | `?session_id=&contact_id=&recipient=wearer\|contact\|chat&limit=1..1000` | `{"outbox":[{"id","template","text","status","channel","attempts","dedupe_key",…}]}` |
| `POST /v1/demo/checkin-now` | — | `{"ok":true,"result":"prompted","state":"PROMPTED"}` |
| `POST /v1/demo/inject` | `{"kind":"head_position"\|"fall_like"\|"offbody"}` | `{"ok":true,"reason":"head_position_change","label":"head position change → early check-in","result":"prompted"}` |
| `POST /v1/demo/hr-source` | `{"mode":"live"\|"sim:steady"\|"sim:climb_still"\|"sim:lost"}` | `{"ok":true,"hr_source":{"mode":"sim:climb_still","since":"…"},"simulated":true}` |
| `POST /v1/demo/profile` | `{"profile":"demo"\|"real"}` | `{"ok":true,"profile":"demo"}` |
| `POST /v1/demo/fast-forward` | — | Fires the next pending deadline now: `{"ok":true,"fired_next":"PROMPT_TIMEOUT","label":"fast-forward (demo)"}` |
| `POST /v1/demo/judge-invite` | — | A fresh invite, same shape as `POST /v1/contacts` |
| `POST /v1/demo/reset` | `{"mode":"hair","hr_source":"live","arm":true}` (all optional) | Closes the demo sessions (alerts marked RESET, not resolved), clears pending messages and re-arms: `{"ok":true,"closed_sessions":1,"armed":{…}}` |

`/v1/demo/inject` can only bring a check-in forward, like a real sensor. `409` when the demo wearer has no
armed session.

## 8. Telemetry (Tiger Data)

Raw accelerometer and squeeze-sensor rows (the `R` lines of the 50 Hz RECORD mode) and heart rate, with
rollups and a z-score for a dashboard. **Nothing here changes a check-in session, opens an alert or moves a
timer.** The z-score is informational; it never brings a check-in forward. `scripts/bridge.py` does not
forward `R` lines yet, so a client posts them here.

On TimescaleDB (Tiger Cloud, or the bundled container) the raw tables are hypertables and the rollups are
real-time continuous aggregates. On plain PostgreSQL the same names are plain tables and views.
`GET /v1/telemetry/info` says which path the database took.

| `profile` | Raw rows kept | Used by |
|---|---|---|
| `night_out` (default) | 48 h | KAZE: a night out leaves no raw motion or heart-rate trail |
| `care` | 30 days | Senior mode trends |

Rollups `hr_1m` → `hr_1h` → `hr_1d` and `telemetry_1m` → `telemetry_1h` → `telemetry_1d` keep 8 days at 1 min
(the z-score baseline needs 7), 90 days at 1 h; daily rows are kept. `POST /v1/hr` stores every sample as
`night_out`.

Reads take the wearer key (own data only; another `user_id` is `403`) or `X-Admin-Key` (any `?user_id=`).
A contact token, or any other bearer, gets `401`.

### `POST /v1/telemetry/batch`

Auth: the device key plus `X-Device-Id`, or the wearer key. Stored with one `COPY` per batch.

```json
{"batch_id": "b7-000123",
 "rows": [[81000, 12, -30, 1004, 812, 640], [81020, 15, -28, 1001, 815, 642],
          [81040, 140, -60, 1250, 820, 900], [81060, 90, -20, 980, 818, 700]]}
```

| Field | Values |
|---|---|
| `rows` | 1–6000 rows of `[t, ax_mg, ay_mg, az_mg, fsr_a_mv?, fsr_b_mv?]`. Accelerometer within ±32000 mg; FSR 0–10000 mV or `null` |
| `clock` | `device_ms` (default): `t` is the device's millisecond counter, and the row at `device_ms_now` (default: the largest `t`) is placed at server time. `epoch`: Unix seconds or milliseconds |
| `device_ms_now` | the device counter at send time |
| `profile` | `night_out` (default) or `care` |
| `simulated` | default `false` |
| `batch_id` | retry key, 1–128 chars, per sender: a `batch_id` repeated by the same device (or by the wearer key without a device) is stored once |
| `device_id` | with the wearer key: one of the wearer's own devices (`403` otherwise) |
| `user_id` | optional; must match the credential (`403` otherwise) |

```json
{"ok":true,"duplicate":false,"inserted":4,"dropped":0}
{"ok":true,"duplicate":true,"inserted":4,"dropped":0}
```

- `dropped` counts rows that land outside server time − 48 h … + 60 s.
- A malformed row is `422`, for example `{"detail":"row 0: expected [t, ax_mg, ay_mg, az_mg, fsr_a_mv?, fsr_b_mv?]"}`.
- 3000 rows (60 s at 50 Hz): median 28 ms per HTTP round trip over 5 runs, against a local TimescaleDB
  2.30.2 / PostgreSQL 18.6 container.

### `GET /v1/telemetry/zscore`

The view `user_zscore_5m`: per user and metric, the mean of the last 5 minutes against the user's own
7-day baseline of 1-minute means. `z` is `null` under 30 baseline minutes. Metrics: `hr_bpm` (heart rate)
and `motion_enmo_mg` (movement: Euclidean norm minus one g, from the accelerometer). `simulated` is true
when either window holds simulated rows. Wearer: own rows. Operator: every user, or `?user_id=`.

```json
{"mode":"timescaledb","method":"plain_avg_stddev","window":"last 5 min vs the 7 days before, as 1-minute means",
 "note":"Informational only. It never changes a check-in session and never alerts anyone.",
 "rows":[{"user_id":"5beca34f-…","metric":"hr_bpm","last_bucket":"2026-10-17T08:08:00+00:00","recent_mean":91.0,
          "recent_minutes":3,"recent_samples":10,"baseline_mean":66.0,"baseline_sd":7.135,"baseline_minutes":10079,
          "z":3.5,"simulated":true,"method":"plain_avg_stddev"},
         {"user_id":"5beca34f-…","metric":"motion_enmo_mg","last_bucket":"2026-10-17T08:08:00+00:00",
          "recent_mean":57.588,"recent_minutes":2,"recent_samples":3004,"baseline_mean":16.023,"baseline_sd":8.388,
          "baseline_minutes":10079,"z":4.96,"simulated":true,"method":"plain_avg_stddev"}]}
```

`method` is `toolkit_stats_agg` when the `timescaledb_toolkit` extension is installed, else `plain_avg_stddev`.

### `GET /v1/telemetry/aggregates`

`?series=hr|motion&resolution=1m|1h|1d&hours=N&user_id=`. `hours` is at most 48 at `1m`, 744 at `1h` and
9600 at `1d` (`422` above). One user's buckets, merged across devices and profiles, oldest first. Without
`user_id` the operator reads the demo wearer.

```json
{"mode":"timescaledb","series":"hr","resolution":"1h","user_id":"5beca34f-…","buckets":[
 {"bucket":"2026-10-17T06:00:00+00:00","n":360,"mean":58.139,"sd":2.308,"min":53.0,"max":62.0,"simulated":true},
 {"bucket":"2026-10-17T07:00:00+00:00","n":360,"mean":59.669,"sd":2.457,"min":55.0,"max":65.0,"simulated":true}]}
```

`series=motion` buckets carry `n, enmo_mean, enmo_sd, enmo_max, ax_mean, ay_mean, az_mean, fsr_a_max,
fsr_b_max, simulated`.

### `GET /v1/telemetry/info`

`X-Admin-Key`. Which path this database took, versions, retention, hypertables, rollups, scheduled jobs
and compression stats (the last four on TimescaleDB only). Shortened:

```json
{"mode":"timescaledb","zscore_method":"plain_avg_stddev","note":null,"server_version":"16.15",
 "timescaledb":"2.30.2","timescaledb_toolkit":null,
 "retention":{"night_out_raw_hours":48.0,"care_raw_days":30,"rollup_1m_days":8,"rollup_1h_days":90},
 "hypertables":[{"hypertable_name":"hr_samples","num_chunks":8,"compression_enabled":true}, "…"],
 "continuous_aggregates":[{"view_name":"hr_1m","materialized_only":false,"reads_from":"hr_samples"}, "…"],
 "jobs":[{"job_id":1014,"proc_name":"stillhere_retention_job","schedule_interval":"01:00:00",
          "config":{"night_out_after":"48 hours"},"last_run_status":"Success", "…"}, "…"],
 "columnstore":[{"hypertable":"hr_samples","total_chunks":8,"number_compressed_chunks":0, "…"}, "…"]}
```

On plain PostgreSQL: `"mode":"plain"` and `"schedule":"none on plain PostgreSQL: raw rows are pruned by the app"`.
`note` holds the reason when the TimescaleDB setup failed and the plain path was taken instead.

### `POST /v1/telemetry/refresh`

`X-Admin-Key`. `{"hours": 2}` (default 46, at most 47): materializes the rollups for that range, bottom layer
first, for a backfill that landed behind the refresh policies. A no-op on plain PostgreSQL (`"refreshed":[]`).

```json
{"ok":true,"mode":"timescaledb","refreshed":["hr_1m","telemetry_1m","hr_1h","telemetry_1h"],
 "from":"2026-10-17T06:08:11.587100+00:00","to":"2026-10-17T08:08:11.587100+00:00"}
```

### `POST /v1/telemetry/synthetic`

`X-Admin-Key`. Seeds a labelled SYNTHETIC baseline (`simulated: true`, heart-rate `source` `synthetic`) so
the z-score has 7 days to compare against. Body (all optional): `user_id` (default the demo wearer; `404`
if unknown), `days` (≤ 7, default 7), `until_minutes_ago` (default 5), `profile`, `hr_every_s` (default 10),
`motion_every_s` (default 1). For demo databases only: it refreshes its whole range for every user.

```json
{"ok":true,"user_id":"5beca34f-…","from":"2026-10-10T08:02:46.589878+00:00","to":"2026-10-17T08:02:46.589878+00:00",
 "profile":"night_out","hr_rows":60480,"motion_rows":604800,"simulated":true,
 "refreshed":["hr_1m","telemetry_1m","hr_1h","telemetry_1h","hr_1d","telemetry_1d"]}
```

## 9. Senior mode (PointClickCare, synthetic)

Twenty synthetic residents × 14 days, scored by fixed rules against each resident's own baseline. Separate
from KAZE: these routes import nothing on the check-in path and write only the `care_*` tables. Every row,
card and response is labelled synthetic; every threshold is an untested assumption, and every card says so.

### `POST /v1/demo/care-seed`

`X-Admin-Key`. Replaces the cohort (20 residents × 14 days ending `end_day`, default yesterday UTC) and
scores it. Body optional: `{"seed": 11, "end_day": "2026-10-16"}`.

```json
{"ok":true,"synthetic":true,"seed":11,"as_of":"2026-10-16","residents":20,"days":14,"rows":280,
 "levels":{"low":13,"watch":1,"check today":6,"not enough data":0},"scripted_change":{"residents":7,"flagged":7},
 "no_change_scripts":{"residents":13,"flagged":0},
 "notice":"Hackathon prototype. Synthetic data only; no real residents. Rule-based change flags, not a medical device and not clinically validated. Thresholds are untested assumptions."}
```

### `GET /v1/care/residents`

Every resident with their latest score and level. `script` is the generator's ground truth, shown for
checking the rules; the rules never read it (`read_by_rules: false`). Before any seed, `residents` is empty
and `hint` names the seed endpoint.

```json
{"synthetic":true,"notice":"Hackathon prototype. …","as_of":"2026-10-16","residents":[
 {"resident_id":"R-02","display_name":"Synthetic resident R-02","synthetic":true,"days_of_data":14,"as_of":"2026-10-16",
  "score":30,"level":"check today","script":{"name":"sway_drift","note":"Standing sway rises 5% of usual a day from day 5.",
  "expected":"flag","read_by_rules":false}}, "…"]}
```

### `GET /v1/care/alerts`

The latest cards at `watch`, `check today` or `not enough data`: `check today` first, then `watch`, then
`not enough data`, then by score. `?include_low=true` returns all 20.

| Card field | Meaning |
|---|---|
| `score` | 0–100, the sum of the rule contributions (30 + 40 + 30 at most) |
| `level` | `check today` from 45 points or when any rule reaches its top band; `watch` from 15; else `low`, or `not enough data` when a rule could not score (its `suggested_check` says which device data is missing) |
| `rules[]` | `rule, observed, baseline, unit, contribution, explanation`, plus `label, scored, top_band, threshold, change_pct, recent_values, baseline_values, max_contribution, threshold_status` |
| `suggested_check` | what a care aide does next |
| `briefing` | plain-language summary of the card |
| `briefing_source` | `vultr` when Vultr Serverless Inference worded the briefing, else `template` |
| `query_ms` | server-side time of this card's own database query (its 28-day `series`), in ms; the response's `timing.list_query_ms` times the card list |
| `series` | the resident's last 28 days: `days`, `night_restless_min`, `stand_hr_delta_bpm`, `wear_hours` (usual vs now) |

The rules: `postural_sway_drift` (quiet-standing sway), `rise_to_settle_time` (time after standing up until
balance and heart rate have both settled) and `night_restlessness` (both of the last 2 nights above their
usual). Each compares the last 48 h with the resident's first 48 h in the record.

The briefing is written after the score and level are fixed and cannot change them. With `VULTR_API_KEY`
set, each `watch` / `check today` card's briefing is reworded by Vultr (section 11); `low` and `not enough data`
cards are not sent.
The text is shown only if it passes the wording rules and every number in it is on the card; otherwise,
and with no key, the card keeps the rule template. Shortened card:

```json
{"resident_id":"R-18","display_name":"Synthetic resident R-18","synthetic":true,"as_of":"2026-10-16","score":60,
 "level":"check today",
 "rules":[{"rule":"postural_sway_drift","label":"Standing sway","observed":0.113,"baseline":0.073,"unit":"m/s²",
           "change_pct":55,"contribution":30,"max_contribution":30,"top_band":true,"scored":true,
           "explanation":"Standing sway over the last 48 h: 0.113 m/s² vs 0.073 m/s² in their own 48 h baseline (+55%); at or above the +50% band, 30 points.",
           "threshold":"+20% -> 15, +35% -> 25, +50% -> 30 points; days worn at least 6 h",
           "threshold_status":"untested assumption","recent_values":[0.109,0.117],"baseline_values":[0.071,0.075]}, "…"],
 "suggested_check":"Ask the nurse to review this resident today. Watch their standing balance during transfers and walks today, with their walking aid within reach; note how they slept and any night-time waking on rounds tonight. Tell the nurse what you see.",
 "briefing":"Synthetic resident R-18: check today, score 60/100 as of 2026-10-16. Standing sway over the last 48 h: … Rule-based flag on synthetic data; not a medical assessment.",
 "briefing_source":"template",
 "method":"deterministic rules against the resident's own baseline; no model","thresholds_status":"untested assumption",
 "recent_window":{"from":"2026-10-15","to":"2026-10-16","hours":48},
 "baseline_window":{"from":"2026-10-03","to":"2026-10-04","hours":48,"note":"their own first 48 h in this 14-day record"},
 "data_quality":{"days_present":4,"days_expected":4,"wear_hours":55.3,"all_rules_scored":true}, "…": "…"}
```

### `POST /v1/care/alerts/{resident_id}/feedback`

A button on a flagged card (`actions[]`): `useful`, `not_useful`, `already_known` (optional `reason`, at
most 200 characters) or `snooze_3d`. Stored against the resident's latest card. `snooze_3d` mutes pushes
for that resident for 3 days. Unknown resident: `404`; unknown action or a longer reason: `422`.

```json
{"ok":true,"synthetic":true,"resident_id":"R-08","action":"snooze_3d","as_of":"2026-10-16",
 "snooze_until":"2026-10-20T10:59:41.120+00:00"}
```

### `GET /v1/care/feedback`

How often each button was pressed, for tuning the rules on synthetic data.

```json
{"synthetic":true,"notice":"Hackathon prototype. …","counts":{"useful":2,"not_useful":1,"already_known":0,"snooze_3d":1}}
```

### `GET /v1/care/digest`

The shift-start digest: the latest flagged cards that were not pushed, each with the reason. A card is
pushed at most once per resident per 24 h; an identical alert (same level and rules) is muted for 3 days
unless its level or score goes up; a resident snoozed with the feedback button goes here too. Every card
on `/v1/care/alerts` carries `delivery: {channel: "push" | "digest" | null, reason}`.

```json
{"synthetic":true,"as_of":"2026-10-16","budget":{"push_per_resident_hours":24,"identical_muted_days":3,
 "source":"Ancker 2017 (…); Joint Commission SEA 50 (2013, recommendation 4); Rantz 2017 (…)","status":"untested assumption"},
 "items":[{"resident_id":"R-08","display_name":"Synthetic resident R-08","level":"review this shift","score":100,
  "reason":"1 push per resident per 24 h; the last one was at 2026-10-17 08:51 UTC","suggested_check":"…","briefing":"…"}]}
```

## 10. Health

### `GET /v1/health`

No auth. `200` when the database answers, `503` with `"db":"down"` otherwise. The Docker healthcheck uses it.

```json
{"ok": true, "db": "up", "server_time": "2026-10-17T07:33:53.026210+00:00", "telegram": "off (log channel)",
 "default_profile": "demo", "emergency_services": "SIMULATED (never placed)",
 "rule_thresholds": {"status": "untested engineering assumptions",
  "values": {"HR_ABS_DELTA_BPM": 30.0, "HR_REL_DELTA": 0.35, "STILL_ENMO_MG": 45.0, "VIGOROUS_ENMO_MG": 100.0,
             "VIGOROUS_SUPPRESS_S": 600.0},
  "notes": {"HR_ABS_DELTA_BPM": "untested engineering assumption; no peer-reviewed data to tune it against (no dancing heart-rate data)", "…": "…"}}}
```

`rule_thresholds` lists the night-out sensor and heart-rate thresholds. None has a published source to tune
it against; they only decide when to ask sooner.

With a valid `X-Admin-Key` it also returns `pending_timers` and `pending_outbox`. They are not public: a duress
PIN adds escalation timers and contact messages, so public counts would tell it apart from the real PIN.

## 11. Outbound services

These are not endpoints of this server; they are listed so the data flows are in one place.

| Service | Used for | Without a key |
|---|---|---|
| Telegram Bot API (long polling) | Wearer "Reminder" nudges (mirrored to the Apple Watch), contact alerts with "I'm on it" / "pass it on" / "she's safe" buttons | Messages go to the log channel; contacts use the web page |
| Vultr Serverless Inference, `POST https://api.vultrinference.com/v1/chat/completions` | Wording the PointClickCare care-team briefing only (`app/vultr.py`: `briefing_text(card)`, async `abriefing_text(card)`; `GET /v1/care/alerts` calls the async one). Sends only rule findings, never names or ids; shows the text only if it passes the wording rules and every number in it is on the card | `template_briefing(card)`, deterministic; on `/v1/care/alerts` the card keeps the rule template |

No KAZE check-in, escalation or alert path calls a model. `tests/test_vultr.py` checks that no escalation
module imports `app.vultr`.
