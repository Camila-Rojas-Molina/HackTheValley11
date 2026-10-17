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
- **Input the database cannot store** (a NUL character in text, a number out of range, an `X-Seq` outside
  0 to 2^63 − 1): `422`, never a `500`, so a device or bridge does not retry it forever. Nothing is stored.
- **Canonical JSON:** device replies and the wearer's PIN and check-in replies are serialised with sorted keys
  and no spaces, so equal replies are equal bytes.
- **Byte-identical PINs:** the real PIN and the duress PIN produce the same status code and the same bytes on
  `cancel` and `end`. The duress alert is masked: the device and the wearer see `state_public` as `ARMED`,
  or `ENDED` after `end`.
- **Masked duress is a whole twin session.** After a duress PIN or a duress squeeze (in any active state,
  during an alert too) every wearer read answers as the session she would have had with the OK action (the
  real PIN, a plain squeeze): replies, device `cmd`, status and `link_state`, timeline, export,
  `GET /v1/me/alerts` and her Reminders messages. The twin keeps its own check-ins, its own alert (the one she
  could already see, as the OK action left it, or one its own unanswered check-in opens), its own ladder and
  its own lost-contact notice. The escalation runs underneath and is hidden until a contact confirms she is
  safe.
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
- **Challenge stamp:** a squeeze carries the `challenge_id` of the prompt the device was showing when it
  arrived (none when no prompt was showing). A buffered squeeze answers a prompt only if it carries that
  prompt's `challenge_id`: one made before the prompt was issued answers nothing (`result: "ignored"`, a
  `stale_squeeze` timeline entry, no retry used). A live squeeze answers as before.
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
| `squeeze` | `{pattern: plain\|duress\|unrecognized, k, buffered?, challenge_id?}` | Answers a prompt (`k` must equal the challenge count, 2 for a routine prompt). Wrong `k` gets one retry with a new count. `duress` opens a masked alert, or turns the open alert into a masked duress alert. Otherwise, during an alert, it is recorded only. |
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
A duress squeeze returns exactly the bytes a plain squeeze would have returned, and from then on the session
answers like one that got the plain squeeze (see "Masked duress is a whole twin session" above): a later PIN
reads `ARMED` / `ENDED` with the LED off exactly as it would after a plain squeeze.

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
default 300), real samples only unless `allow_simulated` is true. Needs at least 5 independent readings,
counted as one per 5-second slot (`409` otherwise): the Watch gives a new value about every 5 s while
HeartCast notifies about once a second, so repeated notifications are one reading. The median takes one
value per slot.

```json
{"window_s": 300}
```
```json
{"ok": true, "baseline_bpm": 71.0, "samples": 60, "readings": 12, "simulated": false}
```

### `GET /v1/hr/status`

Device key or wearer key. Display only: `"HR live"`, or `"HR stale"` when no sample arrived for 15 s
(`"no heart rate yet"` before the first). Stale or missing heart rate never delays a check-in; after the
profile's `hr_lost_s` (120 s) without heart rate the next check-in is brought forward instead. `sources`
lists every source that posted in the last minute; `duplicate_sources` is true when two bridges post for
the same wearer (run only one: the bleak bridge, the Pulsoid bridge or `/hr-bridge`).

```json
{"label":"HR stale","stale":true,"stale_after_s":15,"display_only":true,"last_at":"2026-10-17T11:02:41.5+00:00",
 "age_s":21.3,"sources":[{"source":"heartcast-ble","samples_60s":39,"last_at":"2026-10-17T11:02:41.5+00:00",
 "simulated":false}],"duplicate_sources":false,
 "note":"Display only. Stale or missing heart rate never delays a check-in; …"}
```

### `GET /hr-bridge`

HTML page. Chrome Web Bluetooth reads the standard Heart Rate Service (0x180D, characteristic 0x2A37)
that HeartCast broadcasts from the Apple Watch, and posts to `/v1/hr` from the notification handler. It has
a SIM button that streams `simulated: true`. Open it on `localhost` or over HTTPS (Web Bluetooth needs a
secure context).

