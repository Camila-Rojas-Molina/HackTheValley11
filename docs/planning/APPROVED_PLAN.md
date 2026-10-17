# StillHere: backend push to `main` (HTV11), aligned with the team doc's Outline tab

## Context

**StillHere** is a smart scrunchie, worn on the wrist or in the hair.
- **Check-ins:** squeeze check-ins, plus a silent duress squeeze that looks like a cancel.
- **The prompt:** the check-in reaches the wearer as a silent tap from the Apple Watch, via a Telegram "Reminders" notification. There is no vibration motor or buzzer.
- **Sensors only bring a check-in forward:** the accelerometer (head position) and the Apple Watch heart rate, streamed live by the HeartCast iPhone app.
- **Escalation:** if she doesn't answer, a server-side countdown escalates through trusted contacts until a person takes responsibility.

**Team decisions:**
- **Cami:** backend only, scoped to the Google Doc's Outline tab and the KAZE challenge description.
- **Marco:** push straight to `main` of `camila-rojas-molina/hackthevalley11`. It is cloned at `/home/user/hackthevalley11` and holds only `.gitignore`.
- **Timing:** the team sleeps after approval, and I build and push while they sleep.

**Fixed facts:**
- The Fitbit is an Inspire 3: no live heart rate, so it's out.
- The Google Health and Apple Health apps only update on sync and are not live. Heart-rate history from them is optional, for the senior-mode slide.
- It is ~02:00 Sat. The final deadline is Sun 08:00 ET.

**Sources for the design:**
- The red-teamed build plan at `/tmp/claude-0/-home-user-claimflow-workspace/6862b67e-2ee9-58c1-be81-e3a737e52dd9/scratchpad/final_plan.md` (§6 backend, §7.3 wording, §9 table).
- The doc's Outline tab: KAZE, PointClickCare, Tiger Data, Vultr.
- When the Outline conflicts with KAZE rules, the KAZE rules win.

## Scope (limited on purpose)

**In: one FastAPI backend, three layers**

1. **KAZE core**, the priority and fully working:
   - **State machine:** ARMED → PROMPTED → REMINDED → L1 → L2 → FINAL ("Emergency services: SIMULATED", never dialled), with an ACKED follow-up, RESOLVED and FALSE_ALARM.
   - **Duress:** silent and masked as "Cancelled ✓", with byte-identical responses.
   - **LOST_CONTACT:** fires on lease expiry and has its own wording.
   - **Durable timers:** DB rows claimed with `FOR UPDATE SKIP LOCKED`, so they survive `kill -9`.
   - **Outbox:** at-least-once delivery with dedupe.
   - **Telegram long-poll bot:** invites, "I'm on it" / "pass it on" / "she's safe" buttons, and the wearer's "Reminders" nudges that mirror to the watch.
   - **Device endpoints:** bearer key, idempotent `event_id`.
   - **HR ingest:** `/v1/hr`.
   - **Contacts:** must be approved by the wearer and see nothing before an alert.
   - **Location:** last-known location released at alert time, with link expiry (410).
   - **Timeline:** hash-chained and append-only.
   - **Rule:** every sensor and heart-rate rule can only output `PULL_FORWARD`, with a counted challenge.
2. **Tiger Data** (Outline §3), in the same database:
   - a `scrunchie_telemetry` hypertable for raw accel/FSR at up to 50 Hz, with batch ingest via COPY;
   - an `hr_samples` hypertable;
   - continuous aggregates at 1 min → 1 h → 1 d, with real-time aggregation on;
   - a SQL view giving a z-score of the last 5 min against the 7-day baseline;
   - compression after 1 day;
   - retention per profile: **KAZE night-out 48 h raw** and **senior-mode longer**, so privacy and the PCC trends don't collide.
   - All of it runs on plain Postgres too: the Timescale calls are guarded.
