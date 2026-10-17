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
8. [Health](#8-health)
9. [Outbound services](#9-outbound-services)

## 1. Auth and conventions

| Caller | Credential | Errors |
|---|---|---|
| Device / bridge | `Authorization: Bearer <DEVICE_KEY>` and `X-Device-Id: <id>`; also `X-Boot-Id` and `X-Seq` | `401 {"detail":"device key and X-Device-Id required"}`, `401 {"detail":"unknown device or wrong key"}` |
| Wearer (`/me`) | `Authorization: Bearer <WEARER_KEY>` | `401 {"detail":"wrong wearer key"}` |
| Contact | the capability token in the URL path; it cannot arm a session, see the session or ask for a location | `404`, `410` |
| Operator | `X-Admin-Key: <ADMIN_KEY>` | `401 {"detail":"wrong X-Admin-Key"}`; `503` when `ADMIN_KEY` is unset |

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
| `enmo60_mg` | Mean ENMO over the last 60 s, in milli-g (≥ 0). Below 45 mg counts as still. |
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
the check-in forward, once per gap.

### `POST /v1/hr`

Auth: the wearer key, or the device key plus `X-Device-Id`. `user_id` is optional and must match the
credential (`403` otherwise).

```json
{"source": "apple_watch_heartcast", "simulated": false,
 "samples": [{"t": 1792222433.1, "bpm": 71, "rr_ms": [845.7], "rr_synthetic": false}]}
```

or a single sample: `{"ts": "2026-10-17T07:33:53Z", "bpm": 72, "source": "apple_watch_heartcast", "simulated": false}`.

- `bpm` 20–250; up to 600 samples per call.
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
to contacts only when an alert opens.

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
the duress PIN the session only looks ended; its alert keeps escalating.

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

Every session's visible timeline. The server recomputes the per-session and account hash chains.

```json
{"user": "Maya",
 "sessions": [{"session_id": "…", "created_at": "…", "ended_at": null, "chain_ok": true,
               "head_hash": "b8b6953c…", "entries": ["…"]}],
 "account_chain_ok": true, "account_head_hash": "51de3e74…",
 "note": "Hash-chained: any edit after export breaks the chain when it is recomputed."}
```

### `DELETE /v1/me/data`

Deletes the wearer's timeline, location snapshots and raw heart rate, and leaves one tombstone entry.

```json
{"ok": true, "erased": {"timeline_entries": 32, "hr_samples": 7}}
```

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
encrypted.

```json
{"lat": 43.7845, "lon": -79.1867, "accuracy_m": 25.0, "simulated": false, "as_of": "03:33", "age_min": 0,
 "map": "https://www.openstreetmap.org/?mlat=43.78450&mlon=-79.18670#map=17/43.78450/-79.18670",
 "expires_at": "2026-10-17T07:34:14.789116+00:00"}
```

- `expires_at` is null while the alert is open.
- After expiry: `410 {"error": "gone", "detail": "This location link has expired."}`.
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

## 8. Health

### `GET /v1/health`

No auth. `200` when the database answers, `503` with `"db":"down"` otherwise. The Docker healthcheck uses it.

```json
{"ok": true, "db": "up", "server_time": "2026-10-17T07:33:53.026210+00:00", "pending_timers": 0,
 "pending_outbox": 0, "telegram": "off (log channel)", "default_profile": "demo",
 "emergency_services": "SIMULATED (never placed)"}
```

## 9. Outbound services

These are not endpoints of this server; they are listed so the data flows are in one place.

| Service | Used for | Without a key |
|---|---|---|
| Telegram Bot API (long polling) | Wearer "Reminder" nudges (mirrored to the Apple Watch), contact alerts with "I'm on it" / "pass it on" / "she's safe" buttons | Messages go to the log channel; contacts use the web page |
| Vultr Serverless Inference, `POST https://api.vultrinference.com/v1/chat/completions` | Wording the PointClickCare care-team briefing only (`app/vultr.py`: `briefing_text(card)`, async `abriefing_text(card)`). Sends only rule findings, never names or ids; shows the text only if it passes the wording rules and every number in it is on the card | `template_briefing(card)`, deterministic |

No KAZE check-in, escalation or alert path calls a model. `tests/test_vultr.py` checks that no escalation
module imports `app.vultr`.