### Headless heart-rate bridges

Two scripts post into the same `POST /v1/hr` with the wearer key (`WEARER_KEY`, `STILLHERE_URL`). Neither
is tested here against real hardware or a Pulsoid account; their parsers and posting path are unit-tested.

| Script | Source | Path |
|---|---|---|
| `scripts/hr_bleak.py` | `heartcast-ble` | HeartCast's Heart Rate Service read with `bleak` on the Mac (primary). Pin the phone with `HR_DEVICE=<uuid>`. |
| `scripts/hr_pulsoid.py` | `pulsoid-ws` | Pulsoid's WebSocket `wss://dev.pulsoid.net/api/v1/data/real_time`, token `PULSOID_TOKEN` in the `Authorization` header (backup, no Mac Bluetooth). |

Run only one of them, and not with `/hr-bridge`: each refuses to start while another source posted in the
last 15 s (`GET /v1/hr/status`), unless given `--force`. HeartCast notifies about once a second while the
Watch updates about every 5 s, so the server never counts repeated notifications as separate readings.

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
| `device_id` | one of the wearer's own devices (`404 {"detail":"no such device"}` otherwise); default: her first device |

```json
{"session_id": "9a09f145-…", "state_public": "ARMED", "profile": "demo", "interval_s": 60,
 "end_at": "2026-10-17T13:33:53.072794+00:00",
 "who_can_be_alerted": [{"n": 1, "name": "Sam", "contact_id": 1}, {"n": 2, "name": "Jo", "contact_id": 2}]}
```

`409 {"detail":{"error":"already_armed","session_id":"…"}}` if a session is already running, also when two arm
requests arrive at once (one gets `200`, the other `409`).

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
- **Duress PIN:** opens a masked duress alert (contacts get a silent help request) and answers as above. On an
  alert she can already see, that alert stays hers and reads exactly as the real PIN left it (`FALSE_ALARM`);
  the duress escalation continues on its own, hidden alert.

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

### `GET /v1/me/alerts`

Wearer key. Her record of every alert: who was alerted (`alerted`, with when each contact was first told)
and who acknowledged or answered (`acknowledged`: `ack`, `pass`, `safe`, `cant_reach`, with the source and
the result). Newest first. Masking follows the timeline: an open masked duress alert is left out until it
closes, and in its place she sees the twin's alert, as the real PIN or a plain squeeze would have left it
(the alert she could already see keeps its id). So after a duress PIN or a duress squeeze the record reads
exactly as after the real PIN or a plain squeeze, and contacts told only about the hidden duress alert do not
appear in it until it closes.

```json
{"alerts":[{"acknowledged":[{"action":"ack","at":"2026-10-17T11:20:03.1+00:00","contact_id":12,"name":"Sam",
  "result":"acked","source":"web"}],"alert_id":7,"alerted":[{"contact_id":12,"first_notified_at":"2026-10-17T11:19:51.0+00:00",
  "name":"Sam"}],"closed_at":null,"final":false,"kind":"NO_RESPONSE","level":1,"opened_at":"2026-10-17T11:19:51.0+00:00",
  "resolved_by":null,"session_id":"…","status":"ACKED"}]}
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
alert ladder for approved contacts. `duress_eligible` and `duress_recipients` show who a duress alert can
reach (see `PUT /v1/contacts/{contact_id}/duress`).

```json
{"contacts": [{"contact_id": 1, "name": "Sam", "priority": 1, "channel": "web", "status": "approved", "n": 1,
               "duress_eligible": false},
              {"contact_id": 2, "name": "Alex", "priority": 2, "channel": "web", "status": "approved", "n": 2,
               "duress_eligible": true}],
 "duress_recipients": {"mode": "chosen", "contact_ids": [2]}}
