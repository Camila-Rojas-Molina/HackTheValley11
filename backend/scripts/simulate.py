"""Run the seven KAZE scenarios over HTTP against a running StillHere server. No hardware.

  1 safe            routine check-in answered with a squeeze; a counted challenge (wrong count, one
                    retry with a new count, right count); a replayed event changes nothing
  2 no response     prompt -> reminder -> L1 on the server's own timers; a late squeeze changes nothing;
                    "I'm on it" alone does not close the alert, "she's safe" does
  3 duress          duress squeeze and duress PIN answer byte-identically to OK / real PIN and show ARMED;
                    contacts get a silent help request; the real PIN does not close it
  4 lost contact    heartbeats stop: LOST_CONTACT with its own wording; the check-in deadline still
                    escalates; a buffered squeeze arrives late and changes nothing; contact restored
  5 unacknowledged  L1 ignored -> next contact (L2) -> FINAL, "Emergency services: SIMULATED", no call
  6 false alarm     simulated heart rate only brings a check-in forward; escalated, then the real PIN
                    closes it as a false alarm; the location link answers 410
  7 privacy         contacts see nothing before an alert; an unapproved contact gets nothing; invites are
                    single use; "pass it on"; the location link answers 410 once it expires
  pcc               PointClickCare senior mode: seeds the synthetic cohort (POST /v1/demo/care-seed) and
                    checks GET /v1/care/alerts lists every scripted change and no no-change script

Fake device: the demo device (DEVICE_KEY / DEMO_DEVICE_ID), heartbeat every 2 s from a thread.
Fake contacts: web contacts "SIM-A" and "SIM-B" (approved, in that order) and "SIM-P" (joined, never
approved), created through the wearer API and removed at the end.

Env (the server's values): STILLHERE_URL (default http://localhost:8000), ADMIN_KEY, DEVICE_KEY,
DEMO_DEVICE_ID, WEARER_KEY, DEMO_PIN, DEMO_DURESS_PIN. backend/.env is read if present.

  ../.venv/bin/python scripts/simulate.py          # all seven plus the PCC seed (about 4 minutes in demo timing)
  ../.venv/bin/python scripts/simulate.py 2 5 pcc  # a subset

Prints PASS/FAIL per step and exits 1 if any failed. It changes the demo wearer's session, heart-rate
baseline and contacts, so it refuses to run while the demo wearer has approved contacts it did not create
(they would get real alerts): run it against its own database, e.g. a second server on another port.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from app.config import PROFILES  # noqa: E402  (timing table only; no database)

T = PROFILES["demo"]
FINAL_BANNER = "Emergency services: SIMULATED – would contact 911 / campus safety with last known location"
OK_CMD = {"led": "off", "blink_n": 0, "challenge_id": None}


class Fail(AssertionError):
    pass


def check(cond, msg: str) -> None:
    if not cond:
        raise Fail(msg)


def note(msg: str) -> None:
    print(f"      - {msg}", flush=True)


def wait_for(fn: Callable, timeout: float, what: str, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise Fail(f"timed out after {timeout:.0f} s waiting for {what} (last: {last!r})")


# --- HTTP helpers ------------------------------------------------------------------
class Server:
    def __init__(self, url: str, admin_key: str, wearer_key: str):
        self.url = url.rstrip("/")
        self.c = httpx.Client(base_url=self.url, timeout=10.0)
        self.admin_h = {"X-Admin-Key": admin_key}
        self.wearer_h = {"Authorization": f"Bearer {wearer_key}"}

    def admin(self, method: str, path: str, **kw) -> httpx.Response:
        return self.c.request(method, path, headers=self.admin_h, **kw)

    def wearer(self, method: str, path: str, **kw) -> httpx.Response:
        return self.c.request(method, path, headers=self.wearer_h, **kw)

    def ok(self, r: httpx.Response, what: str) -> dict:
        check(r.status_code == 200, f"{what}: HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    def overview(self) -> dict:
        return self.ok(self.admin("GET", "/v1/admin/overview"), "overview")

    def session(self, sid: str) -> Optional[dict]:
        return next((s for s in self.overview()["sessions"] if s["id"] == sid), None)

    def state(self, sid: str) -> Optional[str]:
        s = self.session(sid)
        return s["state"] if s else None

    def alert(self, sid: str) -> Optional[dict]:
        return next((a for a in self.overview()["alerts"] if a["session_id"] == sid), None)

    def wait_state(self, sid: str, target: str, timeout: float) -> float:
        t0 = time.monotonic()
        wait_for(lambda: self.state(sid) == target, timeout, f"state {target}")
        return time.monotonic() - t0

    def outbox(self, **params) -> list:
        return self.ok(self.admin("GET", "/v1/admin/outbox", params={**params, "limit": 1000}), "outbox")["outbox"]

    def reset(self) -> str:
        j = self.ok(self.admin("POST", "/v1/demo/reset", json={"mode": "hair", "hr_source": "live", "arm": True}),
                    "demo reset")
        return j["armed"]["session_id"]

    def checkin_now(self, sid: str) -> None:
        j = self.ok(self.admin("POST", "/v1/demo/checkin-now"), "check-in now")
        check(j["state"] == "PROMPTED", f"check-in now gave {j['state']}")

    def fast_forward(self, expect: str) -> None:
        j = self.ok(self.admin("POST", "/v1/demo/fast-forward"), "fast-forward")
        check(j.get("fired_next") == expect, f"fast-forward fired {j.get('fired_next')}, wanted {expect}")

    def to_l1(self, sid: str) -> None:
        """Operator shortcut (labelled demo): check-in now, then fast-forward both prompt deadlines."""
        self.checkin_now(sid)
        self.fast_forward("PROMPT_TIMEOUT")
        self.wait_state(sid, "REMINDED", 5)
        self.fast_forward("REMINDER_TIMEOUT")
        self.wait_state(sid, "ESCALATED_L1", 5)

    def cstatus(self, token: str) -> tuple:
        r = self.c.get(f"/v1/contact/{token}/status")
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else None)

    def calert(self, token: str) -> Optional[dict]:
        code, body = self.cstatus(token)
        check(code == 200, f"contact status HTTP {code}")
        return body["alert"]

    def cact(self, token: str, action: str) -> httpx.Response:
        if action == "ack":
            return self.c.post(f"/v1/contact/{token}/ack", json={})
        return self.c.post(f"/v1/contact/{token}/followup", json={"action": action})

    def get_path(self, link: str) -> httpx.Response:
        return self.c.get(urlsplit(link).path)  # PUBLIC_BASE_URL may differ from STILLHERE_URL


class FakeDevice:
    """The scrunchie over HTTP: 2 s heartbeats from a thread, events on demand."""

    def __init__(self, url: str, device_id: str, key: str):
        self.c = httpx.Client(base_url=url.rstrip("/"), timeout=5.0)
        self.device_id, self.key = device_id, key
        self.boot = "sim" + secrets.token_hex(3)
        self.seq = 0
        self.enmo60_mg = 10.0  # still
        self.lock = threading.Lock()
        self.unplugged = threading.Event()
        self.stop = threading.Event()
        self.last_ok: Optional[float] = None
        self.thread = threading.Thread(target=self._loop, name="fake-device", daemon=True)

    def _headers(self) -> tuple:
        with self.lock:
            self.seq += 1
            seq = self.seq
        return seq, {"Authorization": f"Bearer {self.key}", "X-Device-Id": self.device_id,
                     "X-Boot-Id": self.boot, "X-Seq": str(seq)}

    def heartbeat(self) -> httpx.Response:
        _, h = self._headers()
        r = self.c.post("/v1/device/heartbeat", headers=h,
                        json={"wear_mode": "hair", "enmo60_mg": self.enmo60_mg, "offbody": False})
        if r.status_code == 200:
            self.last_ok = time.monotonic()
        return r

    def _loop(self) -> None:
        while not self.stop.is_set():
            if not self.unplugged.is_set():
                try:
                    self.heartbeat()
                except httpx.HTTPError:
                    pass
            self.stop.wait(T.heartbeat_s)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=5)

    def new_event(self, etype: str, payload: Optional[dict] = None) -> dict:
        seq, _ = self._headers()
        return {"event_id": f"{self.boot}:{seq}", "type": etype, "payload": payload or {}, "seq": seq}

    def send(self, ev: dict) -> httpx.Response:
        h = {"Authorization": f"Bearer {self.key}", "X-Device-Id": self.device_id, "X-Boot-Id": self.boot,
             "X-Seq": str(ev["seq"])}
        body = {k: ev[k] for k in ("event_id", "type", "payload")}
        return self.c.post("/v1/device/events", headers=h, json=body)

    def event(self, etype: str, payload: Optional[dict] = None) -> httpx.Response:
        return self.send(self.new_event(etype, payload))

    def squeeze(self, k: Optional[int], pattern: str = "plain") -> httpx.Response:
        return self.event("squeeze", {"pattern": pattern, "k": k})


# --- the run ---------------------------------------------------------------------------
class Run:
    def __init__(self, srv: Server, dev: FakeDevice, pin: str, duress_pin: str, wearer_name: str):
        self.srv, self.dev = srv, dev
        self.pin, self.duress_pin = pin, duress_pin
        self.name = wearer_name
        self.contacts: dict = {}
        self._ok_squeeze: Optional[bytes] = None
        self._real_pin: Optional[bytes] = None

    # contacts ------------------------------------------------------------------------
    def setup_contacts(self) -> None:
        listed = self.srv.ok(self.srv.wearer("GET", "/v1/contacts"), "list contacts")["contacts"]
        for c in listed:
            if c["name"].startswith("SIM-"):
                self.srv.wearer("DELETE", f"/v1/contacts/{c['contact_id']}")
        listed = self.srv.ok(self.srv.wearer("GET", "/v1/contacts"), "list contacts")["contacts"]
        real = [c for c in listed if c["status"] == "approved"]
        if real:
            raise SystemExit(
                f"refusing to run: the demo wearer has {len(real)} approved contact(s) that would receive real "
                "alerts. Run simulate.py against its own database (a second server on another port).")
        for name, approve in (("SIM-A", True), ("SIM-B", True), ("SIM-P", False)):
            inv = self.srv.ok(self.srv.wearer("POST", "/v1/contacts", json={"name": name}), f"invite {name}")
            invite_token = inv["web_link"].rsplit("/", 1)[1]
            info = self.srv.ok(self.srv.c.get(f"/v1/contact/{invite_token}/invite"), "invite info")
            check(info["valid"] and info["wearer"], "invite info")
            j = self.srv.ok(self.srv.c.post(f"/v1/contact/{invite_token}/join", json={"name": name}), f"join {name}")
            if approve:
                self.srv.ok(self.srv.wearer("POST", f"/v1/contacts/{inv['contact_id']}/approve"), f"approve {name}")
            self.contacts[name[-1]] = {"id": inv["contact_id"], "token": j["contact_token"], "invite": invite_token,
                                       "name": name}

    def cleanup(self) -> None:
        for c in self.contacts.values():
            try:
                self.srv.wearer("DELETE", f"/v1/contacts/{c['id']}")
            except httpx.HTTPError:
                pass
        try:
            self.srv.admin("POST", "/v1/demo/reset", json={"arm": False})
        except httpx.HTTPError:
            pass

    @property
    def A(self) -> dict:
        return self.contacts["A"]

    @property
    def B(self) -> dict:
        return self.contacts["B"]

    @property
    def P(self) -> dict:
        return self.contacts["P"]

    # helpers ---------------------------------------------------------------------------
    def reset(self) -> str:
        sid = self.srv.reset()
        self.dev.heartbeat()
        return sid

    def wearer_texts(self, sid: str) -> list:
        return [o["text"] for o in self.srv.outbox(session_id=sid, recipient="wearer")]

    def contact_rows(self, sid: str, contact: dict, template: Optional[str] = None) -> list:
        rows = self.srv.outbox(session_id=sid, contact_id=contact["id"])
        return [o for o in rows if template is None or o["template"] == template]

    def wait_contact_msg(self, sid: str, contact: dict, template: str, timeout: float = 5) -> dict:
        return wait_for(lambda: (self.contact_rows(sid, contact, template) or [None])[-1], timeout,
                        f"{template} to {contact['name']}")

    def ok_squeeze_reference(self) -> bytes:
        if self._ok_squeeze is None:
            sid = self.reset()
            self.srv.checkin_now(sid)
            r = self.dev.squeeze(2)
            check(r.status_code == 200, "reference squeeze")
            self._ok_squeeze = r.content
        return self._ok_squeeze

    def cancel(self, sid: str, pin: str) -> httpx.Response:
        return self.srv.wearer("POST", f"/v1/sessions/{sid}/cancel", json={"pin": pin})

    def real_pin_reference(self) -> bytes:
        if self._real_pin is None:
            sid = self.reset()
            r = self.cancel(sid, self.pin)
            check(r.status_code == 200, f"real PIN: HTTP {r.status_code}")
            check(self.srv.alert(sid) is None, "real PIN in ARMED opened an alert")
            self._real_pin = r.content
        return self._real_pin

    def resolve(self, sid: str, contact: dict) -> None:
        r = self.srv.cact(contact["token"], "ack")
        check(r.status_code == 200 and r.json()["result"] == "acked", f"ack: {r.status_code} {r.text[:120]}")
        check(self.srv.state(sid) == "ACKED", "ack did not move to ACKED")
        check(self.srv.alert(sid) is not None, "an acknowledgement alone closed the alert")
        r = self.srv.cact(contact["token"], "safe")
        check(r.status_code == 200 and r.json()["result"] == "resolved", f"safe: {r.status_code} {r.text[:120]}")
        check(self.srv.state(sid) == "ARMED", "she's safe did not return to ARMED")
        check(self.srv.alert(sid) is None, "alert still open after she's safe")

    # scenarios -------------------------------------------------------------------------
    def s1_safe(self) -> None:
        sid = self.reset()
        self.srv.checkin_now(sid)
        hb = self.dev.heartbeat().json()
        check(hb["state_public"] == "PROMPTED" and hb["cmd"]["led"] == "amber" and hb["cmd"]["blink_n"] == 0,
              f"device did not show a routine prompt: {hb}")
        wait_for(lambda: "Reminder" in self.wearer_texts(sid), 5, "the Reminder nudge")
        note('routine check-in: wearer nudged "Reminder" (the watch tap); device shows amber')
        r = self.dev.squeeze(2)
        j = r.json()
        check(j["result"] == "noted" and j["state_public"] == "ARMED" and j["cmd"] == OK_CMD, f"squeeze: {j}")
        self._ok_squeeze = r.content
        check(self.srv.state(sid) == "ARMED", "not ARMED after the squeeze")
        wait_for(lambda: self.wearer_texts(sid)[-1] == "Noted ✓", 5, "Noted ✓")
        note('squeeze k=2 -> ARMED; wearer gets "Noted ✓"')

        r = self.dev.event("head_position_change", {"held_s": 5})
        j = r.json()
        n = j["cmd"]["blink_n"]
        check(j["state_public"] == "PROMPTED" and n in (1, 3, 4), f"head position change: {j}")
        check(self.srv.alert(sid) is None, "a sensor event opened an alert")
        wrong = next(k for k in (1, 3, 4) if k != n)
        j = self.dev.squeeze(wrong).json()
        n2 = j["cmd"]["blink_n"]
        check(j["result"] == "retry" and j["state_public"] == "PROMPTED" and n2 in (1, 3, 4) and n2 != n,
              f"wrong count: {j}")
        note(f"head position change -> counted challenge N={n}; {wrong} squeeze(s) -> one retry, new N={n2}")
        ev = self.dev.new_event("squeeze", {"pattern": "plain", "k": n2})
        first = self.dev.send(ev)
        check(first.json()["result"] == "noted" and self.srv.state(sid) == "ARMED", f"right count: {first.text}")
        version = self.srv.session(sid)["version"]
        for _ in range(4):
            again = self.dev.send(ev)
            check(again.content == first.content, "a replayed event_id did not return the cached bytes")
        check(self.srv.session(sid)["version"] == version, "a replayed event changed the session")
        check(self.contact_rows(sid, self.A) == [] and self.contact_rows(sid, self.B) == [], "contacts were told")
        note(f"{n2} squeezes -> ARMED; same event_id x5 -> one transition, identical cached reply; contacts told nothing")

    def s2_no_response(self) -> None:
        sid = self.reset()
        self.srv.checkin_now(sid)
        dt = self.srv.wait_state(sid, "REMINDED", T.prompt_window_s + 8)
        note(f"no answer: PROMPTED -> REMINDED after {dt:.1f} s (window {T.prompt_window_s:.0f} s from delivery)")
        check(self.srv.calert(self.A["token"]) is None, "contact saw something before the alert")
        dt = self.srv.wait_state(sid, "ESCALATED_L1", T.prompt_window_s + 8)
        timers = self.srv.ok(self.srv.admin("GET", "/v1/admin/timers", params={"recent": 50}), "timers")
        fired = [t for t in timers["recent_fired"] if t["session_id"] == sid and t["kind"] == "REMINDER_TIMEOUT"]
        check(fired and fired[0]["lag_s"] <= 2.0, f"L1 deadline lag {fired[0]['lag_s'] if fired else None}")
        note(f"REMINDED -> ESCALATED_L1 after {dt:.1f} s; timer fired {fired[0]['lag_s']:.2f} s after its deadline")
        a = self.srv.calert(self.A["token"])
        check(a and a["kind"] == "NO_RESPONSE" and a["actions"] == ["ack", "pass"], f"contact A: {a}")
        check("hasn't responded to 2 check-ins" in a["title"] and "not a confirmed emergency" in a["title"], a["title"])
        check(a["location"] and a["location"]["link"], "no last-known location released")
        check(self.srv.calert(self.B["token"]) is None, "contact B was alerted at L1")
        note("contact A alerted (I'm on it / pass it on) with last-known location; contact B sees nothing")

        j = self.dev.squeeze(2).json()
        check(j["result"] == "noted" and self.srv.state(sid) == "ESCALATED_L1", f"late squeeze: {j}")
        late = self.wait_contact_msg(sid, self.A, "late_device")
        check("please still check on her" in late["text"], late["text"])
        note(f'late squeeze: state unchanged; A told "{late["text"][len("StillHere: "):]}"')
        self.resolve(sid, self.A)
        check(self.srv.calert(self.A["token"]) is None, "contact page still shows the alert")
        note("A: I'm on it -> ACKED (still open); I'm with her - she's safe -> RESOLVED -> ARMED")

    def s3_duress(self) -> None:
        ok_bytes = self.ok_squeeze_reference()
        real_pin = self.real_pin_reference()
        sid = self.reset()
        self.srv.checkin_now(sid)
        r = self.dev.squeeze(3, pattern="duress")
        check(r.content == ok_bytes, f"duress reply differs from OK:\n {r.content!r}\n {ok_bytes!r}")
        note(f"duress squeeze reply == OK squeeze reply, byte for byte ({len(ok_bytes)} bytes)")
        pub = self.srv.ok(self.srv.wearer("GET", f"/v1/sessions/{sid}"), "session status")
        hb = self.dev.heartbeat().json()
        check(pub["state_public"] == "ARMED" and hb["state_public"] == "ARMED" and hb["cmd"] == OK_CMD,
              f"duress not masked: {pub['state_public']} {hb}")
        a = self.srv.alert(sid)
        check(self.srv.state(sid) == "ESCALATED_L1" and a and a["kind"] == "DURESS" and a["masked"],
              f"operator view: {self.srv.state(sid)} {a}")
        wait_for(lambda: self.wearer_texts(sid)[-1] == "Noted ✓", 5, "Noted ✓ to the wearer")
        tl = self.srv.ok(self.srv.wearer("GET", f"/v1/sessions/{sid}/timeline"), "timeline")["entries"]
        check(not any(e["kind"] == "duress" for e in tl), "duress visible on the wearer's timeline")
        note('wearer and device see ARMED / "Noted ✓"; timeline hides it; true state ESCALATED_L1 (DURESS, masked)')
        ca = self.srv.calert(self.A["token"])
        check(ca and ca["kind"] == "DURESS" and ca["title"].startswith("SILENT HELP REQUEST"), f"contact A: {ca}")
        msg = self.wait_contact_msg(sid, self.A, "alert_duress")
        check("Don't call her phone" in msg["text"], msg["text"])
        note(f'contact A: "{ca["title"]}"')

        r = self.cancel(sid, self.pin)
        check(r.content == real_pin, f"real PIN during duress answered differently: {r.content!r}")
        a = self.srv.alert(sid)
        check(a and a["kind"] == "DURESS", "the real PIN closed a duress alert")
        note_msg = self.wait_contact_msg(sid, self.A, "duress_pin_note")
        note(f'real PIN: same "Cancelled ✓" bytes, alert stays open; A told "{note_msg["text"][len("StillHere: "):]}"')
        self.resolve(sid, self.A)
        note("only A's I'm with her - she's safe closed it")

        sid = self.reset()
        r = self.cancel(sid, self.duress_pin)
        check(r.status_code == 200 and r.content == real_pin, f"duress PIN reply differs: {r.content!r} vs {real_pin!r}")
        a = self.srv.alert(sid)
        check(a and a["kind"] == "DURESS" and a["masked"], f"duress PIN opened no masked alert: {a}")
        pub = self.srv.ok(self.srv.wearer("GET", f"/v1/sessions/{sid}"), "session status")
        check(pub["state_public"] == "ARMED", "duress PIN not masked")
        note(f"duress PIN reply == real PIN reply, byte for byte: {real_pin.decode()}")
        self.resolve(sid, self.A)

    def s4_lost_contact(self) -> None:
        sid = self.reset()
        self.dev.unplugged.set()
        try:
            time.sleep(0.5)  # let an in-flight heartbeat finish before reading its time
            t_last = self.dev.last_ok
            self.srv.wait_state(sid, "ARMED", 1)
            wait_for(lambda: (self.srv.session(sid) or {}).get("link_state") == "LOST_CONTACT", T.lease_s + 5,
                     "LOST_CONTACT")
            lost_after = time.monotonic() - t_last
            check(lost_after <= T.lease_s + 2.5, f"LOST_CONTACT after {lost_after:.1f} s")
            wait_for(lambda: any("lost contact with your device" in t for t in self.wearer_texts(sid)), 5,
                     "wearer lost-contact note")
            note(f"unplugged: LOST_CONTACT {lost_after:.1f} s after the last heartbeat (lease {T.lease_s:.0f} s)")
            ca = wait_for(lambda: self.srv.calert(self.A["token"]), T.lost_contact_grace_s + 5, "contact notice")
            check(ca["kind"] == "LOST_CONTACT" and ca["actions"] == [], f"contact A: {ca}")
            check(ca["title"] == f"We lost contact with {self.name}'s device. Cause unknown. "
                                 "Her check-in schedule is still running.", ca["title"])
            msg = self.wait_contact_msg(sid, self.A, "lost_contact")
            check("Cause unknown. Her check-in schedule is still running." in msg["text"], msg["text"])
            check(self.srv.calert(self.B["token"]) is None, "contact B told about lost contact")
            note(f'after the {T.lost_contact_grace_s:.0f} s grace, A (only): "{msg["text"][len("StillHere: "):]}"')

            self.srv.checkin_now(sid)
            buffered = self.dev.new_event("squeeze", {"pattern": "plain", "k": 2})  # held on the device
            self.srv.wait_state(sid, "REMINDED", T.prompt_window_s + 8)
            self.srv.wait_state(sid, "ESCALATED_L1", T.prompt_window_s + 8)
            a = self.srv.alert(sid)
            check(a and a["kind"] == "NO_RESPONSE", f"deadline during the outage: {a}")
            ca = self.srv.calert(self.A["token"])
            check(ca["kind"] == "NO_RESPONSE" and ca["actions"] == ["ack", "pass"], f"contact A: {ca}")
            note("check-in deadline kept running while unplugged: REMINDED -> ESCALATED_L1 (NO_RESPONSE)")
        finally:
            self.dev.unplugged.clear()
        hb = self.dev.heartbeat()
        check(hb.status_code == 200, "heartbeat after replug")
        check(self.srv.session(sid)["link_state"] == "OK", "link not restored")
        restored = self.wait_contact_msg(sid, self.A, "restored")
        buffered["payload"]["buffered"] = True
        j = self.dev.send(buffered).json()
        check(j["result"] == "noted" and self.srv.state(sid) == "ESCALATED_L1", f"buffered squeeze: {j}")
        late = self.wait_contact_msg(sid, self.A, "late_device")
        check("(delivered late)" in late["text"] and "please still check on her" in late["text"], late["text"])
        check(self.srv.alert(sid) is not None, "the alert closed on reconnect")
        note(f'replug: "{restored["text"][len("StillHere: "):]}"; buffered squeeze: "{late["text"][len("StillHere: "):]}", '
             "state unchanged")
        self.resolve(sid, self.A)

    def s5_unacknowledged(self) -> None:
        sid = self.reset()
        self.srv.to_l1(sid)
        note("check-in missed (prompt deadlines fast-forwarded, labelled demo) -> ESCALATED_L1")
        check(self.srv.calert(self.B["token"]) is None, "B alerted at L1")
        dt = self.srv.wait_state(sid, "ESCALATED_L2", T.ack_s + 5)
        b = self.srv.calert(self.B["token"])
        a = self.srv.calert(self.A["token"])
        check(b and b["actions"] == ["ack", "pass"], f"contact B at L2: {b}")
        check(a and a["actions"] == ["ack"], f"contact A at L2 (re-pinged, cannot pass): {a}")
        check(len(self.contact_rows(sid, self.A, "alert_no_response")) >= 2, "A was not re-pinged")
        note(f"A ignored it: L2 after {dt:.1f} s (ack window {T.ack_s:.0f} s); B alerted, A re-pinged")
        dt = self.srv.wait_state(sid, "ESCALATED_FINAL", T.ack_s + 5)
        for c in (self.A, self.B):
            s = self.srv.calert(c["token"])
            check(s and s["final"] and s["banner"] == FINAL_BANNER and s["actions"] == ["ack"], f"{c['name']}: {s}")
            msg = self.wait_contact_msg(sid, c, "alert_final")
            check("Emergency services: SIMULATED" in msg["text"] and "No call is placed." in msg["text"], msg["text"])
        health = self.srv.ok(self.srv.c.get("/v1/health"), "health")
        check(health["emergency_services"] == "SIMULATED (never placed)", health)
        note(f'B ignored it: FINAL after {dt:.1f} s; both pages show "{FINAL_BANNER}"; no call placed')
        self.resolve(sid, self.B)
        note("B: I'm on it, then I'm with her - she's safe -> RESOLVED -> ARMED")

    def s6_false_alarm(self) -> None:
        real_pin = self.real_pin_reference()
        sid = self.reset()
        self.dev.enmo60_mg = 10.0
        self.dev.heartbeat()
        hr = lambda bpm, n: self.srv.wearer(  # noqa: E731
            "POST", "/v1/hr", json={"source": "simulate.py", "simulated": True, "samples": [{"bpm": bpm}] * n})
        self.srv.ok(hr(72, 10), "HR post")
        self.srv.ok(self.srv.wearer("POST", "/v1/hr/calibrate", json={"window_s": 10, "allow_simulated": True}),
                    "calibrate")
        self.srv.ok(hr(118, 20), "HR post")
        t0 = time.monotonic()

        def prompted():
            self.srv.ok(hr(118, 1), "HR post")
            return self.srv.state(sid) == "PROMPTED"
        wait_for(prompted, T.hr_sustain_s + 25, "the heart-rate rule to bring the check-in forward", interval=1.0)
        s = self.srv.session(sid)
        check(s["prompt_cause"] == "challenge" and s["challenge_n"] in (1, 3, 4), f"session: {s}")
        check(self.srv.alert(sid) is None, "heart rate opened an alert")
        self.srv.ok(hr(72, 80), "HR post")  # back near the baseline
        note(f"SIMULATED heart rate 118 vs baseline 72 while still -> PROMPTED (challenge N={s['challenge_n']}) "
             f"after {time.monotonic() - t0:.1f} s; no alert")

        self.srv.fast_forward("PROMPT_TIMEOUT")
        self.srv.wait_state(sid, "REMINDED", 5)
        self.srv.fast_forward("REMINDER_TIMEOUT")
        self.srv.wait_state(sid, "ESCALATED_L1", 5)
        j = self.dev.event("head_position_change", {"held_s": 5}).json()
        check(self.srv.state(sid) == "ESCALATED_L1", f"a sensor event changed an escalation: {j}")
        a = self.srv.calert(self.A["token"])
        link = a["location"]["link"]
        loc = self.srv.get_path(link)
        check(loc.status_code == 200 and "lat" in loc.json(), f"location link: {loc.status_code}")
        note(f"escalated (fast-forward, labelled demo); a sensor event at L1 changes nothing; location link 200 "
             f"({'simulated' if loc.json()['simulated'] else 'real'})")

        r = self.cancel(sid, self.pin)
        check(r.content == real_pin, f"real PIN reply: {r.content!r}")
        check(self.srv.state(sid) == "ARMED" and self.srv.alert(sid) is None, "real PIN did not close it")
        msg = self.wait_contact_msg(sid, self.A, "false_alarm")
        check(msg["text"].startswith(f"StillHere: False alarm: {self.name} cancelled with her PIN at "), msg["text"])
        check(self.srv.calert(self.A["token"]) is None, "contact page still shows the alert")
        gone = self.srv.get_path(link)
        check(gone.status_code == 410, f"location link after the false alarm: {gone.status_code}")
        note(f'real PIN -> FALSE_ALARM -> ARMED; A told "{msg["text"][len("StillHere: "):]}"; page blank; link 410')

    def s7_privacy(self) -> None:
        sid = self.reset()
        for c, approved in ((self.A, True), (self.B, True), (self.P, False)):
            code, body = self.srv.cstatus(c["token"])
            check(code == 200 and body == {"alert": None, "approved": approved}, f"{c['name']} before alert: {body}")
        note('before an alert every contact page gets only {"alert": null, "approved": ...}')
        for h in ({}, {"Authorization": f"Bearer {self.A['token']}"}):
            r = self.srv.c.get(f"/v1/sessions/{sid}", headers=h)
            check(r.status_code == 401, f"contact token read the session: {r.status_code}")
        r = self.srv.c.post(f"/v1/contact/{self.A['invite']}/join", json={})
        check(r.status_code == 410, f"invite reused: {r.status_code}")
        check(self.srv.c.get(f"/v1/contact/{self.A['invite']}/invite").status_code == 410, "invite info after use")
        check(self.srv.cact(self.P["token"], "ack").status_code == 404, "unapproved contact could act")
        note("a contact token cannot read the session; invites are single use (410); unapproved contact cannot act")

        self.srv.to_l1(sid)
        check(self.srv.calert(self.A["token"]) is not None, "A not alerted")
        code, body = self.srv.cstatus(self.P["token"])
        check(code == 200 and body == {"alert": None, "approved": False}, f"unapproved contact saw: {body}")
        check(self.srv.cact(self.P["token"], "ack").status_code == 404, "unapproved contact acked")
        r = self.srv.cact(self.A["token"], "pass")
        check(r.status_code == 200 and r.json()["result"] == "passed", f"pass: {r.text}")
        self.srv.wait_state(sid, "ESCALATED_L2", 3)
        b = self.srv.calert(self.B["token"])
        check(b and b["actions"] == ["ack", "pass"], f"B after pass: {b}")
        note("A: Can't help - pass it on -> L2 at once; B alerted")
        r = self.srv.cact(self.B["token"], "ack")
        check(r.json()["result"] == "acked", r.text)
        a = self.srv.calert(self.A["token"])
        check(a["handled_by"] == "SIM-B" and a["actions"] == [], f"A while B handles it: {a}")
        link = b["location"]["link"]
        r = self.srv.cact(self.B["token"], "safe")
        check(r.json()["result"] == "resolved" and self.srv.state(sid) == "ARMED", r.text)
        check(self.srv.get_path(link).status_code == 200, "location link closed too early")
        t0 = time.monotonic()
        wait_for(lambda: self.srv.get_path(link).status_code == 410, T.link_expiry_s + 8, "location link 410")
        dt = time.monotonic() - t0
        check(self.srv.calert(self.A["token"]) is None and self.srv.calert(self.B["token"]) is None, "pages not blank")
        rows = self.srv.outbox(contact_id=self.P["id"])
        check(rows == [], f"unapproved contact got {len(rows)} message(s)")
        note(f'B: on it ("SIM-B is handling it" shown to A), then safe; location link 200, then 410 after {dt:.1f} s '
             f"(expiry {T.link_expiry_s:.0f} s)")
        note("unapproved SIM-P received nothing in the whole run (0 outbox rows)")


SCENARIOS = [
    ("1", "safe", Run.s1_safe),
    ("2", "no response", Run.s2_no_response),
    ("3", "duress", Run.s3_duress),
    ("4", "connectivity failure / lost contact", Run.s4_lost_contact),
    ("5", "unacknowledged -> next contact -> FINAL", Run.s5_unacknowledged),
    ("6", "false alarm (real PIN)", Run.s6_false_alarm),
    ("7", "privacy", Run.s7_privacy),
]


def pcc_seed(srv: Server) -> str:
    """PointClickCare senior mode: seed the synthetic cohort, then check the alert cards it gives."""
    seed = srv.ok(srv.admin("POST", "/v1/demo/care-seed", json={}), "care seed")
    changes, steady = seed["scripted_change"], seed["no_change_scripts"]
    check(seed.get("synthetic") is True and seed["residents"] == 20, f"care seed: {seed}")
    check(changes["flagged"] == changes["residents"],
          f"scripted changes flagged {changes['flagged']}/{changes['residents']}")
    check(steady["flagged"] == 0, f"no-change scripts flagged {steady['flagged']}")
    alerts = srv.ok(srv.c.get("/v1/care/alerts"), "care alerts")["alerts"]
    flagged = [a for a in alerts if a["level"] in ("watch", "check today")]
    check(len(flagged) == changes["residents"], f"/v1/care/alerts lists {len(flagged)} flagged cards")
    check(all(a["synthetic"] for a in alerts), "a care card is not labelled synthetic")
    return (f"{seed['residents']} synthetic residents seeded; {len(flagged)} flagged cards on /v1/care/alerts "
            f"(all {changes['residents']} scripted changes, 0 of {steady['residents']} no-change scripts)")


# Every step of a run: the seven KAZE scenarios, then the PointClickCare seed.
STEPS = SCENARIOS + [("pcc", "PointClickCare senior mode: synthetic cohort seed", lambda run: note(pcc_seed(run.srv)))]


def main(argv: Optional[list] = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(BACKEND / ".env")
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description="Run the seven KAZE scenarios and the PCC seed against a running server.")
    ap.add_argument("only", nargs="*", help="scenario numbers and/or 'pcc' (default: all)")
    ap.add_argument("--url", default=os.environ.get("STILLHERE_URL", "http://localhost:8000"))
    args = ap.parse_args(argv)
    env = {k: os.environ.get(k) for k in ("ADMIN_KEY", "DEVICE_KEY", "WEARER_KEY", "DEMO_PIN", "DEMO_DURESS_PIN")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print(f"missing env: {', '.join(missing)} (use the server's values)", file=sys.stderr)
        return 2
    srv = Server(args.url, env["ADMIN_KEY"], env["WEARER_KEY"])
    try:
        health = srv.c.get("/v1/health").json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"server not reachable at {args.url}: {exc}", file=sys.stderr)
        return 2
    print(f"StillHere at {args.url}: db {health.get('db')}, telegram {health.get('telegram')}, "
          f"emergency services {health.get('emergency_services')}", flush=True)
    srv.ok(srv.wearer("GET", "/v1/contacts"), "wearer key check")  # validates WEARER_KEY early
    dev = FakeDevice(args.url, os.environ.get("DEMO_DEVICE_ID") or "scrunchie-1", env["DEVICE_KEY"])
    run = Run(srv, dev, env["DEMO_PIN"], env["DEMO_DURESS_PIN"], os.environ.get("DEMO_WEARER_NAME") or "Maya")
    run.setup_contacts()
    dev.start()
    chosen = [s for s in STEPS if not args.only or s[0] in args.only]
    results = []
    try:
        for num, title, fn in chosen:
            print(f"[{num}] {title}", flush=True)
            t0 = time.monotonic()
            try:
                fn(run)
                ok, why = True, ""
            except Fail as exc:
                ok, why = False, str(exc)
            except Exception as exc:  # a crash is a failure too
                ok, why = False, f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            dt = time.monotonic() - t0
            results.append((num, title, ok, dt))
            print(f"[{num}] {title}: {'PASS' if ok else 'FAIL'} ({dt:.1f} s){'  ' + why if why else ''}", flush=True)
    finally:
        dev.close()
        run.cleanup()
    passed = sum(1 for r in results if r[2])
    print(f"\n{passed}/{len(results)} steps passed", flush=True)
    for num, title, ok, dt in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {num} {title}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
