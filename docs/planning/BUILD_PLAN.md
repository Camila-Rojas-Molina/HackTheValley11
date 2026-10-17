# StillHere: build plan for Hack the Valley 11 (Project KAZE), revised after the feasibility and KAZE/safety reviews

**Times.** The clock starts at **Sat Oct 17, 18:00 ET**. If you start later, shift every time in this plan by the same amount.

**Deadlines.** Two pages disagree, so plan around the earlier one.
- The Devpost countdown says **Oct 18, 2026 @ 8:00 am EDT** ([Devpost](https://hack-the-valley-11.devpost.com/)).
- The Rules tab says final submissions are due **10:00 AM ET**, with the expo and judging at **12:00–3:00 PM ET** ([Rules](https://hack-the-valley-11.devpost.com/rules)).
- Treat **08:00 as the hard close** and **submit at 07:00**. Confirm the judging time on Discord.
- If judging is a 12–3 PM expo, you may run the demo many times. §8 covers resetting between runs.
- The initial submission (team members plus prize categories) is due **Sat 9:00 PM ET** ([Rules](https://hack-the-valley-11.devpost.com/rules)).

**What changed from the draft:**
- **Scope.** About 40% of the build is cut now, not "if behind" (§10). Firmware is now four things: squeeze decoding, the counted challenge, head position, and transport.
- **Transport.** USB serial through `bridge.py` is the default. Wi-Fi is an upgrade that must pass a soak test at 01:00.
- **Frontend.** Next.js is dropped. FastAPI serves plain HTML pages.
- **Device security.** HMAC is replaced by a bearer device key.
- **New safety logic:**
  - An **ACKED follow-up** state: an acknowledgement alone never closes an alert.
  - A **final simulated escalation** step.
  - The counted challenge applies at the **first** prompt after any sensor flag.
  - A more deliberate, gated **duress** squeeze.
  - Late squeezes **downgrade** an alert but never close it.
  - Hair mode prompts through the **watch tap, with LEDs off**.
  - The **wearer approves every contact**.
  - **Last-known location** replaces live location.
- **Hardware use of both watches:**
  - The Apple Watch is the wearer's heart-rate source and her discreet prompt.
  - The Fitbit is Contact #1's wrist buzzer by default. It becomes the backup heart-rate source only if it is a Charge 6.
- **Corrected claims:** the GHB numbers, the dated Statistics Canada claim, "hash-chained" instead of "tamper-evident", and the judge's phone "rings" instead of "buzzes".

---

## 1. Product summary and 30-second pitch

**StillHere** is a scrunchie that works as a hands-free safety check-in device. It is built for hair mode (worn in a ponytail) for nights with no pockets and no free hands. The same scrunchie also works on the wrist, using squeezes and LEDs only.

**How it works:**
- **Check-ins.**
  - Your Apple Watch taps you with a neutral message from a bot named "Reminders".
  - For a routine check-in, squeeze twice to answer "I'm OK".
  - The bot replies "Noted ✓" and the watch taps again.
- **Silent duress.**
  - Three short squeezes and then a 2-second hold, or the duress PIN, looks exactly like an "OK" or a cancel.
  - The watch shows the same "Noted ✓", but your contacts are silently asked for help.
- **Sensors only bring the next check-in forward.**
  - In the hair, an accelerometer notices a sustained change in head position. Your own Apple Watch supplies heart rate.
  - Neither decides that you are in trouble. An unusual change only makes StillHere ask sooner.
  - That early check-in needs a **counted** reply ("Reminder 3" means squeeze three times), so a reflex double squeeze cannot dismiss it.
- **Escalation happens only when you don't answer.**
  - A countdown in the database keeps running if your phone dies, or if we kill our own server live.
  - It alerts trusted contacts in order. Tapping "I'm on it" is not the end: that contact must confirm "I'm with her / she's safe", or the alert moves to the next person.
  - If nobody responds, a final step shows **"Emergency services: SIMULATED"**. No call is ever placed.
- **Lost contact is a separate alert,** with neutral wording.
- **Privacy and autonomy.**
  - Only contacts you approve are on the list, and contacts see nothing until an alert.
  - Your last-known location is released only at alert time, with its age shown, and the link expires.
  - Raw signals are deleted after 48 h.
- **Personal timeline.**
  - A hash-chained, timestamped record that you own and can export. Edits made after you export it are detectable.
  - It can help someone who cannot remember the night report it and ask for testing quickly.
  - There is no rapid drug screen for GHB-like drugs, and GHB becomes undetectable in blood within 4–8 h and in urine after at most 12 h ([Liechti & Kupferschmidt, Swiss Med Wkly 2004](https://smw.ch/index.php/smw/article/download/409/406/806)).

**30-second pitch.** Galit delivers it. It is about 60 words, which takes about 27 s at 140 wpm.

> "As of 2018, Statistics Canada filed drink spiking under 'other assaults' ([Global News](https://globalnews.ca/news/4367269/n-s-police-to-review-drink-tampering/amp)). The danger is the gap between when you can't ask for help and when anyone notices. In spy movies they hide weapons in their hair. We hide help in yours. Squeeze to check in. Sensors never decide; they only ask sooner. Don't answer, and your people are alerted until someone takes responsibility. Judge: you're Contact #2."

**Context lines to use only if asked:**
- UK police received 6,732 spiking reports between May 2022 and April 2023 ([GOV.UK](https://www.gov.uk/government/publications/crime-and-policing-bill-2025-factsheets/crime-and-policing-bill-spiking-factsheet-moj)).
- The UK enacted an "administering a harmful substance (including by spiking)" offence on 29 April 2026 ([GOV.UK](https://www.gov.uk/government/publications/crime-and-policing-bill-2025-factsheets/crime-and-policing-bill-spiking-factsheet-moj)).
- 7.8% of 6,064 US students reported being drugged ([APA](https://www.apa.org/news/press/releases/2016/05/drink-spiking)).

**Points engineers will notice** (one slide, plus Q&A):
- Timers are database rows, claimed with `FOR UPDATE SKIP LOCKED`.
- Side effects go through a transactional outbox.
- Device events are idempotent.
- The per-session timeline is hash-chained.
- We run a live `kill -9` mid-countdown.

---

## 2. Prize tracks and how each criterion is met

| Track (Devpost) | Prize | Enter? |
|---|---|---|
| Swapna Samuel: Project KAZE Challenge | $1,500 / $500 cash ([Devpost](https://hack-the-valley-11.devpost.com/)) | **Primary** |
| Top 3 overall | Pixel 10a / keyboards / AirPods; every team is considered ([Devpost](https://hack-the-valley-11.devpost.com/)) | Automatic |
| Best Healthcare & Well-being Hack | Owala bottles ([Devpost](https://hack-the-valley-11.devpost.com/)) | **Yes**, no extra work |
| [MLH] Best Use of Tiger Data | Stream Deck Mini; judged on the "most innovative, impactful, and performance-driven use" ([Devpost](https://hack-the-valley-11.devpost.com/)) | **Yes**, about 30 min of extra work |
| [MLH] Best Domain Name from GoDaddy Registry | Gift card ([Devpost](https://hack-the-valley-11.devpost.com/)) | **Cut** (on the cut-now list) |
| [MLH] DEV Writing Challenge | $100; editors pick monthly ([Devpost](https://hack-the-valley-11.devpost.com/)) | Optional, after the event |
| Gemini / AI for Social Impact / PointClickCare / Vultr / Solana | — | **Skip.** An LLM in the loop weakens "sensors never decide" and the privacy story. PointClickCare targets nursing-home care teams. |

**KAZE rubric** (weights from the KAZE brief in the hacker package):

| Criterion | What we build | Where judges see it |
|---|---|---|
| **Potential impact 30%** | Closes the gap between losing the ability to ask and someone noticing. Hands-free, with no screen. Reuses a watch the person already owns. The personal timeline supports reporting and early testing (the GHB detection window is ≤12 h in urine ([SMW](https://smw.ch/index.php/smw/article/download/409/406/806))). We also answer "why not Apple Check In or Google Safety Check?" (slide below). | Hook; scenario 2; differentiation slide |
| **Safety & reliability 25%** | Sensors can only trigger an earlier check-in, enforced in code and covered by a unit test. The counted challenge runs at the first prompt after any sensor flag. The countdown lives on the server and survives crashes. Escalation runs L1 → L2 → final (simulated). An ACKED follow-up means one reflexive tap cannot close an alert. Lost contact is separate from no response. Duress is gated against accidental triggers. A late squeeze downgrades an alert but never closes it. Messages are delivered at least once through an outbox. Motivation: Apple's auto-calling Crash Detection produced 71 non-emergency calls in one weekend in Summit County ([Macworld](https://www.macworld.com/article/1473802/apple-acknowledges-faulty-crash-detection-skiers.html)), and threshold fall detectors averaged 57% sensitivity on real falls ([Bagalà 2012](https://pmc.ncbi.nlm.nih.gov/articles/PMC3353905/)). | Live `kill -9`; unplug; L2 to the judge; ACKED follow-up |
| **Usability & accessibility 20%** | One-handed squeeze, no screen. Hair mode prompts through a watch tap. The challenge uses counts, not colours. Every response window can be raised to 10× its default (demo 12 s → 120 s; real 2 min → 20 min), which is the "adjust" option in WCAG 2.2.1 ([W3C](https://www.w3.org/WAI/WCAG21/Understanding/timing-adjustable)). Contacts need only Telegram or a web page. | Safe scenario; the judge joins with one tap |
| **Feasibility 15%** | Off-the-shelf parts and watches people already own. No paid platforms: Tiger trial, Telegram Bot API and a free tunnel. Value slide below. | BOM / architecture / value slide |
| **Privacy & autonomy 10%** | The wearer approves every contact and can remove one silently. Contacts can never arm a session, see its status or request location. Nothing is shown before an alert. Last-known location is released only at alert time and then returns 410. The real profile sends only heartbeats and events. Retention is 48 h raw / 7 d aggregate. Data stays in the Tiger `ca-central-1` region ([Tiger regions](https://www.tigerdata.com/docs/learn/tiger-cloud/regions)). The privacy slide names coercive control explicitly. | Scenario 7; privacy slide |

**KAZE constraints**, stated in the Devpost write-up and on a slide:
- All testing is simulated. Impairment is simulated only (gloves, a dual task, impairment goggles), never tested with intoxicated people.
- No real emergency calls. The UI shows "Emergency services: SIMULATED, not placed".
- No claim to confirm spiking, diagnose incapacitation or predict unconsciousness.

**Differentiation slide** (big-tech judges will ask this):
- **Apple Check In** (iOS 17+) shares location, battery and signal if you don't respond. The full option adds your route, last iPhone unlock and Apple Watch removal ([Apple](https://support.apple.com/en-au/guide/personal-safety/ips56b5bc469/web)).
- **Google Safety Check** starts Emergency Sharing with live location when there is no response. A check can last up to 24 h ([Google](https://support.google.com/assistant/answer/13683607)).
- Line to say: "Those are great, and both depend on the phone in your hand." StillHere adds five things:
  - hands-free check-ins from the hair or wrist;
  - a silent duress that looks like a cancel;
  - multi-contact escalation until a person takes responsibility;
  - check-ins pulled forward by motion and heart rate;
  - a server-side countdown that continues if the phone dies.
- Roadmap: StillHere as an input that can start Check In or Safety Check.

**Value slide** (15 s, for the sponsor's FinOps and value lens):
- **Bill of materials:**
  - Grove ADXL345: US$10.90 ([Seeed](https://www.seeedstudio.com/Grove-3-Axis-Digital-Accelerometer-16g.html)).
  - Ohmite FSR01CE: ₱600.48 each at qty 1 and ₱418 at qty 1k ([Digi-Key](https://www.digikey.ph/en/products/detail/ohmite/FSR01CE/9178296)).
  - Plus an ESP32 module.
- **Data per user-night (real profile):** heartbeats every 15 s ≈ 1,440 rows over 6 h; heart rate at 1 Hz ≈ 21,600 rows, compressed.
- **Messaging cost:** $0 on Telegram.
- **Reuse:** watches people already own.
- **Buyers:** campus safety programs and student unions.

**General HTV criteria** ([Rules](https://hack-the-valley-11.devpost.com/rules)):

| Criterion | How we meet it |
|---|---|
| Technicality | Durable state machine, firmware squeeze decoder, Web Bluetooth heart-rate bridge |
| Design | Discreet wearable; calm contact page that turns loud only when needed |
| Solves a current issue | KAZE problem framing |
| Presentation | Scripted 3:30 demo with a judge taking part |
| Inspiration | "We hide help in your hair" |
| Authenticity | Duress that looks like a cancel, the counted challenge, and the ACKED follow-up |

**Tiger Data, mapped to the track's own words** ("Standard SQL", "Unified Data Stack", "Continuous Aggregates", "Compression") ([Devpost](https://hack-the-valley-11.devpost.com/)):
1. One database holds the relational state machine (sessions, timers, outbox, audit) and the `hr_samples` hypertable. `motion_features` exists only in the demo/debug telemetry profile.
2. The dashboard HR chart and "your usual" come from the real-time `hr_1m` continuous aggregate ([real-time caggs](https://www.tigerdata.com/docs/use-timescale/latest/continuous-aggregates/real-time-aggregates)).
3. The columnstore is on by default when tables are created with `CREATE TABLE … WITH (tsdb.hypertable…)` ([docs](https://www.tigerdata.com/docs/api/latest/hypertable/create_table)).
4. Retention policies (48 h raw, 7 d aggregate) double as privacy controls. Retention can be applied to the aggregate itself ([Tiger](https://www.tigerdata.com/docs/use-timescale/latest/data-retention/data-retention-with-continuous-aggregates)).
5. The durable timers live in the same database. That is the `kill -9` story.

---

## 3. System architecture and data flow

```
 [1] SENSE (on the wearer)              [2] RELAY (demo laptop: Mac or Windows)      [3] DECIDE + STORE              [4] REACH PEOPLE
+-------------------------------+ USB  +----------------------------------+        +---------------------------+     +--------------------------------+
| Scrunchie, hair pocket        |serial| bridge.py (pyserial + httpx)     | HTTP   | FastAPI :8000, one origin |     | Telegram Bot API (long poll)   |
|  ESP32 + 2x FSR + Grove accel |----->|  lines -> /v1/device/*           |------->|  state machine + rules    |---->|  Contact #1 phone -> Fitbit    |
|  squeeze decoder, challenge,  |<-----|  downlink C,<led>,<blink_n>      |localhost|  timer_worker  (1 s)     |<----|   wrist buzz                   |
|  head position                |      |  buffers if server is down       |        |  outbox_sender (0.5 s)    | ack |  Wearer's chat "Reminders"     |
|  (Wi-Fi/HTTPS = upgrade, G6)  |      +----------------------------------+        |  telegram_poller (lock)   |     |   -> locked iPhone -> Watch tap|
+-------------------------------+      +----------------------------------+        |  pages: /dashboard /me    |     +--------------------------------+
+-------------------------------+ BLE  | Chrome /hr-bridge (own window)   | POST   |   /hr-bridge /c/<t> /l/<s>|     +--------------------------------+
| Apple Watch -> iPhone (locked)|----->|  Web Bluetooth 0x180D / 0x2A37   |------->|                           |---->| /c/<token> via public URL      |
|  HeartCast workout = std HR   |      |  POST inside the notify handler  | /v1/hr +------------+--------------+     |  Contact #2 = judge (approved) |
| [Fitbit Charge 6 = backup]    |      +----------------------------------+                     | SQL, sslmode=require     +--------------------------------+
+-------------------------------+      | cloudflared or Tailscale Funnel  |        +------------v--------------+
                                       |  -> PUBLIC_BASE_URL (phones only)|        | Tiger Cloud, ca-central-1 |
                                       +----------------------------------+        |  sessions/timers/outbox/  |
                                       | Projector: /dashboard + psql     |<-------|  audit (hash chain)       |
                                       |  \watch countdown                |  poll  |  hr_samples + hr_1m cagg  |
                                       +----------------------------------+        |  retention 48 h / 7 d     |
                                                                                   +---------------------------+
```

**Data flow:**
1. **Device to server.**
   - The scrunchie samples at 100 Hz and decodes squeezes and head position on the device.
   - Over USB serial it prints a heartbeat line every 1 s and event lines immediately.
   - `bridge.py` POSTs `/v1/device/heartbeat` every 2 s (demo), but only while device lines are arriving. It POSTs `/v1/device/events` immediately.
   - The heartbeat reply carries the downlink (`led`, `blink_n`, `challenge_id`), which `bridge.py` writes back over serial.
   - The device and heart-rate paths use `localhost`. The public URL is only for phones.
2. **Heart rate to server.**
   - The Apple Watch runs a HeartCast workout. The iPhone re-advertises heart rate as a standard BLE Heart Rate sensor.
   - The `/hr-bridge` page in Chrome subscribes to it and POSTs `/v1/hr` inside each notification handler, about once a second.
3. **Rules.** Every detector and the heart-rate rule can output only **`PULL_FORWARD`** (ARMED → PROMPTED with a counted challenge). Only a deadline expiring with no qualifying human response can reach `ESCALATED_*`.
4. **Side effects.**
   - Transitions write outbox rows.
   - `outbox_sender` delivers them to Telegram: contacts, and the wearer's "Reminders" chat, which reaches the watch.
   - `/c/<token>` polls `/v1/contact/<token>/status` every second.
5. **Acknowledgement.**
   - "I'm on it" moves the alert to ACKED, and the other contacts get "<name> is handling it".
   - Only "I'm with her / she's safe" or the wearer's real PIN closes the alert.

**Why this topology:**
- **USB serial is the default.** It removes the iPhone hotspot, crowded 2.4 GHz and the tunnel from the device path. Pulling the cable is the lost-contact demo.
- **Quick tunnels:**
  - They need no account. WebSockets work through them; SSE does not. They allow 200 in-flight requests ([Cloudflare](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)).
  - They have "no uptime guarantee", and the hostname changes on every restart (same page).
  - They need outbound port 7844 ([Cloudflare firewall](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)). If UDP is blocked, use `--protocol http2` ([run parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/)).
- **Alternative: Tailscale Funnel.**
  - It is available on all plans (in beta). It listens only on 443, 8443 or 10000, and uses `*.ts.net` names in your tailnet's domain ([Tailscale](https://tailscale.com/kb/1223/funnel)).
  - Confirm at 18:45 that its hostname stays the same across restarts before relying on it.
- **ngrok's free tier is ruled out.** It adds an interstitial page and has a 20k requests/month cap ([ngrok](https://ngrok.com/docs/pricing-limits/free-plan-limits)).
- **Telegram long polling** (`getUpdates`) needs no public URL ([Bot API](https://core.telegram.org/bots/api)).

---

## 4. Hardware

### 4.1 Bill of materials

| Part | Qty | Source | Role |
|---|---|---|---|
| ESP32 dev board ("ESP32S", micro-USB) | 1 | **Have** | Plan A microcontroller, on USB serial by default |
| Micro-USB **data** cable, as long as you can find (≥1 m) | 1–2 | **Hunt at 18:05:** MLH desk, Discord, any power-bank or older-Android owner. The MLH list has no micro-USB cables, ESP32 or batteries ([MLH](https://guide.mlh.com/organizer-resources/hardware-lab-contents)) | Flashing, serial tether and power. **Test it:** in Arduino IDE a new serial port must appear; if not, the cable is charge-only. A charge-only cable can still power an already-flashed ESP32 |
| Power bank | 1 | The white block, if it is one; otherwise borrow | Only for the Wi-Fi upgrade. The ESP32 draws about 145 mA with `WiFi.setSleep(false)` ([mischianti](https://mischianti.org/esp32-practical-power-saving-manage-wifi-and-cpu-1)) |
| Arduino Uno + Grove Base Shield v2 + USB A-B | 1 | **Have** | **Plan B** tethered node; the accelerometer plugs straight into the shield's I2C port |
| Ohmite FSR01 (~40 mm square) | 2 | **Have** | Squeeze input. Activates under 20 g ([Digi-Key](https://www.digikey.ph/en/products/detail/ohmite/FSR01CE/9178296)) |
| 10 kΩ resistors | 2 | MLH "Resistor Kit"; check values on the lab multimeter ([MLH](https://guide.mlh.com/organizer-resources/hardware-lab-contents)) | FSR pull-downs ([Adafruit](https://learn.adafruit.com/force-sensitive-resistor-fsr?view=all)) |
| 220 Ω and 47–100 Ω resistors | 2 + 2 | Same kit | 220 Ω for red and yellow LEDs; 47–100 Ω to test blue and green. Their ~2.8–3.3 V forward voltage makes them dim or dark on a 3.3 V pin through 220–330 Ω ([espboards](https://www.espboards.dev/tools/led-resistor-calculator/)) |
| LEDs | Wrist mode: 1 yellow (status/challenge) + 1 red (setup/error); optional blue (link) | **Have** | Wrist-mode cues only. **Off in hair mode** except one dim LED during setup |
| **Grove 3-Axis Digital Accelerometer** (either an **ADXL345 at 0x53** or a **LIS3DHTR at 0x19**; Seeed sells both under this name) | 2 (one spare) | **Borrow from the MLH lab**; the list has about 10 but names no chip ([MLH](https://guide.mlh.com/organizer-resources/hardware-lab-contents), [Seeed LIS3DHTR](https://wiki.seeedstudio.com/Grove-3-Axis-Digital-Accelerometer-LIS3DHTR/)) | Head position. Read the chip marking and run an I2C scanner before writing any driver code |
| **Grove-to-female-jumper cable** | 1–2 | **Ask the MLH desk first** | Grove accelerometer to bare ESP32 pins |
| Grove cable, cut (fallback) | 1 | One of your 3. The lab has a stripper but no soldering iron ([MLH](https://guide.mlh.com/organizer-resources/hardware-lab-contents)) | Strip ~10 mm, fold the strands back, push into the female end of an M/F jumper, then tape or hot-glue the joint |
| Grove LED Socket kits | 3 | MLH (20 available, each with a resistor on board) ([MLH](https://guide.mlh.com/organizer-resources/hardware-lab-contents)) | **Plan B only** |
| Grove Touch Sensor (TTP223) | 1 | **Have** | Backup tap input if the FSRs misbehave inside the fabric |
| Electrolytic capacitor | 1 | **Have** (check the printed value; ~10 µF is ideal) | Upload fix: between EN and GND, stripe to GND ([Random Nerd Tutorials](https://randomnerdtutorials.com/solved-failed-to-connect-to-esp32-timed-out-waiting-for-packet-header/)) |
| M/F and F/F jumpers, breadboard | some | ~20 owned; the lab has 40 of each ([MLH](https://guide.mlh.com/organizer-resources/hardware-lab-contents)) | Prototyping |
| Oversized scrunchie ×2 | 2 | A teammate's, or buy | Enclosure: FSRs under the fabric; ESP32 and accelerometer in a sewn hair pocket |
| Apple Watch + iPhone | 1 | **Have** | Heart rate (through HeartCast) **and** the wearer's discreet prompt (notification tap) |
| Fitbit (model unconfirmed) | 1 | **Have** | **Default:** Contact #1's wrist buzzer. **If it is a Charge 6:** backup heart-rate source |
| Not used | — | Servo, ultrasonic sensor, quad display, Grove buzzer | — |

**Ask the MLH desk at 18:05 for:**
- 2× Grove accelerometer (read the chip marking at the desk);
- Grove-to-female-jumper cables;
- the resistor kit and a turn on the multimeter;
- 3× Grove LED sockets;
- M/F jumpers;
- "anyone with a micro-USB data cable".

Do not wait for an ESP32, vibration motor, heart-rate sensor or batteries. The lab list has none of them ([MLH](https://guide.mlh.com/organizer-resources/hardware-lab-contents)).

**If upload fails with "Timed out waiting for packet header":**
1. Hold BOOT during the upload.
2. If that fails, fit the capacitor between EN and GND ([RNT](https://randomnerdtutorials.com/solved-failed-to-connect-to-esp32-timed-out-waiting-for-packet-header/)).

**Do not** flash the ESP32 through the Uno as a USB-serial adapter. The Uno drives 5 V into the ESP32's 3.3 V RX, and forum users report failures ([esp32.com](https://esp32.com/viewtopic.php?p=132576)).

### 4.2 Heart-rate sources and watch hardware roles (decide by 19:30, hard stop 20:15)

| Priority | Path | Facts | Action |
|---|---|---|---|
| **1 (primary)** | Apple Watch → **HeartCast** on iPhone → laptop Chrome Web Bluetooth | Free (optional Pro $4.99 / Audible $7.99); v3.9.0 dated Oct 6; needs iOS 18.6+ and watchOS 8.0+. Asks you to keep HeartCast visible on the iPhone while connecting; "will continue to run in the background during your workout" ([App Store](https://apps.apple.com/us/app/heartcast-heart-rate-monitor/id1499771124)). The Apple Watch has no built-in standard BLE heart-rate broadcast ([Wahoo](https://support.wahoofitness.com/hc/en-us/articles/4406329958674-Apple-Watch-and-Apple-Health)) | Connect with the iPhone unlocked and HeartCast in front, then **lock the iPhone and keep it locked for the whole demo**. Background apps hide their service UUIDs from non-Apple scanners, so always reconnect with HeartCast in front ([Apple](https://developer.apple.com/library/archive/documentation/NetworkingInternetWeb/Conceptual/CoreBluetooth_concepts/CoreBluetoothBackgroundProcessingForIOSApps/PerformingTasksWhileYourAppIsInTheBackground.html)). Zero-code test: [guyru.github.io/web-hr-monitor](https://guyru.github.io/web-hr-monitor) |
| 2 | **HeartBLE** (on the watch alone; 7-day trial) | Claims direct broadcast from the watch ([Product Hunt](https://www.producthunt.com/products/heartble-for-apple-watch)) | Only if HeartCast fails at 19:30; stop at 20:00 |
| 3 | **Fitbit Charge 6** (only if that is the model) | Swipe down → **HR on Equipment** → connect → **Share** → **Start**. One connection at a time, and it drains the battery ([Google](https://support.google.com/fitbit/answer/14236705)) | **10-minute test at 18:30.** Keep it as the backup only if it streams to `/hr-bridge`. Maya would wear it while the Apple Watch keeps doing the taps |
| 4 (test only) | **HR Broadcast** | Free. The iPhone app must stay open to relay heart rate as a standard Bluetooth HR sensor ([App Store](https://apps.apple.com/mx/app/id6766073249)) | An open iPhone app means an unlocked iPhone, so the watch tap stops (below). Do not use it in the demo |
| Any other Fitbit | — | Sense/Versa 3 need the third-party "HR Service" app and an Android phone ([GitHub](https://github.com/mogenson/fitbit-heart-rate-service)). The legacy Web API shuts down Oct 30, 2026 ([Google](https://developers.google.com/health/reference/rest/v4/users?hl=id)) and syncs only about every 15 min ([Fitbit dev](https://dev.fitbit.com/build/reference/web-api/intraday/get-heartrate-intraday-by-interval/)) | **Contact #1's wrist buzzer.** It vibrates for phone notifications within 30 ft ([Google](https://support.google.com/googlehealth/answer/14225533)). Turn on Telegram notifications in the Fitbit app on Contact #1's phone and test at 18:30 |
| **Fallback (always built)** | Backend `sim` source | Labelled `simulated=true` | One click in the operator console |

**Do not build:**
- a watchOS app;
- a Shortcuts pipeline, which lags by minutes ([Heckel](https://blog.maximeheckel.com/posts/build-personal-health-api-shortcuts-serverless/));
- Health Auto Export;
- a MAX30102 sensor or camera-based heart rate;
- an ESP32 NimBLE heart-rate client. Wi-Fi and BLE share one radio by taking turns ([Espressif](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-guides/coexist.html)), and the laptop already reads heart rate.

**Limits to say out loud:**
- Apple Watch workout heart rate updates about every 5 s ([Apple forums](https://developer.apple.com/forums/thread/724724)).
- No beat-to-beat data is stored during a workout ([Apple forums](https://developer.apple.com/forums/thread/746218)), so there is **no live HRV**. HRV is roadmap.

**The watch as the wearer's prompt (hair mode):**
- Notifications reach the Apple Watch only while the iPhone is locked or asleep, and not while the watch is in Do Not Disturb ([Apple](https://support.apple.com/en-us/108369)).
- The bot is named "Reminders". It sends "Reminder", "Reminder 3" and "Noted ✓" to the wearer's own chat.
- A Workout DND / Fitness Focus set to "When starting a workout" can turn on when any workout app starts a workout, including HeartCast ([How-To Geek](https://www.howtogeek.com/711255/how-to-enable-do-not-disturb-during-workouts-on-apple-watch/), [iPhone Life](https://iphonelife.com/content/how-to-stop-apple-watch-notifications-during-workout)). Turn it **off**.
- Enter PINs on the `/me` page on the laptop or a second phone, never on Maya's iPhone, so it stays locked.

**Mandatory safety steps** (also on the T−10 checklist):
1. **Apple Watch Fall Detection off.**
   - After a hard fall plus about a minute of stillness and a 30 s countdown, it calls emergency services. Workouts can turn on "only during workouts" mode by default for ages 18–55 ([Apple](https://support.apple.com/en-gw/guide/watch/apd34c409704/watchos)).
2. **"Hold Side Button to Dial" off.**
   - Holding the watch side button "after a countdown … calls emergency services automatically".
   - Turn it off in the iPhone Watch app → Emergency SOS ([Apple](https://support.apple.com/108374)).
3. **iPhone Emergency SOS on every team phone:** turn off "Call with Hold and Release" and "Call with 5 Presses" in Settings > Emergency SOS ([Apple](https://support.apple.com/en-us/104992)).
4. **Workout DND / Fitness Focus off.**
5. **Live test:** with HeartCast running and the iPhone locked, the "Reminder" nudge must tap the watch.

### 4.3 Plan A wiring: ESP32 (3.3 V logic), with the board in the hair pocket next to the sensor

| Signal | ESP32 pin | Notes |
|---|---|---|
| Accelerometer VCC / GND | 3V3 / GND | Both chips accept 3–5 V ([Seeed ADXL345](https://wiki.seeedstudio.com/Grove-3-Axis_Digital_Accelerometer-16g), [Seeed LIS3DHTR](https://wiki.seeedstudio.com/Grove-3-Axis-Digital-Accelerometer-LIS3DHTR/)). Assume INT pins are **not** on the Grove cable ([Core Electronics](https://core-electronics.com.au/grove-3-axis-digital-accelerometer-16g-seeed-studio.html)) |
| SDA / SCL | **GPIO21 / GPIO22** | `Wire.begin(21,22); Wire.setClock(100000); Wire.setTimeOut(20);` The default timeout is 50 ms ([Arduino-ESP32 I2C](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/i2c.html)). Keep the wires short, because the I2C spec allows 400 pF of bus capacitance ([NXP UM10204](https://www.nxp.com/docs/en/user-guide/UM10204.pdf)). Re-run `Wire.begin` after 3 consecutive read errors |
| FSR A | **GPIO34** (ADC1_CH6) | 3V3 → FSR → GPIO34, with 10 kΩ from GPIO34 to GND. An input-only pin with no pull-up, which suits a divider ([ESP-IDF GPIO](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/gpio.html)) |
| FSR B | **GPIO35** (ADC1_CH7) | Same divider; try 4.7 kΩ if a hard squeeze saturates |
| Touch (TTP223) SIG | GPIO4 | Digital input only |
| LED yellow (status/challenge) / red (setup/error) | GPIO25 / GPIO26 | 220 Ω each |
| LED blue (link, optional) | GPIO33 | 47–100 Ω; drop it if still dim |
| Power and data | Micro-USB to the laptop (serial default), or to a power bank (Wi-Fi upgrade) | — |
| **Do not use** | GPIO0, 2, 5, 12, 15 (strapping); GPIO6–11 and 16–17 (flash/PSRAM); ADC2 pins for analog while Wi-Fi is on | [ESP-IDF GPIO](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/gpio.html) |

Read the FSRs with `analogReadMilliVolts()` at 11 dB attenuation, which covers about 150–3100 mV ([Arduino-ESP32 ADC](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/adc.html)).

### 4.4 Plan B wiring: Uno tethered over USB (use if the ESP32 isn't flashed by 19:00)

The Base Shield's Grove digital ports cover only D2–D8 ([Seeed](https://wiki.seeedstudio.com/Base_Shield_V2)).

| Signal | Uno / shield port |
|---|---|
| Accelerometer | Shield **I2C** port (switch at 5 V) |
| FSR A / B | **A0 Grove port** (A0/A1) through a cut Grove cable: 5 V → FSR → pin, 10 kΩ to GND |
| Touch sensor | D2 |
| Grove LED sockets | D4, D6, D8 |

- The firmware streams compact CSV at 50 Hz, or raise the baud rate to 250000.
- With only 2 KB of RAM on the Uno, the squeeze decoder and head position run in `bridge.py`.
- Unplugging the USB cable is the lost-contact demo.

### 4.5 Physical build
- **Hair mode (the demo mode).**
  - The ESP32 and accelerometer go in a sewn pocket at the ponytail base. The two FSRs sit on opposite sides of the band, so a pinch across the band presses both.
  - Only the USB cable leaves the pocket, running to the laptop (or to a pocket power bank in the Wi-Fi upgrade).
  - Recalibrate whenever the scrunchie is re-tied.
- **Wrist mode.** The same scrunchie on the wrist, with squeeze and LEDs only. There is no motion detector on the wrist (fall-like motion is cut and simulated).
- Say plainly that the prototype is bulky and tethered. In production it would be miniaturised and talk BLE to the phone.

---

## 5. Firmware modules (Arduino-ESP32 core 3.x)

**Runtime:**
- `task_sense` on core 1: one 6-byte accelerometer burst read and both FSR reads every 10 ms, all decoding in software.
- `task_net` on core 0: serial line I/O by default; HTTP in the Wi-Fi upgrade, with a stack of at least 12 KB.
- A FreeRTOS queue connects them.

**Libraries:**
- `Wire` and `Preferences` (only for the server URL and SSIDs).
- `Seeed_Arduino_LIS3DHTR`, only if the chip is a LIS3DHTR.
- For the Wi-Fi upgrade only: `HTTPClient` and `NetworkClientSecure`.

**Every detector calls exactly one function:**

```
request_early_checkin(reason, severity, features)   // prints an E-line; the server can only PULL_FORWARD
```

**No firmware path contacts anyone.**

**Cut now, not built:** FIFO and interrupt registers, the fall state machine, sway, prolonged stillness, NVS event persistence, HMAC and NimBLE. See §5.6.

### 5.1 Accelerometer setup (no FIFO, no interrupts)
1. Run an I2C scanner. Expect `0x53` for an ADXL345 or `0x19` for a LIS3DHTR.
2. **ADXL345** ([datasheet](https://cdn.sparkfun.com/assets/9/1/8/9/9/ADXL345.pdf)): write `0x31=0x0B` (FULL_RES ±16 g, 3.9 mg/LSB), `0x2C=0x0A` (100 Hz) and `0x2D=0x08` (measure). Every 10 ms, do **one 6-byte burst read from 0x32**.
3. **LIS3DHTR** ([Seeed](https://wiki.seeedstudio.com/Grove-3-Axis-Digital-Accelerometer-LIS3DHTR/)):
   - `LIS.begin(Wire, 0x19)`
   - `LIS.setFullScaleRange(LIS3DHTR_RANGE_16G)`
   - `LIS.setOutputDataRate(LIS3DHTR_DATARATE_100HZ)`
   - Read with `getAccelerationX/Y/Z()` every 10 ms.
4. Calibrate for 5 s while still at boot, and subtract the per-axis offset.

### 5.2 Squeeze decoder (FSR)

| Parameter | Value |
|---|---|
| Sampling | 100 Hz, EMA α = 0.3 |
| Signal | Per FSR (mV − baseline); "press" = max of the two; "both" = both above ON |
| Baseline | Slow EMA (τ ≈ 10 s), updated only while idle; it absorbs the elastic pre-load |
| Thresholds | Calibrate with 3 firm squeezes to get `vmax`. ON = base + max(300 mV, 0.40·(vmax − base)); OFF = base + max(150 mV, 0.20·(vmax − base)) |
| Debounce | 3 consecutive samples (30 ms) on each edge |
| Press classes | Under 80 ms ignored. 80–1000 ms SHORT. 1000–2000 ms AMBIGUOUS. ≥ 2000 ms **HOLD**. Over 5000 ms PRESSURE_ARTIFACT (logged, re-baseline, **never** duress) |
| End of sequence | 1000 ms with no new press |
| Output | `squeeze{class, shorts:k, hold_ms, both_fsr, orient_change_deg, artifact_10s, wake_before_5s}` |
| Class **DURESS** | Exactly `[S,S,S,HOLD≥2 s]`, **and** both FSRs above ON during the hold, **and** orientation change during the sequence < 30°, **and** no pressure artifact in the previous 10 s (rules out a headrest, lying back or a hug) |
| Class **PLAIN** | k SHORT presses only; the server judges k (2 = routine OK; k = N for a challenge) |
| Class **UNRECOGNIZED** | Anything else, including any AMBIGUOUS press; counts as no response |
| Server-side gate | Outside a prompt window (the 20 s after a prompt is delivered, the REMINDED stage, or an open alert), DURESS counts only if `wake_before_5s` is true: a double squeeze within the previous 5 s |
| Feedback | OK and DURESS get **identical** feedback. Wrist: the same yellow double-blink. Hair: the same "Noted ✓" to the wearer's chat |

- The timings are deliberately slower than Android's 300 ms double-tap and 400 ms long-press defaults ([AOSP](https://raw.githubusercontent.com/aosp-mirror/platform_frameworks_base/main/core/java/android/view/ViewConfiguration.java)), because squeezes may be slow, and reaction time slows with alcohol ([JCU](https://sk.pf.jcu.cz/artkey/stk-201603-0018_vliv-alkoholu-na-zmenu-reakcni-doby-u-muzu.php)).
- **RECORD and REPLAY modes.**
  - RECORD streams raw `R,` lines at 50 Hz.
  - REPLAY feeds recorded `P,` lines in place of the sensor reads, so the **real C decoder** runs on recorded traces.
  - Cami replays the dance, sit-back, lie-down, hug and slump traces and reports the false-duress count in §9.

### 5.3 Counted challenge
- **When it applies:**
  - Always at **REMINDED**.
  - At the **first prompt** whenever PROMPTED was caused by a `PULL_FORWARD` (sensor, heart rate or off-body).
  - Routine scheduled check-ins accept a plain double squeeze.
- **N** is drawn from **{1, 3, 4}**. Leaving out 2 means a reflex "OK" can never pass, and a guess passes 33% of the time.
- **Delivery:**
  - Hair mode: the watch shows "Reminder N" (neutral text).
  - Wrist mode: the yellow LED blinks N times, 250 ms on and 350 ms off. A press during the blinks, or within 300 ms after, is invalid and the device re-blinks once.
- **Wrong count or UNRECOGNIZED:** one retry with a new N. The server deadline is unchanged.
- **Worst-case latency** (on a slide):
  - Scheduled check-ins: interval + 2 + 2 min to L1.
  - Sensor-flagged: about 4 min to L1.
  - Plus 5 min per contact level.

### 5.4 Head position change (hair mode, the only real motion trigger)

**Calibration** (10 s, done on stage):
- 5 s upright gives `g_ref`.
- 5 s looking at a phone gives θ_phone.
- θ_drop = max(θ_phone + 15°, 50°). People flex the head 33–45° to look at phones ([Lee 2015](https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=EXT_ID:25323467%20AND%20SRC:MED&resultType=core&format=json)), and maximum active flexion is about 58° ([norms](https://avesis.iku.edu.tr/yayin/e6a38307-12a9-4526-9d9f-efa478dba5e1/normative-values-for-cervical-and-lumbar-range-of-motion-in-healthy-young-adults)).

**Measurement:**
- Use the angle between the low-passed gravity vector and `g_ref`, computed as acos(ĝ·ĝ_ref) ([AN3461](https://www.nxp.jp/docs/en/application-note/AN3461.pdf)).
- Measure only on quasi-static samples (| |a| − 1 g | < 0.1 g).

**Triggers** (patterns follow [US10964190B2](https://patents.google.com/patent/US10964190B2/en); these are precedents, not validated cut-offs):

| Trigger | Condition | Severity | Demo profile |
|---|---|---|---|
| Sustained | angle ≥ θ_drop **and** SD(svm over 1 s) < 0.05 g, held ≥ 20 s | MED | held **5 s** |
| Fast | angle change ≥ 50° between the last quasi-static sample before and the first after, ≤ 1 s apart, then held ≥ 10 s | HIGH (still only a check-in) | held 5 s |

The stillness requirement suppresses walking and dancing. Nodding (3 or more cycles in 120 s) is designed, not built.

### 5.5 Off-body flag (about 30 min; build after G6 only if green)
- Flag when the FSR baseline falls below 50% of its calibrated elastic pre-load (relative to the open-air reading) for 60 s while ENMO < 10 mg. This emits `offbody` → `PULL_FORWARD(LOW, 'device may be off')`.
- Separately, heart rate stopping for more than 120 s while armed triggers the server's "heart rate lost" rule (§6.6).
- If the flag is not built, the Q&A answer is: "The prototype doesn't detect removal; that's on the roadmap."

### 5.6 Designed, not built (on the §9 slide)

| Module | Design kept for the roadmap |
|---|---|
| Fall-like motion (wrist and hair) | Free-fall SVM < 0.6 g for ≥ 60 ms, then an impact ≥ 3.0 g within 1 s, then stillness and orientation change ([AN-1023](https://www.analog.com/media/en/technical-documentation/application-notes/AN-1023.pdf), [Bagalà](https://pmc.ncbi.nlm.nih.gov/articles/PMC3353905/), [UMAFall](https://pmc.ncbi.nlm.nih.gov/articles/PMC5982860/)). In the demo it is an **operator injection labelled "fall-like motion (simulated)"** |
| Gait unusual for you (sway) | ML RMSR from walking windows against a personal sober baseline ([Sekine](https://pmc.ncbi.nlm.nih.gov/articles/PMC3882286/), [PMC5017322](https://pmc.ncbi.nlm.nih.gov/articles/PMC5017322/), [Suffoletto 2020](https://www.federvini.it/images/jsad.2020.81.505.pdf)) |
| Prolonged stillness | 10 min below 45 mg ENMO; specificity to wakefulness only 45–65% ([van Hees 2015](https://pmc.ncbi.nlm.nih.gov/articles/PMC4646630/)) |
| HRV / RMSSD | Needs real RR intervals, for example from a chest strap ([phyphox](https://phyphox.org/wiki/index.php/Polar_H9_or_H10_(Bluetooth_Heart_Rate_Service))) |

### 5.7 Heart-rate read (on the laptop, not the firmware)

`/hr-bridge` is served at `http://localhost:8000/hr-bridge`, which counts as a secure context ([Chrome](https://developer.chrome.com/docs/capabilities/bluetooth)).
- **Platform:** use a Mac or Windows laptop. Web Bluetooth on Linux needs a flag (same page).
- **Connect:**
  - Call `navigator.bluetooth.requestDevice({filters:[{services:['heart_rate']}]})` from a click.
  - Then `gatt.connect()` → `getPrimaryService('heart_rate')` → `getCharacteristic('heart_rate_measurement')` → `startNotifications()`.
- **Reconnect with backoff on `gattserverdisconnected`.** Reconnecting needs HeartCast in front on an unlocked iPhone, so rehearse the one-click switch to SIM.
- **POST inside the `characteristicvaluechanged` handler, never from `setInterval`.** Chrome checks timers only once a minute on pages hidden for more than 5 min ([Chrome](https://developer.chrome.com/blog/timer-throttling-in-chrome-88)). Keep the bridge in its own visible window.

```js
function parseHRM(dv) {                       // DataView of a 0x2A37 notification
  const f = dv.getUint8(0); let i = 1;
  const bpm = (f & 0x01) ? dv.getUint16(i, true) : dv.getUint8(i); i += (f & 0x01) ? 2 : 1;
  if (f & 0x08) i += 2;                        // skip Energy Expended
  const rr = [];
  if (f & 0x10) for (; i + 1 < dv.byteLength; i += 2) rr.push(dv.getUint16(i, true) * 1000 / 1024);
  const rrSynthetic = rr.length > 0 && rr.every(r => Math.abs(r - 60000 / bpm) <= 1);
  return { bpm, rr_ms: rr, rr_synthetic: rrSynthetic };
}
```

- The layout follows [SimpleBLE](https://docs.simpleble.org/recipes/heart-rate-monitor). The fake-RR check follows [Rutenberg](https://www.guyrutenberg.com/2026/04/04/web-hr-monitor-heart-rate-and-hrv-in-the-browser/).
- Each POST sends `{ts, bpm, rr_ms, rr_synthetic, source:'apple_watch_heartcast'|'fitbit_charge6', simulated:false}`.
- RR is stored if present, but RMSSD is not computed.

### 5.8 Transport and protocol

```
USB serial, 115200 baud, one line per message (default transport)
 device -> bridge
  H,<boot_id>,<seq>,<enmo60_mg>,<tilt_deg>,<fsr_base_mv>,<offbody>     every 1 s
  E,<boot_id>:<seq>,<type>,<json_payload>                              immediately
  R,<ms>,<ax_mg>,<ay_mg>,<az_mg>,<fsr_a_mv>,<fsr_b_mv>                 50 Hz, RECORD mode only
 bridge -> device
  C,<led>,<blink_n>,<challenge_id>                                     from each heartbeat reply
  P,<ms>,<ax>,<ay>,<az>,<fsr_a>,<fsr_b>                                REPLAY mode

HTTP (bridge.py, or the ESP32 itself in the Wi-Fi upgrade)
POST /v1/device/heartbeat   every 2 s (demo) / 15 s (real)
  hdr:   Authorization: Bearer <device_key>, X-Device-Id, X-Boot-Id, X-Seq
  body:  {wear_mode, enmo60_mg, offbody, feats:[...]}   // feats only in the badged demo/debug telemetry profile
  reply: {state_public, server_time, cmd:{led, blink_n, challenge_id}}
POST /v1/device/events      immediately; event_id = boot_id:seq; retry 1, 2, 4, 8 s with the same id
  body:  {event_id, type, payload, device_ms}   // squeeze | head_position_change | offbody | prompt_shown
```

- **Auth: a static per-device bearer key.** HMAC is cut, because the ESP32 clock resets on power-on and needs SNTP ([ESP-IDF](https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/system/system_time.html)), so staleness checks would reject every request after the unplug and replug.
- **Replay protection:** dedupe on `event_id`, with `last_seq` keyed by `(device_id, boot_id)`. Server time is the only clock.
- **Masking:** `state_public` never reveals a duress alert. It reads ARMED.
- **`bridge.py`:**
  - POSTs heartbeats only while device lines keep arriving.
  - Buffers events in memory while the server is down and flushes them oldest first, duress first.
  - Reopens the serial port automatically on replug.

**Wi-Fi upgrade (attempt only after G5, decide at G6):**
- `HTTPClient.setReuse(true)` for keep-alive, a 2 s demo heartbeat, and a network task stack of at least 12 KB.
- A fresh TLS handshake needs 40–50 KB of free heap ([Espressif](https://docs.espressif.com/projects/esp-faq/en/latest/software-framework/protocols/esp-tls.html)).
- Use `setInsecure()` from the start and disclose it ([NetworkClientSecure](https://github.com/espressif/arduino-esp32/blob/master/libraries/NetworkClientSecure/README.md)).
- The server URL lives in `Preferences` and can be set over serial (`URL https://…`).
- **Hotspot:**
  - Rename the phone to an ASCII name such as `SHDEMO`. Default names contain a curly apostrophe (U+2019) ([mdash](https://forum.mdash.net/t/wifi-ssid-limitations-and-scans/508), [MIT](https://fab.cba.mit.edu/classes/863.24/people/AimanAnuar/week13.html)).
  - Turn on **Maximize Compatibility** for 2.4 GHz. Third-party clients are dropped after 90 s idle ([Apple](https://support.apple.com/guide/platform-support/sup025284ac0)).
  - Keep the phone plugged in and parked on the Personal Hotspot screen ([Apple](https://support.apple.com/en-afri/119837)). Prefer an Android hotspot if a teammate has one.
  - Store two SSIDs.
- **Soak test at G6:**
  - 5 unplug/replug cycles, each rejoining and completing its first POST in ≤ 15 s.
  - No spurious LOST_CONTACT over 10 min.
  - If either check fails even once, demo on serial.

### 5.9 Offline behaviour
- **Device:**
  - Keeps a 32-entry RAM ring of events (NVS persistence is cut).
  - After 2 failed sends, wrist mode flashes the LED briefly every 10 s.
  - On reconnect, flushes with the original `device_ms`, duress first.
- **Server:**
  - Escalation keeps running during the outage, and lease expiry sets `link_state = LOST_CONTACT`.
  - A late squeeze after escalation **never auto-cancels**. Contacts get "Maya responded on her device at 21:17 (delivered late) – please still check on her."

---

## 6. Backend (FastAPI + Tiger Data)

### 6.1 Stack and process
- **Libraries:** Python 3.12, FastAPI, uvicorn, `psycopg[pool]`, httpx, qrcode, pydantic.
- **Pool:** `AsyncConnectionPool(conninfo, open=False)`, then `await pool.open()`, with a `check` callback ([psycopg](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)).
- **Lifespan:** an `@asynccontextmanager` lifespan starts `timer_worker`, `outbox_sender` and `telegram_poller` ([FastAPI](https://fastapi.tiangolo.com/advanced/events/)).
- **Poller:** only one runs. It holds `pg_try_advisory_lock` on a **dedicated connection** outside the pool ([PG docs](https://www.postgresql.org/docs/current/functions-admin.html)).

**Database:**
- Tiger trial: $1,000 of credit for 30 days, no credit card ([Tiger](https://www.tigerdata.com/go/trial)). Create the service in **ca-central-1** ([regions](https://www.tigerdata.com/docs/learn/tiger-cloud/regions)).
- `DATABASE_URL=...?sslmode=require&keepalives=1&keepalives_idle=10`
- Run `ALTER ROLE tsdbadmin SET idle_in_transaction_session_timeout = '10s'`. Without it, a connection lost mid-transaction leaves timer rows locked, and `SKIP LOCKED` would skip them ([PG docs](https://www.postgresql.org/docs/current/runtime-config-client.html)).
- **Spare:** a free service with 750 MiB, which becomes read-only when full ([changelog](https://docs.tigerdata.com/about/latest/changelog)).
- **Local fallback:** **pull the TimescaleDB Docker image at 18:00**, while the Wi-Fi is quiet ([quickstart](https://www.tigerdata.com/docs/get-started/quickstart/timescaledb-quickstart)).
- **Ports:** Tiger uses a per-service high port (for example 30133), which campus Wi-Fi may block ([Tiger](https://docs.tigerdata.com/use-timescale/latest/services/connection-pooling)). Test it at the 18:45 network gate.
- **Tooling:** `tiger mcp install claude-code` lets Marco iterate on the schema from Claude Code ([Tiger CLI](https://www.tigerdata.com/tiger-cli)).

**Process:**
- `serve.sh` (the file name must **not** contain "uvicorn") runs `while true; do python run.py; sleep 1; done`.
  - `pkill -f` matches full command lines and would kill the loop as well ([pkill](https://man7.org/linux/man-pages/man1/pkill.1.html)).
- **Kill only the listener:** `kill -9 $(lsof -tiTCP:8000 -sTCP:LISTEN)` ([lsof](https://man7.org/linux/man-pages/man8/lsof.8.html)).
  - Without `-sTCP:LISTEN`, `lsof` also returns the Chrome tabs connected to :8000 and kills the dashboard and HR bridge.
- **Windows:** single-process uvicorn uses ProactorEventLoop ([uvicorn](https://uvicorn.dev/concepts/event-loop/)), which psycopg async does not support ([psycopg](https://www.psycopg.org/psycopg3/docs/advanced/async.html)).
  - In `run.py`, call `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` before `uvicorn.run`, or run the backend on macOS or WSL.
- **Tunnel:** run `cloudflared tunnel --url http://localhost:8000` (add `--protocol http2` if UDP is blocked), or Tailscale Funnel, as **its own long-lived process**. Export its URL as `PUBLIC_BASE_URL`.
- **Disable laptop sleep** from 05:00 until judging ends.

### 6.2 State machine

- `sessions.state` holds the main state, and `sessions.link_state` is separate.
- `alerts.kind` ∈ {NO_RESPONSE, DURESS, LOST_CONTACT}, and `alerts.masked=true` for duress.
- Every transition is a guarded `UPDATE sessions SET state=$to, version=version+1 WHERE id=$id AND version=$v AND state = ANY($from)`. Zero rows changed means the request is stale.
- **Prompt deadlines start at delivery.** On entering PROMPTED or REMINDED, the timer is inserted at `now() + window + 30 s`. When the wearer's nudge is marked sent, or the device reports `prompt_shown`, it is lowered to `delivered_at + window`. The countdown can never stall, and never starts before the prompt arrives.

| From | Trigger | To | Side effects | Deadline demo / real |
|---|---|---|---|---|
| ARMED | CHECKIN_DUE (routine) | PROMPTED (routine) | Nudge "Reminder" (watch tap); wrist: amber pulse | 12 s / 2 min from delivery |
| ARMED | **PULL_FORWARD** (head position, HR, off-body, HR lost, injected fall-like motion) | PROMPTED (**challenge**) | N ∈ {1,3,4}; nudge "Reminder N"; wrist: blink N | 12 s / 2 min |
| PROMPTED | Routine: PLAIN k=2. Challenge: PLAIN k=N. Or `/me` "I'm OK" | ARMED | Identical "Noted ✓"; schedule the next check-in; `/me` interaction refreshes last-known location | Interval 60 s / preset |
| PROMPTED / REMINDED | Wrong k or UNRECOGNIZED | Same state | One retry with a new N; deadline unchanged | — |
| PROMPTED | PROMPT_TIMEOUT | REMINDED | New N; nudge "Reminder N" | 12 s / 2 min |
| REMINDED | PLAIN k=N | ARMED | "Noted ✓" | — |
| REMINDED | REMINDER_TIMEOUT | **ESCALATED_L1** | Alert NO_RESPONSE to priority 1; release last-known location with its age | Ack 15 s / 5 min |
| L*i* | ACK_TIMEOUT, or contact taps "Can't help – pass it on" | L*i+1*, or **ESCALATED_FINAL** after the last contact | Notify the next contact; re-ping earlier ones; RENOTIFY every 20 s / 60 s, capped at 10 per level | Ack 15 s / 5 min |
| L*i* / FINAL | "I'm on it" | **ACKED** (by contact c) | Others: "<name> is handling it" | Follow-up 20 s / 10 min |
| ACKED | "I'm with her – she's safe" | RESOLVED, then the session returns to ARMED | Everyone: "Resolved by <name> at 21:16"; location link expires | Link: 15 s / 30 min after resolution |
| ACKED | "Can't reach her", or follow-up timeout | L*i+1* or FINAL | Re-ping the contact who acknowledged | — |
| Last L | ACK_TIMEOUT | **ESCALATED_FINAL** | Dashboard and contact pages show "Emergency services: SIMULATED – would contact 911 / campus safety with last known location". Logged. **Never placed.** Renotify all contacts every 20 s / 60 s | — |
| PROMPTED / REMINDED | Real PIN on `/me` | ARMED | — | — |
| ESCALATED_* / ACKED / FINAL | Real PIN on `/me` | FALSE_ALARM, then ARMED | "False alarm: Maya cancelled with her PIN at 21:17" to everyone notified | — |
| ESCALATED_* / ACKED / FINAL | Any device squeeze | **Unchanged** | "Maya responded on her device at 21:17 – please still check on her." | — |
| Any active state | DURESS squeeze (gated) or duress PIN | Alert DURESS at L1 immediately; **masked** (device and `/me` show ARMED / "Cancelled ✓") | "SILENT HELP REQUEST. She may not be able to talk. Don't call her phone." | Ack 10 s / 2 min, then the ladder above |
| DURESS alert open | Real PIN | Unchanged | "Maya entered her cancel PIN. Please confirm in person." Only a contact's "she's safe" closes it | — |
| `link_state` OK | `lease_expires_at < now()` (lease 10 s with a 2 s heartbeat / 60 s with a 15 s heartbeat) | `LOST_CONTACT` | **Real:** nudge the wearer first; tell contacts only if contact isn't restored within 5 min, a check-in comes due, or an alert is already open. **Demo:** contacts after a 10 s grace, or immediately if an alert is open. Text: "We lost contact with Maya's device at 21:14. Cause unknown. Her check-in schedule is still running." Pending deadlines **keep running** | — |
| `LOST_CONTACT` | Heartbeat | OK | "Contact with Maya's device restored at 21:16" to whoever got the lost-contact note | — |
| Session end | Preset end time | PROMPTED ("Home safe?") | Escalates normally if missed. Disarm only with the PIN. "Walking home" ends only on PIN or an arrival tap | — |

**Invariants, each unit-tested:**
- No path from a sensor or heart-rate event reaches `ESCALATED_*`.
- Only "she's safe" or the real PIN reaches RESOLVED or FALSE_ALARM.
- A device squeeze never stands down an escalation.
- Real-PIN and duress-PIN HTTP responses are **byte-identical**, and so are OK and duress device `cmd` replies and wearer messages.

### 6.3 Schema

**Plain tables:**
- users
- devices (`device_key_hash`, `boot_id`, `last_seq` per boot, `lease_expires_at`)
- contacts (`priority`, `channel`, `telegram_chat_id`, `web_token_hash`, `invite_token`, `invite_expires_at` = 10 min, single use, `approved_at`, `revoked_at`)
- sessions (`state`, `link_state`, `version`, profile timings, `challenge_n`, `prompt_cause`)
- timers
- alerts (`kind`, `level`, `masked`)
- outbox (`dedupe_key UNIQUE`, `provider_message_id`)
- acks (`telegram_update_id UNIQUE`)
- device_events (`event_id` PK, cached `result`)
- audit_log
- location_snapshots (encrypted coordinates, `captured_at`, `released_at`, `expires_at`)
- bot_state
- user_baselines

These stay out of hypertables, because a hypertable's unique keys must include the time column ([Tiger](https://www.tigerdata.com/docs/use-timescale/latest/hypertables/hypertables-and-unique-indexes)).

```sql
CREATE TABLE hr_samples (
  time timestamptz NOT NULL, user_id uuid NOT NULL, source text NOT NULL,
  simulated boolean NOT NULL DEFAULT false, bpm smallint NOT NULL,
  rr_ms real[], rr_synthetic boolean
) WITH (tsdb.hypertable = true, tsdb.partition_column = 'time',
        tsdb.segmentby = 'user_id', tsdb.chunk_interval = '1 day');
-- motion_features (demo/debug telemetry profile only; cut-if-behind #2)

CREATE MATERIALIZED VIEW hr_1m WITH (timescaledb.continuous) AS
  SELECT time_bucket('1 minute', time) AS bucket, user_id, simulated,
         avg(bpm) AS avg_bpm, stddev(bpm) AS sd_bpm, min(bpm) AS min_bpm, max(bpm) AS max_bpm, count(*) AS n
  FROM hr_samples GROUP BY bucket, user_id, simulated;
SELECT add_continuous_aggregate_policy('hr_1m', start_offset => INTERVAL '1 day',
       end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '1 minute');
ALTER MATERIALIZED VIEW hr_1m SET (timescaledb.materialized_only = false);
SELECT add_retention_policy('hr_samples', drop_after => INTERVAL '48 hours');
SELECT add_retention_policy('hr_1m',      drop_after => INTERVAL '7 days');

CREATE INDEX timers_due ON timers (fire_at) WHERE fired_at IS NULL AND cancelled_at IS NULL;

CREATE FUNCTION audit_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_setting('stillhere.erase', true) = 'on' THEN RETURN OLD; END IF;
  RAISE EXCEPTION 'audit_log is append-only';
END $$;
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_guard();
```

- **Syntax:** `tsdb.*` needs TimescaleDB 2.20 or later; otherwise use `create_hypertable` ([docs](https://www.tigerdata.com/docs/api/latest/hypertable/create_table)).
- **Retention ordering:** the cagg refresh window (1 day) is shorter than raw retention (48 h), so dropping raw chunks never wipes fresh aggregates. The aggregate has its own 7-day retention ([Tiger](https://www.tigerdata.com/docs/use-timescale/latest/data-retention/data-retention-with-continuous-aggregates)).
- **audit_log** columns: `prev_hash`, `hash = sha256(prev_hash || canonical_json(row))`, chained per session. The REVOKE and role split are cut.
- **Erase ("Delete my data"):**
  1. In one transaction: `SET LOCAL stillhere.erase='on'`, delete the user's audit, location and raw HR rows, and insert a tombstone ("timeline erased by user at T").
  2. Then, on an autocommit connection (each refresh batch runs in its own transaction), `CALL refresh_continuous_aggregate('hr_1m', <oldest remaining raw time>, now())` ([Tiger](https://www.tigerdata.com/docs/api/latest/continuous-aggregates/refresh_continuous_aggregate/)).
  3. Older aggregate rows age out within 7 days. §9 says so.
- **Personal timeline retention:** auto-deleted after 30 days unless the user pins it (a daily job using the same erase flag).

### 6.4 Timer worker and outbox

```sql
BEGIN;
SELECT id, session_id, kind, expected_version FROM timers
 WHERE fired_at IS NULL AND cancelled_at IS NULL AND fire_at <= now()
 ORDER BY fire_at LIMIT 20 FOR UPDATE SKIP LOCKED;
-- per timer: SELECT ... FROM sessions WHERE id=$s FOR UPDATE; guarded UPDATE; INSERT next timer;
-- INSERT INTO outbox ... ON CONFLICT (dedupe_key) DO NOTHING; INSERT audit_log; UPDATE timers SET fired_at=now()
COMMIT;
```

- **Timer worker:**
  - The database's `now()` is the only clock.
  - `SKIP LOCKED` lets N workers run safely ([PostgreSQL](https://www.postgresql.org/docs/current/sql-select.html)). It was tested with one worker; the two-laptop test is cut.
  - A `kill -9` before COMMIT rolls back the whole transition, and the next process redoes it from the stored deadline.
- **Outbox sender:**
  - Claim rows with `UPDATE outbox SET status='sending', locked_until=now()+'30 s' WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED LIMIT 10) RETURNING *`.
  - Send, then mark `sent` with the Telegram `message_id`. Lower the prompt deadline when the wearer's nudge is sent.
  - A reaper resets stale `sending` rows.
  - On HTTP 429, set `next_attempt_at = now() + retry_after`, and send at most about 1 message/s per chat ([Bot FAQ](https://core.telegram.org/bots/faq)).
  - Delivery is **at least once**. Tell judges this is a deliberate safety trade-off.

### 6.5 Endpoints

- **Device** (bearer key; a replayed `event_id` returns the cached result):
  - `POST /v1/device/heartbeat`
  - `POST /v1/device/events`
- **Heart rate:**
  - `POST /v1/hr` with `{user_id, source, simulated, samples:[{t, bpm, rr_ms[], rr_synthetic}]}`
- **Wearer** (the `/me` page):
  - `POST /v1/sessions`: arm. Takes a preset (each ends with "Home safe?"), mode and profile. Returns the "who can be alerted" list.
  - `POST /v1/sessions/{id}/checkin`: also captures last-known location if the browser grants it.
  - `POST /v1/sessions/{id}/cancel {pin}`: real and duress PINs return byte-identical responses.
  - `POST /v1/sessions/{id}/end {pin}`
  - `GET /v1/sessions/{id}/timeline`: duress entries stay hidden until resolved.
  - `GET /v1/me/export`: JSON plus a printable HTML page with chain verification.
  - `DELETE /v1/me/data`
  - `POST /v1/contacts`: creates an invite and returns `{t_me_link, web_link, qr_png}`.
  - `POST /v1/contacts/{id}/approve`
  - `DELETE /v1/contacts/{id}`: silent; the contact is not notified.
- **Contact** (cannot arm a session, see session status or request location):
  - `GET /c/{token}`
  - `POST /v1/contact/{token}/join`: the contact is pending until the wearer approves.
  - `GET /v1/contact/{token}/status`: returns `{alert:null}` with **no session data** before an alert.
  - `POST /v1/contact/{token}/ack`
  - `POST /v1/contact/{token}/followup {safe|cant_reach|pass}`
  - `GET /l/{share_token}`: returns **410 Gone** after expiry.
- **Operator** (`X-Admin-Key`; every injected row is tagged `actor='demo'`):
  - `GET /v1/health`
  - `GET /v1/admin/timers`
  - `POST /v1/demo/checkin-now`
  - `POST /v1/demo/inject {head_position|fall_like|offbody}`
  - `POST /v1/demo/hr-source {live|sim:steady|sim:climb_still|sim:lost}`
  - `POST /v1/demo/profile {demo|real}`
  - `POST /v1/demo/fast-forward`
  - `POST /v1/demo/judge-invite`
  - `POST /v1/demo/reset`: must restore the starting state in under 5 s.

### 6.6 Heart-rate rule (output is PULL_FORWARD only)

Every threshold here is an **untested engineering assumption** and is listed in §9.

- **Baseline:** median `avg_bpm` from `hr_1m` over a 5-minute seated calibration **before going out**, with `simulated=false`, stored in `user_baselines`. In the demo, this is done at setup.
- **Every 10 s:** compute the 60 s median from `hr_samples`.
- **Fire** when |median − baseline| ≥ max(30 bpm, 35%) for 90 s (15 s in the demo), **and** the heartbeat's 60-s mean ENMO is below 45 mg.
- **Suppress** for 10 min after ENMO ≥ 100 mg, which covers recovery after dancing ([ENMO cut-points](https://sportsmedicine-open.springeropen.com/articles/10.1186/s40798-025-00820-1/tables/4)).
- **Output:** `PULL_FORWARD(reason='hr_different_from_usual', MED)`, with a 15-min cooldown.
- **Heart rate lost** for more than 120 s while armed: one `PULL_FORWARD` at LOW.
- **UI wording:** "heart rate different from your usual → early check-in". Never "bradycardia" or "tachycardia".
- **Rationale (Q&A only, never on slides):**
  - Alcohol raises heart rate and lowers HRV ([JMIR](https://mental.jmir.org/2018/1/e23)).
  - GHB can **lower** it: bradycardia below 55/min was seen in 30% of 141 Swiss cases, 20% of 104 Barcelona cases and 35% of 88 San Francisco cases ([Liechti & Kupferschmidt](https://smw.ch/index.php/smw/article/download/409/406/806)).
  - A change in heart rate is therefore **non-specific**. It only means "ask sooner", never "it detects GHB".

### 6.7 Telegram flows
- **Create the bot** with @BotFather and name it "Reminders" ([features](https://core.telegram.org/bots/features)).
  - Call `deleteWebhook` once, then long-poll `getUpdates(timeout=25, allowed_updates=['message','callback_query'])`.
  - Store the offset in `bot_state` in the same transaction that processes the update ([Bot API](https://core.telegram.org/bots/api)).
- **Invite:**
  - The link is `https://t.me/<bot>?start=<token>`, using `secrets.token_urlsafe(16)`, single use, with a 10-minute expiry. Deep-link parameters allow up to 64 characters ([features](https://core.telegram.org/bots/features)).
  - On `/start <token>`, bind the `chat_id` as **pending**, and show "Approve <name> as Contact #N?" on `/me`.
  - A bot cannot message someone who has never started it ([Sinch](https://support.sinch.com/hc/en-us/articles/54629170811027-Is-it-possible-to-use-a-Telegram-Bot-to-send-campaign-or-regular-updates-to-Telegram-users)), so **every team phone runs /start at 18:15**.
  - After approval: "You're Maya's trusted contact #1. You'll see nothing from StillHere unless she misses check-ins or asks for help."
- **Alert:**
  - `sendMessage` with `protect_content=true`. Keep the text minimal, with no health data.
  - Text: "StillHere: Maya hasn't responded to 2 check-ins (21:14, 21:14). This is not a confirmed emergency. Last known location (as of 21:02, 12 min old): <link>".
  - `reply_markup` rows:
    - `[{"text":"I'm on it","callback_data":"ack:<a>:<c>"}]`
    - `[{"text":"Can't help – pass it on","callback_data":"pass:<a>:<c>"}]`
    - `[{"text":"Open live page","url":"<PUBLIC_BASE_URL>/c/<token>"}]`
  - `callback_data` is limited to 64 bytes, so use short IDs.
- **Ack:**
  1. INSERT into acks (unique on `update_id`).
  2. Compare-and-set the alert to ACKED.
  3. `answerCallbackQuery`.
  4. `editMessageText`, adding the follow-up buttons "I'm with her – she's safe" and "Can't reach her".
  5. Fan out "<name> is handling it".
- **Wearer chat:**
  - "Reminder", "Reminder N" and "Noted ✓" (identical for OK and duress).
  - At session end, send the timeline's head hash, so later edits are detectable.

### 6.8 QR contact page (Marco owns it; it is the judge moment)
1. **Invite.** `/v1/demo/judge-invite` creates a fresh single-use invite. The QR is **shown on screen**, generated from `PUBLIC_BASE_URL` at demo time, not printed.
2. **Consent and approval.**
   - `/c/<token>` shows one consent screen: "You'll see nothing unless Maya misses check-ins. Tap to ask to become Contact #2."
   - Maya approves on `/me`.
3. **That tap also unlocks the alarm:**
   - Starts a looping `<audio>` element with `muted=true`. Audio elements ignore the iOS mute switch, while Web Audio obeys it ([WebKit 237322](https://bugs.webkit.org/show_bug.cgi?id=237322)).
   - Sets `navigator.audioSession.type='playback'` where available ([WebKit 251532](https://bugs.webkit.org/show_bug.cgi?id=251532)).
   - Resumes an AudioContext ([Chrome autoplay](https://developer.chrome.com/blog/autoplay)).
   - Requests a screen Wake Lock ([wake lock](https://developer.chrome.com/docs/capabilities/web-apis/wake-lock)).
   - POSTs `/join`.
4. **Polling.** The page polls status every second.
5. **On an alert:**
   - Full-screen red, unmute the alarm (the phone **rings**), and show **I'm on it** / **Can't help – pass it on**, then the follow-up buttons.
   - `navigator.vibrate` does not exist on iOS Safari ([MDN](https://developer.mozilla.org/en-US/docs/Web/API/Navigator/vibrate)), so never say "buzzes".
   - FINAL shows the red "Emergency services: SIMULATED" banner.
6. **Spare phone.** The default is a spare phone that has already joined and been approved, offered with "or scan with yours". Bank judges may not want to scan with a work phone.

### 6.9 Chaos and reliability tests (`tests/chaos.sh`, run at 03:30 and 05:00)

| Test | How | Pass criterion |
|---|---|---|
| `kill -9` mid-REMINDED | `kill -9 $(lsof -tiTCP:8000 -sTCP:LISTEN)`; `serve.sh` restarts | L1 fires ≤ 2 s after the stored `fire_at`; exactly one Telegram message; Chrome tabs survive |
| Crash between send and mark | Inject an exception after the HTTP 200 | At most one duplicate (documented) |
| Event replay | Same `event_id` ×5 | One transition; identical cached responses |
| Duress indistinguishable | Diff OK vs duress (HTTP body, device `cmd`, wearer message) | Byte-identical. Timing is not padded (in §9) |
| ACKED follow-up | Ack, then do nothing for 20 s | Re-escalates to the next contact and re-pings the one who acknowledged |
| Final step | Two contacts both ignore | ESCALATED_FINAL with the SIMULATED banner; no call |
| Late squeeze | Squeeze during L1 | State unchanged; "please still check on her" sent |
| Contact approval | Join without approval, then trigger an alert | Unapproved contact gets nothing |
| Unplug device | Pull USB | LOST_CONTACT within lease + 1 s, with its own wording; pending deadline still fires as NO_RESPONSE |
| Telegram 429 | Mock it | `next_attempt_at` honoured |
| Tunnel down | Stop the tunnel for 30 s | Device and Telegram unaffected (both are local or long-poll) |
| DB unreachable | Wrong `DATABASE_URL` | 503s; `bridge.py` buffers; no lost events after recovery |
| **Core rule** | Unit test over all transitions | No path from a sensor or heart-rate event to `ESCALATED_*` |
| Privacy | Status before an alert; location after expiry | No session fields; 410 |
| Wording | `grep -rniE "spik\|intoxicat\|unconscious\|diagnos\|detect\|bradycard\|tachycard\|tamper\|evidence"` over templates, static JS, Telegram strings and dashboard labels | Zero hits |

---

## 7. Frontend: plain HTML/JS pages served by FastAPI on one origin (Next.js and the PWA are cut)

- **Pages:** `/me` (wearer), `/dashboard` (projector), `/c/<token>` (contact), `/hr-bridge` and `/l/<share>`.
- **Libraries:** Chart.js 4.4.1 from cdnjs (`https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js`). There is no build step and no CORS to configure.

### 7.1 Wearer page `/me`

`/me` runs on the laptop or a second phone. Maya's iPhone stays locked.

1. **Arm.**
   - Presets:
     - "Night out: every 30 min until 2 AM, then 'Home safe?'"
     - "Walking home: one check-in in 20 min; ends on PIN or 'I'm home'"
     - "Date: every 45 min"
   - Choose hair or wrist mode, which starts calibration.
   - Show the **"Who can be alerted"** list every time she arms.
   - A heart-rate chip shows LIVE (Apple Watch via HeartCast) or SIMULATED.
2. **Contacts.**
   - Invite by QR or t.me link, and approve pending joins with one tap.
   - Drag to set priority. Remove silently.
3. **PINs.**
   - The cancel and duress PINs must differ (4–6 digits, stored as hashes).
   - The duress PIN screen explains: "Entering this looks exactly like cancelling."
   - Squeeze practice mode shows the decoded result live: OK, DURESS or UNRECOGNIZED.
4. **Active session.**
   - Shows the next check-in, an "I'm OK" button and "Cancel with PIN".
   - Both PINs show the identical "Cancelled ✓".
   - Duress entries stay hidden until a contact resolves the alert.
5. **My timeline.**
   - Entries read "device response at 21:02", not wellbeing.
   - Shows a chain-verified badge, export (JSON or printable HTML) and "Delete my data".
   - Labelled "personal timeline, not evidence".
6. **Accessibility.**
   - Large tap targets and high contrast.
   - Counts, never colour alone.
   - ARIA labels.
   - Every response window can be adjusted up to 10× its default.

### 7.2 Judge-facing dashboard `/dashboard`
- **Top bar:**
  - The state badge (ARMED / PROMPTED / REMINDED / L1 / L2 / ACKED / FINAL / RESOLVED).
  - A countdown computed as `fire_at − now()` from the database.
  - `link_state`.
- **Live signals:**
  - The heart-rate chart from `hr_1m` (real-time aggregate), labelled **LIVE · Apple Watch via HeartCast** with the age of the last sample, or a permanent **SIMULATED SIGNAL** badge.
  - Head angle against θ_drop, and both FSR traces.
  - Event labels:
    - "head position change → early check-in"
    - "heart rate different from your usual → early check-in"
    - "fall-like motion (simulated) → early check-in"
    - "device may be off → early check-in"
- **Prompt mirror:** "Sent to watch: Reminder 3", so judges see what the wearer felt.
- **Timeline:** audit rows with actor tags (device / worker / contact / demo) and chain status.
- **Outbox panel:** recipient, channel, status and attempts.
- **Tiger panel** (cut-if-behind #2): rows per hypertable, the cagg chart, and jobs from `timescaledb_information.jobs`, labelled "retention: configured".
- **Fixed side panel:** the §9 table.
- **Operator console** (behind the admin key): scenario buttons, check-in now, inject, HR source, profile, fast-forward, reset and "judge invite QR".
- **Second window:** `psql` running `SELECT kind, fire_at - now() AS remaining FROM timers WHERE fired_at IS NULL \watch 1`. It keeps counting while the server is dead.

### 7.3 Wording rules (Galit owns every string; the grep in §6.9 enforces them)

| Never say | Say instead |
|---|---|
| "spiked", "drugged", "intoxicated", "unconscious", "diagnose" | "unusual change from your usual, checking in early" |
| "fall detected", "head drop detected", "detect", "detected" | "fall-like motion → early check-in", "head position change → early check-in" |
| "bradycardia", "tachycardia", "abnormal heart rate" | "heart rate different from your usual" |
| "tamper-evident", "tamper-proof", "evidence" | "hash-chained; edits after export are detectable", "personal timeline" |
| "she's OK" (from a device squeeze) | "device response at 21:02" |
| "(battery or signal?)" | "Cause unknown. Her check-in schedule is still running." |
| "the judge's phone buzzes" | "the judge's phone rings" |
| "Statistics Canada doesn't track spiking" | "As of 2018, Statistics Canada filed spiking under 'other assaults'" |
| "meets WCAG" | "response windows adjustable up to 10×, in line with WCAG 2.2.1" |

---

## 8. The demo script (target 3:30; 4:00 hard limit)

**Cast:**
- **Galit:** narrator.
- **Maya (wearer):** the Apple Watch owner. Scrunchie in her hair; HeartCast workout running; iPhone locked.
- **Contact #1:** the Fitbit owner. Telegram on their phone; the Fitbit buzzes on their wrist.
- **Marco:** operator, at the dashboard, `/me` and the terminal.
- **PICkachu:** hardware; does the unplug and replug.

**Pre-demo checklist (T−10 min, and between expo runs):**
- [ ] `/v1/demo/reset` (under 5 s); session armed in the demo profile; hair calibration done (10 s).
- [ ] Apple Watch settings:
  - **Fall Detection off.**
  - **Hold Side Button to Dial off.**
  - **Workout DND / Fitness Focus off.**
- [ ] **iPhone SOS "Call with Hold and Release" and "Call with 5 Presses" off** on every team phone.
- [ ] Start a fresh HeartCast workout (iPhone unlocked, HeartCast in front); confirm the dashboard shows LIVE; **lock the iPhone**.
- [ ] Live nudge test: a "Reminder" taps the watch.
- [ ] `bridge.py` shows heartbeats; the `psql \watch` window is open; `serve.sh` is running.
- [ ] `PUBLIC_BASE_URL` resolves from a phone on cellular; the judge QR is on screen.
- [ ] Spare judge phone joined, approved, page in front, volume up.
- [ ] Contact #1's ringer is on and the Fitbit is charged.
- [ ] Laptop sleep is disabled; the backup video is queued.
- [ ] Charge the watch, Fitbit and phones between expo runs.

| Time | Beat (KAZE scenario) | Action → what judges see | Fallback |
|---|---|---|---|
| 0:00–0:30 | Hook + judge joins | Galit gives the 60-word pitch and hands the judge the spare phone, already approved as Contact #2: "or scan the QR with yours". If the judge scans, Maya taps **Approve** on `/me`: "contacts can't add themselves". The page is calm and shows no data. | Spare phone |
| 0:30–0:45 | Core rule | "Sensors never decide; they only ask sooner." The dashboard shows LIVE Apple Watch heart rate and the demonstrated/simulated legend. | One click to SIM, with the badge |
| 0:45–1:05 | **1 Safe** | Operator "check-in now" (labelled demo). The watch taps "Reminder". Maya double-squeezes and the watch shows "Noted ✓". PROMPTED → ARMED. | Touch sensor; `/me` "I'm OK" |
| 1:05–1:30 | **3 Duress** | Next check-in: Maya squeezes three times and holds for 2 s. The watch shows the **same** "Noted ✓" and `/me` shows "Cancelled ✓". Contact #1's Telegram (and Fitbit): "SILENT HELP REQUEST… Don't call her phone." Contact #1 taps **I'm on it**, then **I'm with her – she's safe**: "an acknowledgement alone never closes an alert." | Duress PIN on `/me` |
| 1:30–2:40 | **2 No response + 5 Unacknowledged + server crash + 4 Connectivity** | Maya slumps her head (held 5 s): "head position change → early check-in". The watch shows "Reminder 3". Maya half-rouses and **reflex double-squeezes**; the dashboard shows "2 ≠ 3, still open". REMINDED: "Reminder 4". **Marco runs `kill -9`**; `psql` keeps counting in Tiger; the server returns. The deadline fires on time and L1 goes to Contact #1, whose Fitbit buzzes, and who **ignores it**. **PICkachu pulls the USB cable**; 10 s later: "We lost contact with Maya's device… Cause unknown." After the 15 s ack window, L2: **the judge's phone turns red and rings**. The judge taps **I'm on it**, then **I'm with her**. Contact #1 sees "Judge is handling it". Replug: "Contact restored". | Operator "inject head position (demo)"; skip `kill -9` if `serve.sh` is unhealthy and play the clip |
| 2:40–3:00 | **6 False alarm + 7 Privacy** | Operator SIM HR "climb while still" (badge shown) → pre-staged fast-forward to L1 (labelled). Maya enters her real PIN on `/me`: "False alarm: Maya cancelled with her PIN at 21:15." The judge's page goes blank; the L2 location link now returns **410**; Export shows chain ✓; the retention job is shown as "configured". | Screenshots in the slides |
| 3:00–3:15 | Differentiation | "Apple Check In and Google Safety Check are great, and both depend on the phone in your hand." | — |
| 3:15–3:30 | Close | §9 slide: "StillHere never claims to detect spiking or unconsciousness. It makes sure someone notices when you stop answering." | — |
| 3:30–4:00 | Slack | — | Privacy beat can shrink to 10 s |

**Q&A cheat sheet (1 minute):**
- **"Why not Apple's fall detection?"** It calls emergency services automatically and has produced floods of false calls ([Macworld](https://www.macworld.com/article/1473802/apple-acknowledges-faulty-crash-detection-skiers.html)). We ask the person first, then call people, not 911.
- **"Why not Apple Check In or Google Safety Check?"** Both need the phone in your hand. See the differentiation slide ([Apple](https://support.apple.com/en-au/guide/personal-safety/ips56b5bc469/web), [Google](https://support.google.com/assistant/answer/13683607)).
- **"False alarm rate?"** Even the best threshold algorithm in a real-fall study gave about 5 false alarms a day ([Bagalà](https://pmc.ncbi.nlm.nih.gov/articles/PMC3353905/)). That is why a false trigger costs only a counted squeeze.
- **"What if someone takes the scrunchie off?"** If the off-body flag is built: a falling elastic pre-load plus stillness pulls a check-in forward, and the watch's heart rate stopping does the same. If it isn't built: "The prototype doesn't detect removal; that's on the roadmap." Never say "its link drops".
- **"What if one friend taps 'I'm on it' and goes back to sleep?"** ACKED follow-up: if they don't confirm "she's safe" within 10 min, the alert moves to the next contact.
- **"What if nobody answers?"** The final step: in production, emergency services or campus safety with last-known location. Here it is simulated, never placed.
- **"Does it detect GHB?"** No. Heart rate can go up or down, so a change only means "ask sooner" ([SMW](https://smw.ch/index.php/smw/article/download/409/406/806)).
- **"How does duress stay hidden?"** The same watch message, the same API bytes, and it is hidden on `/me` until resolved. The response timing is not yet padded (§9).
- **"Location when her phone is locked?"** A web page can't get location while hidden ([W3C](https://www.w3.org/TR/geolocation/)), so we release her last-known location with its age. A native app is on the roadmap.
- **"Why Telegram?"** Native push and watch mirroring at no messaging cost. SMS trials reach only verified numbers ([Twilio](https://www.twilio.com/docs/messaging/guides/how-to-use-your-free-trial-account)). Bot chats are not end-to-end encrypted ([Telegram](https://telegram.org/faq)), so alert text carries no health data.

---

## 9. Demonstrated / simulated / configured / assumed / needs validation

| Item | Status | Assumption or what needs validation |
|---|---|---|
| Durable server countdown, L1 → L2, ACKED follow-up, `kill -9` resume | **Demonstrated** | At-least-once delivery means possible duplicates. Tested with one worker; the two-worker test is designed, not run |
| ESCALATED_FINAL ("would contact 911 / campus safety") | **Simulated, never placed** | A real escalation partner and protocol |
| Telegram delivery and ack; judge web page | **Demonstrated** | Contacts must have started the bot. The page must stay in front on iOS. Real-world channels: Telegram, a Home Screen web app with push (iOS supports web push only for Home Screen web apps ([WebKit](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/))), and SMS in production. Telegram bot chats are not end-to-end encrypted ([Telegram](https://telegram.org/faq)) |
| Wearer approves contacts; single-use 10-min invites; silent removal | **Demonstrated** | Coercive-control scenarios need expert review |
| Squeeze OK / gated duress / counted challenge on the real FSR | **Demonstrated** | Learnability needs testing, with simulated impairment only. False-duress count from replayed traces: **__ of __ traces (fill in at 01:00)** |
| Lost contact distinct from no response | **Demonstrated** (unplug) | Lease length and the 5-min grace in real use |
| Off-body flag | **Demonstrated on the table** / **roadmap** if not built | Pre-load threshold is an assumption |
| Live heart rate from the wearer's Apple Watch (or Charge 6) | **Demonstrated** (data path) | About 5 s updates ([Apple forums](https://developer.apple.com/forums/thread/724724)); relay stays connected; watch worn snugly |
| Heart-rate rule → early check-in | **Simulated** (scripted trace, badged) | Untested engineering assumption: thresholds, false-prompt rate, individual variation |
| HRV / RMSSD | **Not built (roadmap)** | No real-time RR from the Apple Watch ([Apple forums](https://developer.apple.com/forums/thread/746218)) |
| Head position change → early check-in | **Demonstrated** on controlled motion (voluntary head bow) | Resting the head looks the same. Thresholds come from precedents, not validation |
| Fall-like motion | **Simulated** (operator injection) | Threshold detectors averaged 57% on real falls ([Bagalà](https://pmc.ncbi.nlm.nih.gov/articles/PMC3353905/)); the wrist is the weakest site ([UMAFall](https://pmc.ncbi.nlm.nih.gov/articles/PMC5982860/)) |
| Gait unusual for you; prolonged stillness | **Designed, not built** | Needs a personal sober baseline ([Suffoletto](https://www.federvini.it/images/jsad.2020.81.505.pdf)) |
| Location | **Simulated** (fixed UTSC coordinates, last-known, age shown) | A web page cannot get position while hidden ([W3C](https://www.w3.org/TR/geolocation/)); needs a native app with background location |
| Timers in seconds rather than minutes | **Simulated compression** | Real cadence chosen by the user |
| Transport | **Demonstrated tethered** over USB serial (or Wi-Fi, if G6 passed) | Production would use BLE to the phone |
| Device auth: static bearer key; `setInsecure()` if Wi-Fi | **Assumed / disclosed** | HMAC and certificate pinning are roadmap |
| Duress responses byte-identical | **Demonstrated** | Response timing side channel not addressed |
| No data before an alert; 410 expiry; export / delete | **Demonstrated** | Formal privacy review |
| 48 h raw / 7 d aggregate retention; region ca-central-1 | **Configured** (job shown) | Erased aggregates finish ageing out within 7 days |
| Real profile sends heartbeats and events only | **Configured** (profile switch shown) | The demo profile streams features, badged "debug telemetry" |
| Personal timeline hash chain | **Demonstrated** (verify on export; head hash sent to the wearer's chat) | Not evidence. The server could recompute the chain before export |
| Response windows adjustable up to 10× | **Demonstrated** (setting) | Usability with real users |
| Full-night battery (watch, iPhone relay, ESP32) | **Needs validation** | The Series 10's 18 h rating assumes a 60-min workout ([Apple](https://support.apple.com/121202)) |
| Wearable form factor | **Not demonstrated** | Hardware iteration |

---

## 10. Roles, timeline, cut list and go/no-go gates

**Roles:**
- **PICkachu (hardware/firmware):**
  - Cable hunt and upload fixes; borrowing the accelerometer and identifying the chip; wiring and I2C robustness.
  - Firmware: squeeze decoder, challenge, head position, serial protocol, RECORD/REPLAY, LEDs.
  - `bridge.py`; the off-body flag; the Wi-Fi upgrade attempt.
  - Sewing the enclosure with Galit.
- **Marco (backend lead and integrator):**
  - Repo; Tiger setup and network gate; FastAPI, state machine, timers and outbox.
  - Telegram, invites and approval; **the contact page `/c/<token>`**.
  - Tunnel, operator endpoints and the chaos script.
  - Keeps `main` demoable at all times.
- **Cami (data and frontend):**
  - Heart-rate sources and the Fitbit check; `/hr-bridge`; sim traces.
  - Hypertable, caggs, retention and erase; the heart-rate rule.
  - `/dashboard` and `/me`; replaying traces for the false-duress count.
- **Galit (clinical, product, pitch):**
  - Devpost; slides: pitch, differentiation, value, privacy with coercive control, worst-case latency, §9.
  - Every UI and Telegram string and the wording grep; literature for Q&A.
  - The scenario checklist; rehearsal direction; the backup video; sewing.

### 10.1 Cut list (from both reviews), pinned to the timeline

**Cut now (announced at 19:00; none of these appear in the demo):**
- Sway / "gait unusual for you"; prolonged stillness; the nodding trigger.
- The fall state machine and wrist-mode motion calibration. Fall-like motion becomes an operator injection labelled simulated.
- ADXL345 FIFO and interrupt registers.
- Device NVS event persistence (RAM ring only).
- HMAC (use a bearer device key) and CA pinning (use `setInsecure()`, disclosed).
- The ESP32 NimBLE heart-rate client; RMSSD/HRV and the chest strap.
- Next.js and the PWA / Web Push (use plain FastAPI pages).
- The two-laptop worker test; duress timing padding; audit REVOKE and the role split.
- The GoDaddy domain; the Grove buzzer.
- The Fitbit as a heart-rate source, unless it is a Charge 6 that streams within 10 min at 18:30. Otherwise it becomes Contact #1's wrist buzzer.

**Cut if behind** (each decided at the named gate, in this order):

| # | Gate | Cut | Replace with |
|---|---|---|---|
| 1 | **20:15 G3** | Live Apple Watch heart rate | SIM heart rate with the badge visible |
| 2 | **23:30 G5** | Tiger extras | Keep `hr_samples`, the real-time `hr_1m` and both retention policies. Drop `motion_features`, `motion_1m` and the Tiger panel; show jobs with one `psql` query |
| 3 | **23:30 G5** | Telegram polish | Plain `sendMessage` with a link to `/c/<token>`; ack and follow-up on the web page. **Keep the wearer's "Reminders" nudge** |
| 4 | **01:00 G6** | ESP32 Wi-Fi/HTTPS | Stay on USB serial plus `bridge.py` (the default) |
| 5 | **01:00 G6** | Head position on the device | Operator "inject head position (demo)". Squeeze and the counted challenge stay real |
| 6 | **01:00 G6** | Off-body flag | Q&A answer: "roadmap" |
| 7 | **02:30 G7** | Full `/me` page | Keep only arm, contact approval, PIN / duress PIN and export. If still broken, operator-console buttons with identical responses |
| 8 | **03:30 G8** | Export page | JSON download with chain verification |
| 9 | **03:30 G8** | Telegram for contacts | All contacts on the web page. Keep the wearer nudge. If Telegram is blocked entirely, demo in wrist mode with LEDs |
| 10 | **05:00 rehearsal** | Live `kill -9` | Recorded clip, with the `psql \watch` countdown still shown live |

**Never cut:**
- The server-side countdown with L1 → L2 escalation and the ACKED follow-up.
- Duress that looks like a cancel.
- A lost-contact alert distinct from no response.
- The judge QR page with wearer approval.
- The "sensors only pull forward" unit test.
- The SIMULATED labels and the §9 table.
- A real squeeze on real hardware.

### 10.2 Timeline

| Time | PICkachu | Marco | Cami | Galit | Gate / cut decision |
|---|---|---|---|---|---|
| 18:00–18:30 | MLH desk (accelerometers with chip read, Grove-to-female cables, resistor kit, LED sockets, M/F jumpers); cable hunt and data test; ESP32 blink (BOOT button or EN capacitor if upload fails) | Public GitHub repo; Tiger trial in ca-central-1 plus a spare free service; save credentials (the password is shown once) ([Tiger](https://www.tigerdata.com/docs/integrate/find-connection-details)); idle-transaction timeout; `docker pull` TimescaleDB | Check the Fitbit model; install HeartCast; zero-code test on the guyru page; Apple Watch and iPhone safety settings | Create the Devpost project and invite the team; @BotFather "Reminders" bot; Telegram on team phones; everyone runs /start | — |
| 18:30–19:00 | FSR dividers; I2C scanner; accelerometer burst read; squeeze readout on serial | `schema.sql`; FastAPI with pool and lifespan; `serve.sh` and `run.py`; start the tunnel | **Charge 6 10-min test** (if it is one), otherwise set up the Fitbit as Contact #1's buzzer; `/hr-bridge` skeleton | 60-word pitch; strings document; §9 draft | **18:45 G0 network:** `psql` to Tiger and the tunnel on venue Wi-Fi (fixes: `--protocol http2`, Funnel, hotspot, local Docker). **19:00 G1:** ESP32 flashed → Plan A on serial, otherwise **Plan B (final)**. **G2:** accelerometer in hand with chip identified, otherwise head position is injected. **Apply the cut-now list** |
| 19:00–21:00 | Squeeze decoder (classes, duress conditions); counted challenge; serial protocol; LEDs; RECORD/REPLAY | State machine incl. ACKED, FINAL, late squeeze, duress mask, PIN rules; delivery-based deadlines; outbox; audit chain; unit tests incl. the core rule | `/hr-bridge` → `/v1/hr` → `hr_samples`; sim traces; hypertable, `hr_1m`, both retention policies | **Initial Devpost submission by 20:45**; slide skeleton; differentiation and value slides | **19:30 G3:** live heart rate in Chrome, otherwise HeartBLE until 20:00, Charge 6 until 20:15, then **SIM only (cut #1)**. **20:45 G4:** Devpost initial submission done |
| 21:00–23:30 | `bridge.py` (bearer key, downlink, buffer, port reopen); head-position calibration and triggers; I2C re-init | Telegram poller (dedicated lock connection); invites, approval, alerts, ack and follow-up buttons; device endpoints and idempotency; **`/c/<token>` page** (audio unlock, polling, buttons) | Heart-rate rule (ENMO gate, 10-min suppression, baseline); `/dashboard` v1 (state, countdown, HR chart, timeline, prompt mirror, §9 panel, operator console) | Slides v1; Q&A sources; start sewing the hair pocket with PICkachu | **23:30 G5:** scenario 2 end to end with simulated device events (PULL_FORWARD → challenge → REMINDED → L1 → L2 → ACKED → RESOLVED) plus a rehearsed `kill -9` by port. Otherwise **cuts #2–#3** |
| 23:30–01:00 | Real squeeze, duress and challenge reach the server through `bridge.py`; record traces with Cami (dance, sit back against a headrest, lie down, hug, slump); Wi-Fi upgrade only if all of this works | Integrate; lease and LOST_CONTACT (real vs demo behaviour); last-known location, release and 410; FALSE_ALARM; export, delete and erase flag | `/me` (presets with "Home safe?", who-can-be-alerted, approval, PINs, squeeze practice); **REPLAY traces → false-duress count** | Scenario checklist; QA; first wording grep | **01:00 G6:** real hardware path on serial (squeeze, challenge, head position, unplug → LOST_CONTACT → restored). Wi-Fi soak passes or **cut #4**. Head position works or **cut #5** |
| 01:00–03:30 | Enclosure fit (ESP32 and accelerometer in the pocket; FSR placement); off-body flag (30 min) or **cut #6**; 30-min nap | Chaos script; FINAL banner; `/demo/reset` < 5 s; judge-invite QR from `PUBLIC_BASE_URL`; README | Dashboard polish; Tiger panel (if not cut); export page | **Nap 01:00–02:30**, then narrate over the partial demo; slides | **02:30 G7:** judge page via the public URL on a phone on cellular rings with the mute switch on; `/me` complete or **cut #7**. **03:30 G8: FEATURE FREEZE.** Anything broken becomes labelled simulation (**cuts #8–#9**) |
| 03:30–05:00 | Fix blockers; 30-min nap | Chaos run #1; fixes | Polish; 30-min nap | Wording audit (grep); final slides; fill in the false-duress count in §9 | — |
| 05:00–06:00 | All: three stopwatch rehearsals (target 3:30, with at least 20 s of slack); chaos run #2 | ← | ← | Record the backup video | **05:00:** `kill -9` healthy in rehearsal, otherwise **cut #10**. **05:45 G9:** video recorded |
| 06:00–07:00 | Photos of the hardware in hair and on the wrist | Tag `v1.0`; final README with AI disclosure | Screenshots | Final Devpost text | **07:00 G10: submit** (hard close 08:00) |
| After submission | Sleep in turns. Laptop sleep stays off and the tunnel stays up. Arrive one hour before your judging slot and run the pre-demo checklist | | | | |

---

## 11. Devpost submission checklist

- [ ] **By 20:45 Sat:**
  - Project created at hack-the-valley-11.devpost.com.
  - All four teammates invited **and accepted**.
  - Categories selected: Project KAZE, Best Healthcare & Well-being Hack, [MLH] Best Use of Tiger Data.
  - Title, tagline, placeholder description and repo link.
  - The initial submission is due 9 PM ([Rules](https://hack-the-valley-11.devpost.com/rules)).
- [ ] **Public GitHub repo** linked. "Your project will not be judged if you do not submit this" ([Devpost](https://hack-the-valley-11.devpost.com/)).
- [ ] All code written after **Fri 8 PM ET**, and the git history shows it. No prior projects; open-source libraries are fine ([Rules](https://hack-the-valley-11.devpost.com/rules)).
- [ ] The write-up covers why you built it, the tech stack and what it does ([Devpost](https://hack-the-valley-11.devpost.com/)).
- [ ] **KAZE section:**
  - The §9 table, including the "designed, not built" rows and the false-duress count.
  - "Simulation-only testing; impairment simulated only; no real emergency calls; no detection or diagnosis claims."
  - The differentiation from Apple Check In and Google Safety Check.
- [ ] **Tiger Data section:** the hypertable, real-time continuous aggregate, columnstore, retention as privacy (48 h / 7 d), timers in the same database, and ca-central-1.
- [ ] **AI disclosure.** MLH requires stating how AI was used and a README that credits tools and separates what you made from what was generated. It disqualifies teams that don't credit AI use ([MLH rules](https://guide.mlh.com/general-information/judging-and-submissions/rules-for-your-hackathon)). The README "AI usage" section says:
  - Claude Code (Claude Max) was used for scaffolding and boilerplate in named directories.
  - The humans designed the state machine, safety rules, thresholds and pitch.
  - There is no generative AI in the product runtime.
  - Answer the Devpost GenAI question if the form has one ([MLH portal guide](https://guide.mlh.com/general-information/judging-and-submissions/hackathon-submission-portal)).
- [ ] **Media and tags:**
  - Backup video (2–3 min).
  - Screenshots: dashboard, judge page (calm and red), Telegram alert with the follow-up buttons, and the scrunchie in the hair and on the wrist.
  - "Built with" tags: ESP32, Arduino, FastAPI, Chart.js, Tiger Data/TimescaleDB, Telegram Bot API, Web Bluetooth, Cloudflare Tunnel.
- [ ] Final submit at **07:00**, then confirm the "Submitted" status and every category before 08:00 EDT.

---

## 12. Top risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | ESP32 can't be flashed (no micro-USB data cable, or upload timeouts) | Hunt at 18:05 and test for a new serial port. Hold BOOT, or fit the EN capacitor ([RNT](https://randomnerdtutorials.com/solved-failed-to-connect-to-esp32-timed-out-waiting-for-packet-header/)). **Plan B (Uno) final at 19:00.** Never flash through the Uno ([esp32.com](https://esp32.com/viewtopic.php?p=132576)) |
| 2 | No accelerometer, or an unknown chip | I2C scanner; ADXL345 or LIS3DHTR driver path ([Seeed](https://wiki.seeedstudio.com/Grove-3-Axis-Digital-Accelerometer-LIS3DHTR/)); otherwise head position is injected and labelled |
| 3 | Intermittent Grove-to-ESP32 wiring hangs I2C | Grove-to-female cable from the desk, or folded and taped joints; 100 kHz; `setTimeOut(20)`; re-init after 3 errors ([Arduino-ESP32](https://docs.espressif.com/projects/arduino-esp32/en/latest/api/i2c.html)); ESP32 next to the sensor; Plan B needs no cutting |
| 4 | HeartCast not discoverable from Chrome | Unlocked iPhone with HeartCast in front while connecting ([App Store](https://apps.apple.com/us/app/heartcast-heart-rate-monitor/id1499771124)); then HeartBLE, then Charge 6, then SIM, all by 20:15; rehearse the switch |
| 5 | **A watch or phone places a real emergency call** | Fall Detection off ([Apple](https://support.apple.com/en-gw/guide/watch/apd34c409704/watchos)); Hold Side Button to Dial off ([Apple](https://support.apple.com/108374)); iPhone SOS auto-call off on every team phone ([Apple](https://support.apple.com/en-us/104992)); all on the T−10 checklist |
| 6 | The wearer's watch tap is silently lost | Workout DND / Fitness Focus off ([How-To Geek](https://www.howtogeek.com/711255/how-to-enable-do-not-disturb-during-workouts-on-apple-watch/)); iPhone locked for the whole demo; PINs on `/me` elsewhere; live nudge test at T−10 |
| 7 | Venue network blocks the tunnel or Tiger's port | 18:45 G0; `--protocol http2` ([Cloudflare](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/run-parameters/)); Tailscale Funnel ([Tailscale](https://tailscale.com/kb/1223/funnel)); laptop on a hotspot; local Docker TimescaleDB |
| 8 | Tunnel URL changes | The tunnel runs as its own process; QR generated on screen from `PUBLIC_BASE_URL`; device and heart rate use `localhost`; laptop sleep off; never restart the tunnel after 05:00 |
| 9 | `kill -9` takes down the restart loop or Chrome, or the server won't start on Windows | `serve.sh` (no "uvicorn" in the name); `lsof -tiTCP:8000 -sTCP:LISTEN` ([lsof](https://man7.org/linux/man-pages/man8/lsof.8.html)); `WindowsSelectorEventLoopPolicy`, or macOS/WSL ([psycopg](https://www.psycopg.org/psycopg3/docs/advanced/async.html)) |
| 10 | HR bridge stalls in a background tab and fires "heart rate lost" on stage | POST inside the notification handler; the bridge in its own visible window ([Chrome](https://developer.chrome.com/blog/timer-throttling-in-chrome-88)); a Mac or Windows laptop |
| 11 | Judge's phone is muted, backgrounded or an iPhone | Muted audio loop unlocked on tap; wake lock; full-screen red; say "rings" ([MDN](https://developer.mozilla.org/en-US/docs/Web/API/Navigator/vibrate)); pre-joined spare phone by default |
| 12 | Telegram 403 or 429 | Everyone runs /start at 18:15; the outbox honours `retry_after` ([FAQ](https://core.telegram.org/bots/faq)); one poller on its own lock connection |
| 13 | Accidental silent duress | `[S,S,S,HOLD≥2 s]` with both FSRs pressed; orientation and pressure-artifact rejection; wake gate outside prompts; replay-trace count in §9; the duress PIN as the primary path |
| 14 | Overclaiming (KAZE red line) | Galit owns the strings; the expanded grep; SIMULATED badges; the §9 table on the dashboard and in Devpost; GHB physiology only in Q&A |
| 15 | False triggers on stage | Demo profile; head position needs a held, quasi-static angle; every trigger only causes a check-in |
| 16 | Demo runs past 4:00, or the expo needs many runs | 3:30 script; three stopwatch rehearsals; reset in under 5 s; charge between runs; backup video |
| 17 | Wi-Fi upgrade flakes on stage (hotspot, 2.4 GHz, rejoin) | Serial is the default; Wi-Fi only after passing the G6 soak; ASCII SSID, Maximize Compatibility, parked hotspot screen ([Apple](https://support.apple.com/guide/platform-support/sup025284ac0)) |
| 18 | Countdown stalls after a network blip | `idle_in_transaction_session_timeout=10s` ([PG](https://www.postgresql.org/docs/current/runtime-config-client.html)); TCP keepalives; prompt deadlines have a +30 s fallback, so they always fire |
| 19 | Integration problems at 3 a.m. | Cut-now list applied at 19:00; gates G0–G10, each with a named cut; freeze at 03:30; nap rotation; `main` always demoable |