```

### `PUT /v1/contacts/{contact_id}/duress`

Wearer key. `{"eligible": true}` adds a contact to the duress recipients; `false` takes them off. A duress
alert (duress squeeze or duress PIN) escalates only through the chosen duress recipients, in ladder order.
**If the wearer chooses none, every approved contact can receive a duress alert** (`duress_recipients.mode`
is `"all approved contacts (none chosen)"`). A contact kept out of the list is never told about a duress
alert and cannot take, pass on or close it; a duress contact can take over an "I'm on it" they held. They
see the twin instead (see "Masked duress is a whole twin session"): exactly the messages, page and buttons
they would have had if she had used the real PIN or a plain squeeze. For example, told about a no-response
alert that then turned into duress, they get "False alarm: … cancelled with her PIN" after the duress PIN,
and their location link stops working as it would after a real false alarm. `404` for a contact that is not
the wearer's.

```json
{"ok": true, "contact_id": 2, "duress_eligible": true}
```

### `POST /v1/contacts/{contact_id}/approve`

Contacts cannot add themselves; only the wearer approves. `404` if the contact has not joined.

```json
{"ok": true, "contact_id": 1, "n": 1, "name": "Sam"}
```

### `DELETE /v1/contacts/{contact_id}`

Silent removal: the contact is not told, their pending messages are dropped (a message already being sent
when they are removed is not retried if that send fails), and their location links stop working (`410`); the
other contacts keep theirs. Reply `{"ok": true}`.

### `DELETE /v1/contacts`

Wearer key. Removes every contact (approved, pending and invited) in one action, silently, like Apple
Safety Check's emergency reset: nobody is told, nothing still queued or in flight for them is sent, and every
location link they hold stops working. An open alert is
not stood down: with nobody left, the ladder reaches the simulated FINAL step (no call is placed).

```json
{"ok": true, "removed": 3}
```

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
| `cant_reach` | the contact who acknowledged | Next level, or FINAL after the last contact. Back at FINAL, the final alert goes to everyone again only while the renotify budget lasts (10 per alert at FINAL), and a contact who takes it on again is not announced again, so one contact cannot flood the others | `escalated` |
| `pass` | the contact at the current level | "Can't help – pass it on": next level now | `passed` |

### `GET /l/{share_token}`

Last-known location released with an alert (the token is in `location.link`). Each contact gets a link of
their own (the same one in their alert messages and on their page), so removing a contact ends theirs
without touching anyone else's. Coordinates are stored
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
- Unknown token: `404`, which is also the answer once retention has deleted an expired link.
- No location history: each new location replaces every earlier one that no alert released. Real
  locations are deleted 48 h after capture (the timer worker checks every 10 minutes) unless an open alert
  still holds them; links that expired more than 48 h ago are deleted too. The simulated demo location is
  not personal data and is kept.

## 7. Operator and demo

All need `X-Admin-Key`. Every change made here is logged with `actor = demo` and labelled as a demo
action. They act on the demo wearer (`DEMO_SEED=1`).

| Endpoint | Body | Reply |
|---|---|---|
| `GET /v1/admin/timers` | `?recent=N` (0–200) adds the last N fired timers with their lag | `{"timers":[{"id","session_id","kind","fire_at","in_s","expected_version"}],"recent_fired":[{"kind":"REMINDER_TIMEOUT","lag_s":0.331,"result":"escalated",…}]}` |
| `GET /v1/admin/overview` | — | True states, including masked alerts: `{"sessions":[{"state","public_state","link_state","public_link_state","version",…}],"alerts":[…],"cover_alerts":[…],"outbox":[…]}`; `cover_alerts` are the twins' alerts of masked sessions |
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

- `dropped` counts rows that land outside server time − 47 h … + 60 s (an hour inside the 48 h `night_out`
  window, so every accepted row can still be refreshed into the rollups).
- A backlog (rows older than the 1-minute refresh policies' 2-hour window) is refreshed into the 1 min, 1 h
  and 1 d rollups by the batch call itself on TimescaleDB, so every accepted row reaches them. The batch is
  committed first: if that refresh fails it is logged, the reply still reports the rows as stored, and
  `POST /v1/telemetry/refresh` can redo it.
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

### `GET /v1/telemetry/gaps`

Wearer key (own data) or `X-Admin-Key` (`?user_id=`, default the demo wearer). `?hours=` up to 168, default
24. A read-only "device sent no data" timeline for the care team (care profile): a sample counts as data for
60 s, and every longer silence is a dead range. The server's check-in countdown stays the authority; this
view never changes a session. With TimescaleDB and Toolkit 1.15+ it reads an hourly real-time continuous
aggregate of `heartbeat_agg` with `rollup()` and `live_ranges()` (`method: toolkit_heartbeat_agg`, verified on
TimescaleDB 2.30.2 + Toolkit 1.26.0): everything in the window outside the live ranges is dead, including the
silence before the first hour with data and after the last one, and the last sample keeps its 60 s even past
an hour boundary. Otherwise it computes the same ranges with `lead()` over the raw rows (`method: plain_lag`).
Contact tokens have no access.

```json
{"user_id":"…","label":"device sent no data","method":"toolkit_heartbeat_agg","liveness_s":60.0,"profile":"care",
 "from":"2026-10-17T10:23:26+00:00","to":"2026-10-17T11:23:26+00:00",
 "dead_ranges":[{"start":"2026-10-17T10:23:26+00:00","end":"2026-10-17T10:43:26+00:00","seconds":1200.0,
                 "start_s_ago":3600.0,"end_s_ago":2400.0}, "…"],
 "num_gaps":3,"downtime_s":1980.0,"note":"Read-only, for the care team (care profile). …"}
