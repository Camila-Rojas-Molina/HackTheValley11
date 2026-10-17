# Hack the Valley 11: final recommendation

**Bottom line**
- **Build SqueezeBack**, which is Tether renamed and hardened. Put full effort into **Project KAZE** ($1,500 / $500 cash) and the **overall top 3**, which every project is judged for automatically. On Devpost, also tick Best Healthcare & Well-being, Best AI for Social Impact and MLH Gemini, but build nothing extra for them.
- **Each team can submit only one project.** The rules say "Each team may submit only one project as their final entry" (https://hack-the-valley-11.devpost.com/rules, checked today). Entering Tether for KAZE and Unasked for PointClickCare (PCC) is not allowed, so choose one lane now.
- **Deadlines (checked today):**
  - The initial Devpost entry is due **Saturday 9:00 PM ET**, with team members and prize categories. Tick KAZE in that entry.
  - The Devpost page header says the final deadline is **Sunday 8:00 AM EDT**. The rules page says 10:00 AM. Treat 8 AM as the real one.
  - The rules put the expo and judging on **Sunday 12–3 PM**, not in the morning. Confirm on Discord.
- **Rename before 9 PM.** devpost.com/software/squeezeback is free (404 today). The name "Tether" was used by a winning project at the UIUC Product Hackathon two weeks ago (https://devpost.com/software/sfdsf).
- **Write all code this weekend.** Do not reuse earlier Tether code or Cami's existing delirium model. The rules require code written after Friday 8 PM.

---

## 1. Ranking of the 8 shortlisted ideas

**How the score works**

`Score = 0.35 × Panel + 0.30 × Track fit + 0.15 × Novelty + 0.20 × Prize upside`

- **Panel** combines the five panel-judge scores: explain 20%, wow 25%, technical 15%, demo reliability 25%, impact 15%. Wow and reliability get the most weight because each judge ranks a top 3 from their batch, and a demo that fails on stage scores close to zero.
- **Track fit** = 0.7 × fit with the main track + 0.3 × fit with the second track, taken from the sponsor-rubric scores.
- **Novelty** is the score from the prior-art check.
- **Prize upside** is 9 when the main track is KAZE: it is the only real cash prize, and its single judge is on site, so you can talk to her. It is 5 when the main track is PCC, whose prizes are air fryers and Lego Gameboys.
- **Check without the prize term** (0.45 Panel + 0.35 Fit + 0.20 Novelty): the PCC ideas edge ahead. SqueezeCheck scores 6.66, RiseScore 6.63 and SqueezeBack 6.47. That gap is within the noise of the inputs. The cash prize, a demo that makes a real phone ring, and the team's strengths decide it.

| # | Idea | Best two tracks | Panel scores (explain / wow / technical / reliability / impact) | Fit: KAZE / PCC / Healthcare | Novelty | Score | Call |
|---|---|---|---|---|---|---|---|
| 1 | **SqueezeBack** (Tether) | KAZE + Healthcare | 8 / 7 / 6 / 5 / 6 | 8 / 2 / 6 | 5 | **7.01** | **Build it. Final pick.** |
| 2 | On Your Side | KAZE + Healthcare | 8 / 7 / 5 / 5 / 5 | 7 / 2 / 6 | 6 | 6.85 | Fallback only. Shares the escalation engine, but the pose detection and the safety framing are both risky. |
| 3* | RiseScore | PCC + Healthcare | 8 / 6 / 5 / 5 / 5 | 2 / 8 / 8 | 6 | 6.35 | The best choice if the team prefers PCC plus a top-3 shot over cash. |
| 4* | SqueezeCheck | PCC + Healthcare | 7 / 6 / 5 / 6 / 5 | 2 / 8 / 8 | 6 | 6.37 | Same lane as RiseScore with a lighter build, but "delirium" is harder to explain to bankers. |
| 5 | Knock | KAZE + MLH Tiger Data | 8 / 5 / 4 / 6 / 5 | 7 / 2 / 5 | 4 | 6.32 | Drop. Brave Sensor already sells this to bars (https://www.brave.coop/sensor). |
| 6 | Noticed | PCC + Healthcare | 7 / 6 / 6 / 5 / 6 | 2 / 8 / 7 | 5 | 6.14 | Drop. PCC's own eINTERACT already sends aide-to-nurse alerts (https://pointclickcare.com/products/einteract/), and a live voice demo is fragile. |
| 7 | TrayTrace | PCC + Healthcare | 8 / 5 / 6 / 6 / 6 | 1 / 7 / 7 | 5 | 6.00 | Drop. Lowest wow, and Smart Coaster already won with the same coaster idea (https://devpost.com/software/smart-coaster). |
| 8 | Unasked | PCC + Healthcare | 6 / 6 / 6 / 4 / 5 | 4 / 7 / 7 | 4 | 5.57 | Drop. The most moving parts, a crowded market (Inspiren https://inspiren.com/hardware/beacon, Tunstall https://www.tunstall.co.uk/products/personal-safety/bed-chair-occupancy-sensor/), and one doorway beam that can clear its own alarm. |

\*RiseScore and SqueezeCheck are 0.02 apart, which is a tie. RiseScore takes the #3 spot because falls need no clinical explanation for bank judges (explain score 8 vs 7), and because the judge's own body raises a physical flag, which helps in the overall top 3.

---

## 2. The top 3 in depth

### 2.1 SqueezeBack (the final pick)

**30-second explanation for the judges**
> "SqueezeBack is a liveness probe for a person on a night out. Every 20 minutes the band on your wrist buzzes. You squeeze twice without looking or unlocking anything, and nothing else happens. If you miss two in a row, or the band dies, a server (rules, not AI) phones the friends you picked, one at a time. It tells them when you last checked in and where, and keeps going until someone presses 1 for 'I'm going'. Then your wrist blinks blue so you know help is coming. It doesn't detect spiking. It notices that you went quiet."

**Why it wins KAZE and the overall top 3**
- **The brief's own words.** KAZE asks for support "even when they cannot make a call, send a message or consciously activate an SOS" (checked today at https://hack-the-valley-11.devpost.com/). In SqueezeBack, silence is what triggers help.
- **KAZE rubric, criterion by criterion:**
  - Impact (30): it covers any night out, and the group rule covers a whole group that gets spiked together.
  - Safety & reliability (25): timers run on the server, a lost heartbeat counts as a missed check-in, escalation survives a server crash, and failures are injected live on stage.
  - Usability (20): the person only has to squeeze a fist. No screen, no unlocking.
  - Feasibility (15): roughly $20–30 in parts per band (an estimate), and cents per escalation.
  - Privacy (10): both the wearer and each contact consent, location is shared only during an incident, and the log deletes itself after 72 hours.
- **All 7 brief scenarios have an answer:**

  | Scenario | How SqueezeBack handles it |
  |---|---|
  | Safe | Double squeeze |
  | No response | Call ladder |
  | Duress | A long hold that looks like a cancel |
  | Connectivity failure | Heartbeat loss counts as a missed check-in |
  | Unacknowledged alert | Contact B, then the wearer's own phone, then a simulated 911 |
  | False alarm | Cancel window |
  | Privacy | Consent, location only during an incident, auto-delete |

- **The judge.** Swapna Samuel is the only KAZE judge. She is MD & Head, FinOps & Value Management at BMO. A cost line and the sentence "the AI never decides when to escalate" speak her language.
- **Overall top 3.** An ESP32 plus sensors, a live loop, and an alert to someone else is the Impactify pattern that won HTV 9 (https://devpost.com/software/impactify). Here the judge holds the device and their own phone rings.

**4-minute demo script**
Use a persona, "Maya", never the judge's real name. Shorten every timer to about 10 seconds.
Roles: Marco presents, PICkachu runs the hardware, Cami drives the dashboard, Galit runs the slides and keeps time.

| Time | What happens | Who |
|---|---|---|
| 0:00–0:20 | "Maya's friends left. She's on a stranger's couch and can't unlock her phone. Google's own Safety Check says, 'If your phone is locked, you may need to unlock it.' This is the opposite of a panic button: silence is the trigger." | Marco |
| 0:20–0:40 | Judge 1 straps on the band. Judge 2 scans the QR code, which opens a consent page and saves "SqueezeBack" as a contact, making them contact B. If Swapna is judging alone, a teammate wears the band and she is the contact. | PICkachu |
| 0:40–1:00 | The band buzzes. The judge squeezes twice. The beacon shows "OK", the band goes green. | Judge |
| **1:00–1:50** | **Judge takes part.** At the next buzz the judge ignores it. The band re-buzzes and the LED goes red. That is miss 1, which sends contact A a text: "missed one check-in, no action yet". The judge ignores the second buzz too. That is miss 2: the servo snaps a HELP flag up and the beacon counts down. Contact A, a teammate, declines the call. The judge's phone rings on speaker: "This is SqueezeBack calling about Maya. She has not answered two check-ins since 10:41. Last known location: Highland Hall. Press 1 if you are going to her." The judge presses 1. The flag drops, the display reads "ACK", the band blinks blue, and the dashboard shows "acknowledged in 47 s". | Judge, Cami |
| 1:50–2:20 | **Engineer moment.** PICkachu unplugs the band's power. About 10 s later the dashboard shows "heartbeat lost: treating as missed check-in", and the call ladder starts on its own. Plug it back in. | PICkachu |
| 2:20–2:35 | (Optional) **Duress.** A long hold shows the same green "cancelled" blink as a real cancel, while the dashboard quietly opens a duress incident. | PICkachu |
| 2:35–3:10 | **Evidence slide.** The miss rate you measured in the hall tonight, plus a trade-off chart: false calls per night against minutes until a phone rings. "One miss costs a text; two cost a call." | Cami |
| 3:10–3:45 | **Demonstrated / Simulated / Assumed slide** plus the cost line. Credit borrowed ideas openly: "the call ladder comes from PagerDuty, the duress hold from bank alarms." | Galit, Marco |
| 3:45–4:00 | Close: "A liveness probe for a person. If she stops answering, or the band dies, it calls her people." | Marco |

Have a 3-minute cut ready in case you present again on stage: drop the duress moment and shorten the evidence slide.

**The Demonstrated / Simulated / Assumed slide**
- **Demonstrated:** the band, the squeeze, the buzz, the server timers, a real call to a real phone, pressing 1 to acknowledge, and recovery from heartbeat loss.
- **Simulated:** the 911 step (the dispatcher packet is shown on screen and never dialled), the second group member, the shortened check-in interval, and the fixed location.
- **Assumed:** the wearer turns it on and charges it while sober, location comes from her phone, and her contacts have signal and have saved the number.

**Hardware, mapped to what's on the table**

| Item | What it does |
|---|---|
| ESP32 NodeMCU-32S | The band's controller. Connects through a phone hotspot and sends a heartbeat every 2 s. |
| Ohmite FSR #1 (the one with 3M adhesive) | Sits inside a velcro strap or sweatband. Wired through a 10 kΩ divider to GPIO34, an ADC1 pin. ADC2 pins don't work while Wi-Fi is on (https://docs.espressif.com/projects/esp-idf/en/stable/esp32/api-reference/peripherals/adc/adc_oneshot.html). |
| Round-disc Grove module | The haptic buzzer, if it turns out to be the Grove Vibration Motor. If it is a touch sensor, it becomes a second "I'm OK" input and you borrow a coin vibration motor. |
| Green, amber, red and blue 5 mm LEDs with 220–330 Ω resistors | Show the band's state. |
| White pack | Powers the band only if it is a power bank that doesn't switch itself off at low current. Test it for 30+ minutes. If it fails, power the band from laptop USB and pull that cable for the failover demo. |
| Arduino Uno + Grove Base Shield | The desk beacon, connected over USB serial. |
| Quad alphanumeric display (I2C) | Shows "OK", "SQZ?", "0:25", "HELP", "ACK". |
| Grove servo | Raises a paper HELP flag when an incident opens and drops it on acknowledgement. |
| FSR #2 | A big "I'm going" pad on the beacon. |
| Not used | The ultrasonic sensor and the capacitor. |

**Borrow from the MLH lab:**
- Grove-to-female jumper cables
- A coin vibration motor (insurance)
- A LiPo or USB battery that doesn't auto-shut-off
- A second ESP32, only if you want a live two-band demo of the group rule
- Hot glue, heat-shrink, spare USB cables, and a USB-C-to-HDMI adapter

MLH's hardware guide says to plan about 30% of your time for physical packaging, hot-glue joints, and write a reset checklist (https://guide.mlh.com/hardware-hackathon-guide/pitfalls).

**AI and data: why this isn't "an LLM over a public dataset"**
- **No public dataset.** All the data is collected this weekend:
  1. Pressure traces from each wearer, used to calibrate thresholds for a double squeeze, a long hold, steady pressure and a bump. Thresholds are enough; skip a trained classifier unless they fail.
  2. A study in the hall tonight: 20–30 sober hackers wear the band while walking, wearing headphones, and during the loud midnight event. It measures time from buzz to squeeze and the miss rate. KAZE allows simulation-only testing, so nobody drinks.
  3. 10,000 simulated nights using those measured rates, producing the trade-off chart. Label every other input as "assumed".
  4. A replay harness that injects failures: Wi-Fi drop, battery death, contact A declining, the server killed mid-escalation, a false alarm cancelled. Its headline number is the time from a missed check-in to a human pressing 1.
- **Deterministic core.** The escalation logic is a FastAPI server with a Postgres job table that tracks each incident's state. The AI never decides when to escalate. Bank engineers know this approach from model-risk rules (OSFI E-23, https://www.osfi-bsif.gc.ca/en/guidance/guidance-library/guideline-e-23-model-risk-management-2027).
- **Gemini has one job.** Gemini 2.5 Flash writes the 15–25 s call script and the responder card from the incident data. Its output is checked against a fixed format: it must include name, last check-in time, number of misses and location, and it must make no medical claims. If Gemini is slow, a fixed template goes out instead. That makes MLH Gemini a free tick, not a track to chase.

**The killer question, and the answer**
> **Q:** "You tested sober people. Someone drunk but fine, dancing, will miss buzzes. How many false 1 AM calls does that make, and why doesn't she turn it off after the second one?"
>
> **A:** "We tested [N] people tonight walking the hall in headphones during the midnight event. [X]% missed one buzz, [Y]% missed two in a row. One miss only sends a heads-up text. Only two misses in a row, a dead band or a duress hold rings a phone. At a 20-minute interval that's [Z] false calls in a 4-hour night. She can lengthen the interval, and any double squeeze resets the timer, so checking in early never nags. This chart is the trade-off: a tighter interval means faster help and more false calls. It's the same trade-off as fraud false declines."
> Fill in N, X, Y and Z from the study. If you don't have the numbers, say so.

> **Likely follow-up:** "Google Safety Check already does this."
> **Answer:** "Safety Check is one scheduled check. It may need the phone unlocked, it texts contacts with no acknowledgement, and it acts on a dead phone only at the scheduled time (https://support.google.com/pixelphone/answer/7055029?hl=en). Ours is a recurring check you answer without looking, a call ladder that stops only when a human presses 1, and a band that tells her help is coming."

**Credit prior art openly.** Batman won 1st at HackNC 2026 with check-in-if-silent and "the LLM never decides to escalate" (https://devpost.com/software/batman-cjer91). FieldLine built a call-by-call ladder with a duress phrase (https://devpost.com/software/fieldline). Claim only what is new: the squeeze loop that needs no eyes, a dead band counting as a miss, the acknowledgement sent back to the wrist, and the group rule.

**Build plan for the remaining hours**

| Time block | PICkachu (hardware) | Marco (backend, UI, presenter) | Cami (data) | Galit (rubric, pitch, Devpost) |
|---|---|---|---|---|
| First 30 minutes | Check what the disc module and the white pack really are. Create the repo and commit often, because MLH's cheating check reads commit history. | Upgrade Twilio and buy a 416/647 number. | Set up the pressure-trace logger. | Ask Swapna one question at her booth. |
| Now → 9 PM | Pressure divider, squeeze patterns, LEDs, buzzer and heartbeat on the ESP32. | FastAPI + Postgres state machine. Twilio voice call with "press 1" (12–15 s ring timeout; only a pressed 1 counts), SMS, and the QR consent page that saves a contact card. | Per-wearer thresholds, the study protocol, a first version of the simulation. | **Devpost initial entry by 8:30 PM**: name, team, tick KAZE + Healthcare + Social Impact + Gemini. Also the 7-scenario table, the Demonstrated / Simulated / Assumed slide, consent wording, and Twilio Canada rates for the cost line. |
| **Milestone, 9 PM** | A missed squeeze rings a teammate's phone, end to end. | | | |
| 9 PM → 1 AM | Beacon (Uno, display, servo flag, "I'm going" pad). Package the band: strap, hot glue, strain relief. | Heartbeat-loss watchdog. Resume escalation after a server crash. Next.js responder dashboard with a timeline and buttons to inject failures. Gemini script with format check and fallback. | **Run the hall study at the midnight event.** Simulation and trade-off chart. Replay harness. | Pitch script v1, five slides, Q&A cards, first timed run-through. |
| 1 AM → 4 AM | Harden the unplug demo. Write the reset checklist card. | Duress hold. Morning-after timeline that deletes after 72 h. Fix integration bugs. | Put the final numbers into the slides. | **Record backup videos around 2–3 AM** (one 60 s, one full 4 min). |
| 4 AM (feature freeze) → 8 AM | Everyone: sleep in pairs from 4 to 6 AM. Rehearse 5 times, once in front of someone outside the team. Finish the README (libraries used, Gemini, Twilio, what each person built). Put the Demonstrated / Simulated / Assumed table on Devpost. **Submit by 7:30 AM.** | | | |

**Cut first:** the Gemini Live bystander call, then the group rule (keep it as one slide), then Tiger Data, then any trained classifier.
**Never cut:** the real call to a real phone with acknowledgement, and the heartbeat-loss failover.

---

### 2.2 On Your Side (backup KAZE idea)

**30-second explanation** (reworded after the stress test: never say "passed out")
> "When a friend has had far too much, the advice is to put them on their side and stay with them. But the sober friend has to sleep eventually. On Your Side only turns on if she can still be woken. A laptop camera watches whether she rolls onto her back, and a pad in her pillow lets her answer with a squeeze. If she rolls over, it asks her to roll back, then taps her, then phones you until you press 1, then a roommate. Nothing is recorded; only alerts leave the laptop."

**Why it can win**
- KAZE fit is 7 because the person at risk does nothing at all. It also fits Best Healthcare.
- Webcam computer vision has won HTV before: Heckler.ai took 1st at HTV 8 (https://hack-the-valley-8.devpost.com/project-gallery).
- It has the highest novelty of the KAZE ideas (6). Its ancestor, Drinking Buddy, won only minor prizes and listed this design's features as future work (https://devpost.com/software/drinking-buddy-e61tig).

**4-minute demo**

| Time | What happens |
|---|---|
| 0:00–0:20 | "The NHS says stay with them. It also says call 999 if they can't be woken (https://www.nhs.uk/conditions/alcohol-poisoning/). So this only turns on if she can." |
| 0:20–0:45 | **Arming check.** The teammate on the mat answers a check-in with a squeeze. The stick figure turns green ("ON SIDE"). A panel showing exactly what data leaves the laptop stays visible. |
| **0:45–1:50** | **Judge takes part.** Cami rolls onto her back. "BACK 0:08" turns red, the pillow buzzes, and a voice says "Cami, roll onto your side." No response, so a servo "finger" taps the pillow. The team phone handed to the judge rings twice. The judge presses 1, the display reads "OTW", and a recovery-position card appears. **The judge physically rolls Cami onto her side**, the screen goes green, and the log reads "repositioned 01:52". |
| 1:50–2:15 | **False alarm.** She raises a hand, and the room says, "Glad you're okay. Checking again in 30 min." |
| 2:15–2:40 | **Failure.** Cover the camera. The state becomes "NOT VISIBLE", and the system falls back to pillow check-ins; it never goes silent. |
| 2:40–3:20 | **Model card.** Show results with each teammate held out in turn, and false nudges per hour. MediaPipe's own model card says it is "not intended for human life-critical decisions", so the design rule is: the camera can only make it louder, never quieter. |
| 3:20–4:00 | Demonstrated / Simulated / Assumed, privacy (processing on the laptop, stick figure only, stops at noon, morning log goes to her), close. |

**Hardware**
- Laptop webcam high on a borrowed stand, cropped to the mat, with a desk lamp.
- ESP32 with FSR #1 under the pillowcase (ADC1 pin), the vibration motor inside the pillow, and four LEDs.
- Uno with the Grove servo as the tapping "finger" and the quad display ("WTCH", "BACK", "WAKE", "OTW").
- FSR #2 as the host's "I'm here" pad. The ultrasonic sensor is optional.
- **Borrow:** a yoga mat, a pillow, a tripod or phone stand, and Grove-to-jumper cables.

**AI and data**
- 1,000–1,500 labelled frames of the four teammates (both sides, back, face down, sitting; two angles; with and without a blanket).
- MediaPipe extracts body landmarks, and a simple logistic-regression model sorts them into BACK, NOT BACK or NOT VISIBLE.
- Evaluate by holding out one teammate at a time, and publish a model card listing known failures: a dark room, a duvet, facing the wall (reads as NOT VISIBLE), two people on the couch.
- Fixed rules sit on top and fail toward escalation. Gemini, if used at all, only words the messages.
- It is the team's own data and the team's own measured model.

**The killer question, and the answer**
> **Q:** "The NHS says call 999 if someone can't be woken, and MediaPipe's model card says it's 'not intended for human life-critical decisions' (https://storage.googleapis.com/mediapipe-assets/Model%20Card%20BlazePose%20GHUM%203D.pdf). Aren't you giving a tired friend permission to go to sleep instead of calling 911?"
>
> **A:** "That's why it refuses to turn on unless she answers a check-in, and why every alert says 'if you can't wake her, call 911'. The camera can only escalate. The pillow check-in every 20 minutes works in the dark, and losing sight of her also escalates. It's a backup to staying with her, not a replacement."

**Build plan**
- **Hour 0–1:** all four record labelled frames.
- **PICkachu:** pose pipeline at about 10 frames per second (hours 1–6), then the pillow pad and servo (hours 6–9).
- **Cami:** classifier, held-out evaluation and model card (hours 1–5).
  **Go/no-go at hour 2:** if back vs. not-back on the mat isn't reliable (at least 90% recall on BACK with each teammate held out), switch to SqueezeBack. The escalation engine carries over.
- **Marco:** escalation engine (shared with SqueezeBack), calls that repeat until acknowledged, the stick-figure dashboard with the privacy panel (hours 0–8).
- **Galit:** NHS and NIAAA wording, the arming-check text, the consent flow, the Demonstrated / Simulated / Assumed slide, the pitch.
- **Cut first:** Gemini, then the servo, then the ultrasonic, then scheduled checks.
- **Never cut:** the arming check, the room saying her name, and the phone ringing.

---

### 2.3 RiseScore (best route without cash: PCC plus top 3)

**30-second explanation**
> "In a nursing home, a resident's legs often weaken for days before she falls, but nobody times how she gets out of a chair. Homes already put pressure pads on chairs; they just beep. RiseScore makes that pad measure: how long each stand takes, whether she pushes off with her arms, whether she rocks and fails first. It compares all of that with her own last week. If she's slipping, a flag pops up on the chair and the nurse's screen says why in one line: 'arms on 4 of 5 stands, 3.6 s vs her usual 2.0, third slow day: physio screen today.' No camera, nothing to wear, no diagnosis."

**Why it can win PCC, Healthcare and the overall top 3**
- The PCC prompt names falls and "a bit less mobility" word for word (checked at https://hack-the-valley-11.devpost.com/).
- A reason on every flag earns PCC's bonus for explaining why something is flagged.
- Healthcare fit is 8.
- The judge's own body sets off the hardware.
- The case against plain alarms: a large trial found that more bed-alarm use did not reduce falls (Shorr et al. 2012, https://pubmed.ncbi.nlm.nih.gov/23165660/). So the pitch is "measure, don't beep".
- **The cost:** the PCC prize is an air fryer, and you give up KAZE's cash.

**4-minute demo**

| Time | What happens |
|---|---|
| 0:00–0:25 | Hook, then a 10-second contrast: chair alarms beep, a physio tests her twice a year, fall detectors call after the fall. RiseScore measures every stand. |
| **0:25–1:30** | **Judge takes part.** The judge sits and stands normally: a green dot, and the display reads "1.8S". Then the judge stands "like Mrs. Rossi", pushing on the armrest and rocking twice. The servo flag goes up, the LED turns yellow, and the reason card appears. A teammate stands beside the chair as a spotter. |
| 1:30–2:20 | Shift view: Mrs. Rossi's 30 days of stands, one dot each (labelled SYNTHETIC), her slow drift, the reason codes, and suggested next steps by role (physio screen, falls huddle, nurse checks for a new sedative or an infection). |
| 2:20–3:00 | Accuracy: rise time from the sensors vs. webcam frames, as average error over about 30 team stands. Alert budget: false flags per resident per week on the synthetic group. |
| 3:00–4:00 | Demonstrated / Simulated / Assumed. "It feeds the chart PCC already has." A Gemini handoff note where every number is checked against the computed values. Close: "The chair alarm you already own, measuring instead of beeping." |

**Hardware**
- One ESP32 with FSR #1 under the seat cushion (on a rigid card with a coin glued over the sensor, GPIO34) and FSR #2 under a rigid armrest plate with a hand outline drawn on it (GPIO35).
- The servo flag, quad display and green/yellow LEDs run on the same ESP32. Drop the Uno to remove a failure point. Connect to the laptop over USB serial.
- Overhead ultrasonic: use it only if it can be aimed at the standing spot from at least 2.2 m. Otherwise detect stands from the seat sensor alone. The Grove sensor's beam is only 15° wide and needs a smooth target of at least 0.5 m² (https://wiki.seeedstudio.com/Grove-Ultrasonic_Ranger/), and a head seen from above is neither.
- **Borrow:** a sturdy armchair from the venue, a tripod or mic stand, Grove-to-jumper cables.

**AI and data**
- **"The camera teaches the chair."** During development, MediaPipe on the webcam labels the moments of leaving the seat, standing upright and putting a hand on the armrest. Those labels train a small model on the pressure-sensor signals that finds each stand and classifies arm use and failed attempts. Report its accuracy. The finished product has no camera.
- Each resident gets a personal baseline, and a drift detector produces the reason codes.
- A synthetic group of 20 residents × 30 days, labelled SYNTHETIC, shows the alert budget. It is not evidence that the method works clinically.

**The killer question, and the answer**
> **Q:** "The CDC test you cite uses a chair without armrests, arms crossed, and 30 seconds of repeated stands (https://www.cdc.gov/steadi/media/pdfs/STEADI-Assessment-30Sec-508.pdf). This chair isn't running it, and you injected your own 'early' declines. Why should a nurse act on this?"
>
> **A:** "You're right: we don't run the CDC test, and we don't claim to predict falls. The CDC treats needing your arms to stand as a warning sign. We track changes in her arm use and rise time against her own last week, and the flag only asks for a physio screen. The synthetic data shows the pipeline and the alert budget, not clinical results. Validating it would take a pilot."
>
> **Do not cite** Pozaic 2016 for "more arm push means more risk". In that study, fallers actually used *less* arm support (https://pmc.ncbi.nlm.nih.gov/articles/PMC5396921/).

**Build plan**
- **PICkachu:** wiring, seat and armrest plates, servo flag, display, a calibration stand at startup, the webcam check over about 30 stands.
- **Cami (with PICkachu for 3–4 hours):** stand detection and the arm-use classifier from webcam labels; personal baselines and drift detection; the synthetic group and alert budget.
- **Marco:** FastAPI data intake, the Next.js shift view (a dot per stand, reason chips), a push alert to the nurse, the Gemini note with number checks.
- **Galit:** claim wording; a prior-art contrast slide covering NoFall (https://devpost.com/software/nofall), chair alarms and VirtuSense; Demonstrated / Simulated / Assumed; PCC's "change in condition" wording; the Devpost entry.
- **Cut first:** the steadiness-after-standing measure, then the ultrasonic, then Gemini.

---

## 3. Ten research takeaways

1. **Hardware that works live wins at Hack the Valley far more often than its share of entries.** Hardware projects are about 3–5% of entries but took 5 of the 15 top-3 places at HTV V through HTV X. That includes 1st at HTV 9 (Impactify: ESP32, pressure sensors, Twilio alerts) and 1st at HTV X (Plant Hopper). HTV 7's three hardware entries won nothing, so it has to work on the day. https://hack-the-valley-9.devpost.com/project-gallery · https://hack-the-valley-x.devpost.com/project-gallery
2. **Projects built around Gemini don't place.** 78 of 124 HTV X projects (63%) used Gemini, and none of the top 3 was built around it. Gemini health web apps such as Guess The Disease, SelfPharma and ELDA won nothing. Give Gemini one visible job. https://hack-the-valley-x.devpost.com/project-gallery?page=2
3. **Health and safety wins at HTV come from a sensor in a live loop that alerts someone, not from dashboards.** Every health or safety top-3 project since 2022 used a sensor or camera. The last clinician dashboard to reach the top 3 was in 2021. SleepStop (HTV V, 3rd place and Best Hardware) noticed no response and contacted help automatically, which is the closest precedent to KAZE. https://hack-the-valley-v.devpost.com/project-gallery
4. **The judges are bankers and big-tech engineers, with no clinicians, and several have judged here before.** Swapna Samuel (BMO) and Daniela Aldave-Garza (RBC) judged HTV X, and Conrad Mo sponsored HTV X's Best UI prize. Talk about reliability, cost and what happens when things fail, and make the UI look finished. https://hack-the-valley-x.devpost.com/ · https://hack-the-valley-11.devpost.com/
5. **The KAZE judge thinks in value and cost.** Swapna Samuel is a FinOps Foundation Ambassador. Her talk says, "No one migrates to cloud 'just because'. There has to be value." Show parts cost per band, cents per escalation, and a precise Demonstrated / Simulated / Assumed slide. https://www.finops.org/assets/measuring-business-value-of-all-things-cloud-including-ai/
6. **The rules that can hurt you:** one project per team; all code written after Friday 8 PM ET; projects without a GitHub link are not judged; "Authenticity" (originality) is a scored criterion. https://hack-the-valley-11.devpost.com/rules
7. **You can't see competitors yet, but you can predict them.** The project gallery isn't published. Devpost shows 181 participants today, which suggests roughly 45–60 projects based on past years. Expect phone check-in timers and drink-spiking lids and coasters in KAZE. The spiking gadgets are a crowded category, and KAZE forbids claiming to detect spiking. https://hack-the-valley-11.devpost.com/project-gallery · https://devpost.com/software/search?query=drink%20spiking
8. **SqueezeBack's server side already exists and has won.** Batman (HackNC 2026, 1st) shipped "check in if silent", a duress code word, and "the LLM never decides to escalate". FieldLine shipped a call-by-call escalation ladder. Credit both and claim only the band loop. https://devpost.com/software/batman-cjer91 · https://devpost.com/software/fieldline
9. **Users are tired of detection gadgets; the gap is getting someone to come.** The top reply on a 2.7k-upvote r/TwoXChromosomes post: "We've seen SO MANY of these things… i'm so sick of it." Google's own Safety Check says, "If your phone is locked, you may need to unlock it." Use the line: "We don't detect anything. We make sure someone comes." https://www.reddit.com/r/TwoXChromosomes/comments/1r5rw9c/ · https://support.google.com/pixelphone/answer/7055029?hl=en
10. **Judges decide in the first 30–90 seconds, and honesty raises scores.** Show it working within about 90 seconds and rehearse the first 30 seconds on their own. Naming a weakness raised scores, while claiming nothing breaks cost credibility. Keep a 60-second backup recording, because Wi-Fi fails at most events. https://blog.jetbrains.com/ai/2026/06/how-to-win-a-hackathon-notes-from-the-judging-table/ · https://dev.to/kurbaitaev/what-judges-actually-score-notes-from-a-year-of-hackathon-judging-3p4l

---

## 4. Final pick and the biggest risk

**Final pick: SqueezeBack, targeting Project KAZE and the overall top 3.**
- It is the only idea that fits the KAZE brief word for word ("cannot … consciously activate an SOS") and also pays cash.
- It has the strongest one-line explanation for this panel: "a liveness probe for a person."
- It gives the judge a full loop in under 90 seconds: squeeze, go silent, your own phone rings, press 1, the flag drops, the wrist blinks blue. Then it breaks itself on purpose by pulling the power, and still escalates.
- It uses parts already on the table and plays to each person's strength: PICkachu builds the band, Marco builds the durable escalation backend, Cami runs the measured study, and Galit builds the safety and privacy case.
- It was already the team's top idea (7.7).

**The single biggest risk: the call doesn't ring on stage.** The wow moment, the "demonstrated" claim and the 25% safety score all depend on it.

| Way it fails | Fix |
|---|---|
| Twilio trial accounts can only call verified numbers (https://www.twilio.com/docs/usage/tutorials/how-to-use-your-free-trial-account) | **Upgrade the account and buy a 416/647 number before 9 PM tonight.** |
| iPhone "Screen/Silence Unknown Callers" sends unsaved numbers to voicemail or screens them (https://support.apple.com/en-us/111106) | Make "save SqueezeBack as a contact" part of the QR consent step. Keep a pre-verified team phone as a spare contact B, and offer it to any judge who'd rather not share a number. |
| Voicemail picks up and looks like "answered" | Count only a pressed 1 as acknowledgement. Set the ring timeout to 12–15 s (https://www.twilio.com/docs/voice/api/call-resource). |
| Venue Wi-Fi drops | Put the ESP32 and the laptop server on a phone hotspot. |
| The power bank switches itself off, causing a false "heartbeat lost" mid-pitch | Test it for 30+ minutes on the real band, or power the band from laptop USB. |
| Everything fails at once | Send a parallel Pushover emergency-priority alert, which repeats until acknowledged (https://pushover.net/api), and keep the backup video recorded at 2–3 AM. |

**Owner:** Marco. **Proof it's handled:** by 6 AM, run the full sequence (missed check-in → ring → press 1, plus the power pull) 5 times on the hotspot with a stopwatch. After each demo, delete the judge's number on screen.