3. **PointClickCare senior mode** (Outline §2), small:
   - a synthetic data generator (20 residents × 14 days, scripted declines plus counter-examples, all labelled SYNTHETIC);
   - a deterministic rule engine for sway drift against a 48 h baseline, rise-to-settle time after standing (accelerometer + heart rate), and night restlessness;
   - an explainable 0–100 alert card with the rule breakdown and a suggested CNA check, via `GET /v1/care/residents` and `GET /v1/care/alerts`;
   - wording that never diagnoses, and no "48–72 h" claim. Skin temperature is dropped (no sensor).
4. **Vultr** (Outline §4), thin:
   - `Dockerfile` + `docker-compose.yml`, with deploy notes for a Vultr Compute VM;
   - an optional Vultr Serverless Inference client (OpenAI-style) that **only words the PCC care-team briefing**, with a template fallback. It is never in the KAZE escalation path, and the app runs fine without a Vultr key.

**Also in:**
- `c.html`: the judge/contact page, with consent, join, polling, full-screen alarm and buttons.
- `hr_bridge.html`: Chrome Web Bluetooth for HeartCast → `/v1/hr`, with a SIM fallback.
- `scripts/simulate.py`: runs all 7 KAZE scenarios plus a PCC seed, with no hardware.
- `scripts/bridge.py`: the USB-serial bridge.
- `serve.sh` / `run.py` (the kill -9 restart loop) and `chaos.sh`.
- `docs/API.md`: the device line protocol and HTTP contract for firmware and frontend.
- `docs/CLAIMS.md`: the Outline claims reworded to KAZE-safe language. For example, "signature of spiking" becomes "sway well above your own baseline → early check-in", and "GPS" becomes "phone's last-known location".
- `README.md` and `.env.example` (no secrets).

**Out:** ESP32 firmware, the `/me` wearer page, the projector dashboard, Twilio voice, Next.js, and any LLM in a safety decision.

**Layout:** `backend/app/{main,config,db,states,rules,timers,outbox,telegram,audit,care,vultr}.py`, `backend/app/routes/*.py`, `backend/app/pages/*.html`, `backend/migrations/001_schema.sql`, `backend/scripts/*`, `backend/tests/*`, `docs/*`.

**Heart rate is pluggable.** `/v1/hr` accepts heart rate from any source: HeartCast via `hr_bridge.html`, a streaming-service API, an Apple Health Shortcut, or the simulator. The heart-rate research (`wf_324baab7-954`, still running) only decides which small adapter to add. If a cloud streaming API such as Pulsoid wins, I add a server-side subscriber that posts into the same table, with no other changes.

**Merge note:** the Outline-alignment workflow (`wf_47aa33b2-47c`) is still running. I'll fold its verified SQL and API details into the build when it lands, without changing scope.

## Steps
1. **KAZE core and tests first**, then Tiger, then PCC, then Vultr. Each layer is a separate commit, so `main` always works.
2. **Verify** as below.
3. **Push:** `git fetch origin main` (the clone is shallow), then `git push origin main`.
4. **Summary:** post a short summary for the team chat: what's in it, how to run it, and what each teammate builds on it in the morning.

## Verification
- **Unit tests** (`pytest backend/tests`, no DB needed):
  - No sensor or heart-rate path reaches `ESCALATED_*`.
  - Only "she's safe" or the real PIN resolves an alert.
  - A late squeeze never cancels an escalation.
  - OK and duress responses are byte-identical.
  - The banned-wording grep is clean.
  - The PCC rules fire on the scripted declines and not on the counter-examples.
- **Integration**, if Postgres can be installed here: apply the migration, run the server, and run `simulate.py` across all 7 scenarios. Check transitions and outbox rows, and that `kill -9` mid-REMINDED still fires L1 after restart. If Postgres isn't available, say so and ship with the unit tests only.
- **Not testable here:** Telegram, Tiger Cloud and Vultr (no keys). The README gives a 10-minute plug-in checklist.