```

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

`connections` is the most this API process can hold: the pool (`DB_POOL_MAX`, default 8), the Telegram
poller's lock connection (only with a bot token) and the migration runner at start-up; the scripts talk HTTP
and open none. A free Tiger service allows 25 connections and has no pooler, so keep `total_max` well under it.

```json
"connections":{"pool_max":8,"telegram_lock":1,"migrations_at_start":1,"total_max":10,"scripts":0,
               "free_tier_max_connections":25,"server_max_connections":25}
```

### `POST /v1/telemetry/refresh`

`X-Admin-Key`. `{"hours": 2}` (default 46, at most 47): materializes the rollups for that range, bottom layer
first, for a backfill that landed behind the refresh policies. A no-op on plain PostgreSQL (`"refreshed":[]`).

```json
{"ok":true,"mode":"timescaledb","refreshed":["hr_1m","telemetry_1m","hr_1h","telemetry_1h"],
 "from":"2026-10-17T06:08:11.587100+00:00","to":"2026-10-17T08:08:11.587100+00:00"}
```

### `POST /v1/telemetry/compression-demo`

`X-Admin-Key`. A compression ratio that is actually measured. The columnstore policy only converts chunks
older than a day on a daily schedule, so a weekend demo compresses nothing on its own. This writes `days`
(1–6, default 3) of SYNTHETIC care-profile telemetry (`motion_every_s`, default 1 Hz; heart rate every
`hr_every_s`, default 5 s) ending 25 h ago, calls `convert_to_columnstore` on every chunk older than a day,
and reads the bytes back from `hypertable_columnstore_stats`. The care profile keeps raw rows 30 days
(night_out only 48 h). The rollups are not refreshed over this backdated range. On plain PostgreSQL it
writes nothing and answers `"available": false`. Body optional: `{"user_id": "…", "days": 3}`.

Measured on TimescaleDB 2.30.2 (Docker, `timescale/timescaledb-ha:pg18`) with the defaults:

```json
{"mode":"timescaledb","available":true,"synthetic":true,"profile":"care",
 "rows":{"hr_rows":51840,"motion_rows":259200,"simulated":true},
 "tables":[{"hypertable":"scrunchie_telemetry","total_chunks":4,"number_compressed_chunks":3,
            "before_compression_total_bytes":46055424,"after_compression_total_bytes":4923392,"ratio":9.4,
            "chunks_converted":3},
           {"hypertable":"hr_samples","total_chunks":4,"number_compressed_chunks":3,
            "before_compression_total_bytes":8609792,"after_compression_total_bytes":253952,"ratio":33.9,
            "chunks_converted":3}], "…": "…"}
```

The ratio depends on the data: these rows come from the synthetic generator, not from a person.

### `POST /v1/telemetry/synthetic`

`X-Admin-Key`. Seeds a labelled SYNTHETIC baseline (`simulated: true`, heart-rate `source` `synthetic`) so
the z-score has 7 days to compare against. Body (all optional): `user_id` (default the demo wearer; `404`
if unknown), `days` (≤ 7, default 7), `until_minutes_ago` (default 5), `profile`, `hr_every_s` (default 10),
`motion_every_s` (default 1). For demo databases only: it refreshes its whole range for every user. It can be
run again over chunks the columnstore has already compressed (the transaction lifts TimescaleDB's
per-transaction decompression limit to replace the earlier synthetic rows).

```json
{"ok":true,"user_id":"5beca34f-…","from":"2026-10-10T08:02:46.589878+00:00","to":"2026-10-17T08:02:46.589878+00:00",
 "profile":"night_out","hr_rows":60480,"motion_rows":604800,"simulated":true,
 "refreshed":["hr_1m","telemetry_1m","hr_1h","telemetry_1h","hr_1d","telemetry_1d"]}
```

## 9. Senior mode (PointClickCare, synthetic)

Twenty synthetic residents × 28 days, scored by fixed rules against each resident's own rolling baseline.
Separate from KAZE: these routes import nothing on the check-in path and write only the `care_*` tables.
Every row, card and response is labelled synthetic; every threshold is an untested assumption with its
source on the card. Pattern change only: the cards never name a condition and never look ahead.

### `POST /v1/demo/care-seed`

`X-Admin-Key`. Replaces the cohort's data (20 residents × 28 days ending `end_day`, default yesterday UTC) and
scores it. Body optional: `{"seed": 11, "end_day": "2026-10-16", "reset": false}`. The residents' push history
and feedback carry over, so seeding the next day applies the 24 h push budget, the 3-day mute and a
"Snooze 3 days" pressed on the day before; `"reset": true` forgets them too.

```json
{"ok":true,"synthetic":true,"seed":11,"as_of":"2026-10-16","residents":20,"days":28,"rows":560,
 "levels":{"low":15,"worth a look this shift":3,"review this shift":2,"not enough data":0},
 "scripted_change":{"residents":5,"flagged":5},"no_change_scripts":{"residents":15,"flagged":0},
 "notice":"Hackathon prototype. Synthetic data only; no real residents. Rule-based change flags, not a medical device and not clinically validated. Thresholds are untested assumptions."}
```

### `GET /v1/care/residents`

Every resident with their latest change score and level. `script` is the generator's ground truth, shown
for checking the rules; the rules never read it (`read_by_rules: false`). Before any seed, `residents` is
empty and `hint` names the seed endpoint.

```json
{"synthetic":true,"notice":"Hackathon prototype. …","as_of":"2026-10-16","residents":[
 {"resident_id":"R-05","display_name":"Synthetic resident R-05","synthetic":true,"days_of_data":28,"as_of":"2026-10-16",
  "score":55,"level":"worth a look this shift","script":{"name":"hr_response_flatter",
  "note":"Heart rate rises about 12 bpm less than usual after standing on the last 2 days.","expected":"flag",
  "read_by_rules":false}}, "…"]}
```

### `GET /v1/care/alerts`

The latest flagged cards: `review this shift` first, then `worth a look this shift`, then `not enough data`,
then by change score. `?include_low=true` returns all 20.

**Rules** (scored; each compares the resident with their own 14 days just before the last 48 h, and needs 7
valid days in that baseline before it can score):

- `stand_hr_response`, heart-rate change after standing (when available): per stand-up, mean heart rate
  60–180 s after standing minus the 60 s before; the daily median over stand-ups that had heart rate (3+ a
  day). Both of the last 2 days at z ≤ −2.5 or ≥ +2.5 in the same direction (SD at least 2 bpm). Off for a
  resident whose record notes non-sinus rhythm or a pacemaker. Heart rate alone cannot show blood pressure,
  so the suggested check is lying and standing vitals per the home's protocol.
- `night_restlessness`: minutes moving at night. Both of the last 2 nights at z ≥ +2.5, at least +15% and at
  least 15 min above the usual (one bad night scores nothing).

Points follow the smaller z of the 2 days: 2.5 SD gives 30 / 25, 4 SD gives the maximum 55 / 45.
**Experimental, not scored** (shown greyed out): `postural_sway_drift` and `settle_balance` (sway is not
validated from a wrist or hair tie, and traditional sway measures were not linked to the outcome in 738 older
adults).

**Levels** never follow the score: `worth a look this shift` when one rule fires today (even at its top
band); `review this shift` only when 2 different rules fired in the last 3 days, one of them today; a
medication start or change in the last 30 days (antihypertensive or an AGS Beers 2023 class) raises a
`worth a look` card one level and never starts one; `low` otherwise, or `not enough data` when nothing fired
and a rule could not score for lack of device data. Heart rate is "when available": a worn device whose
stand-ups had too little heart rate leaves the card `low` (the heart-rate rule shows `not enough data` and
`data_quality.heart_rate` says how often it was available). The medication raise is listed on the card under
`thresholds.medication` (30-day window and the classes), an untested assumption like the other thresholds.

**Delivery:** at most 1 push per resident per 24 h; an identical alert (same level and rules) is muted for
3 days unless it gets worse; the rest goes to the shift-start digest (`GET /v1/care/digest`).

| Card field | Meaning |
|---|---|
| `score`, `score_label`, `score_note` | the 0–100 change score: the sum of today's rule points (55 + 45 at most). It ranks change from the resident's own usual; it is not a risk score and does not set the level |
| `level` | see above |
| `rules[]` | `rule, label, status (scored / not enough data / off), observed, baseline, baseline_sd, z, unit, contribution, max_contribution, explanation, threshold, threshold_status, source, recent_values, baseline_values, baseline_days` |
| `experimental[]` | greyed-out signals: `signal, label, status: "experimental, not scored", observed, baseline, change_pct, note` |
| `fired_last_3_days` | each rule that fired in the last 3 days, with the dates |
| `data_quality` | `hours_worn_last_48h`, `gaps` (dates with no data), `heart_rate` (`available on N of 16 days` or `not available`), `days_present`, `all_rules_scored` |
| `record_context`, `priority_raised_by` | synthetic medication starts or changes in the last 30 days, and whether they raised the level |
| `suggested_check` | what a care aide does next |
| `pattern_note` | "Pattern change only - not a medical assessment." |
| `actions`, `feedback_url` | the Useful / Not useful / Already known / Snooze 3 days buttons (flagged cards only) |
| `delivery` | `{channel: "push" / "digest" / null, reason}` |
| `briefing`, `briefing_source` | plain-language summary with the detail in the message itself; `vultr` when Vultr worded it, else `template` |
| `query_ms` | server-side time of this card's own database query (its 28-day `series`), in ms; the response's `timing.list_query_ms` times the card list |
| `series` | the resident's last 28 days: `days`, `night_restless_min`, `stand_hr_delta_bpm`, `wear_hours` (usual vs now) |

The briefing is written after the score and level are fixed and cannot change them. With `VULTR_API_KEY`
set, each flagged card's briefing is reworded by Vultr (section 11); `low` and `not enough data` cards are
not sent. The text is shown only if it passes the wording rules and every number in it is on the card;
otherwise, and with no key, the card keeps the rule template. The model never sees the data-quality line or
the record context, so the template's sentences for both (including "This raised the priority one level.")
follow its text. Shortened card:

```json
{"resident_id":"R-08","display_name":"Synthetic resident R-08","synthetic":true,"as_of":"2026-10-16","score":100,
 "score_label":"change score (0-100)","level":"review this shift",
 "rules":[{"rule":"stand_hr_response","label":"Heart-rate change after standing (when available)","status":"scored",
           "observed":1.4,"baseline":13.7,"baseline_sd":2.0,"z":[-7.0,-5.4],"unit":"bpm","contribution":55,
           "max_contribution":55,"explanation":"Heart rate rose less than usual after standing (avg +1.4 vs their usual +14 bpm over the 14-day baseline, SD 2.0; z -7.0 and -5.4; 12 and 8 stands with heart rate). Many things can cause this, including medications or watch fit; 55 points.",
           "threshold_status":"untested assumption","source":"CDC STEADI 2017 (…); Norcliffe-Kaufmann 2018 (…); Falter 2019 (…)"}, "…"],
 "experimental":[{"signal":"postural_sway_drift","label":"Standing sway (experimental, not scored)",
                  "status":"experimental, not scored","observed":0.102,"baseline":0.08,"change_pct":27,"contribution":0}, "…"],
 "fired_last_3_days":{"stand_hr_response":["2026-10-16"],"night_restlessness":["2026-10-16"]},
 "data_quality":{"hours_worn_last_48h":27.4,"gaps":[],"heart_rate":{"available_days":16,"of_days":16,
                 "label":"available on 16 of 16 days"},"days_present":16,"days_expected":16,"all_rules_scored":true},
 "suggested_check":"Ask the nurse to review this resident this shift. If they seem unsteady or dizzy, consider lying and standing vitals per your protocol (lying 5 min; blood pressure and pulse at 1 and 3 min standing); note how they slept and any night-time waking on rounds tonight. Tell the nurse what you see.",
 "pattern_note":"Pattern change only - not a medical assessment.",
 "delivery":{"channel":"push","reason":"first alert for this resident in 24 h"},
 "baseline_window":{"from":"2026-10-01","to":"2026-10-14","days":14,"note":"rolling: their own 14 days just before the last 48 h; …"},
 "briefing_source":"template","query_ms":0.41, "…": "…"}
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

With a valid `X-Admin-Key` it also returns `pending_timers`, `pending_outbox` and `failing_timers`. They are
not public: a duress PIN adds escalation timers and contact messages, so public counts would tell it apart from
the real PIN. `failing_timers` counts deadlines whose transition raised an error: a timer is never dropped
after errors; it is retried (backoff capped at 30 s) until it fires, and `GET /v1/admin/timers` shows its
`attempts` and `last_error`.

## 11. Outbound services

These are not endpoints of this server; they are listed so the data flows are in one place.

| Service | Used for | Without a key |
|---|---|---|
| Telegram Bot API (long polling) | Wearer "Reminder" nudges (mirrored to the Apple Watch), contact alerts with "I'm on it" / "pass it on" / "she's safe" buttons | Messages go to the log channel; contacts use the web page |
| Vultr Serverless Inference, `POST https://api.vultrinference.com/v1/chat/completions` | Wording the PointClickCare care-team briefing only (`app/vultr.py`: `briefing_text(card)`, async `abriefing_text(card)`; `GET /v1/care/alerts` calls the async one). Sends only rule findings, never names or ids; shows the text only if it passes the wording rules and every number in it is on the card | `template_briefing(card)`, deterministic; on `/v1/care/alerts` the card keeps the rule template |

No KAZE check-in, escalation or alert path calls a model. `tests/test_vultr.py` checks that no escalation
module imports `app.vultr`.
