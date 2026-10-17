"""Ops layer over HTTP: the served pages, the consent-screen invite info, contact actions per level,
and the operator views the simulator and chaos script use."""
from __future__ import annotations

import re

from app import states
from tests.conftest import ADMIN_KEY, fire, make_world, to_state

ADMIN = {"X-Admin-Key": ADMIN_KEY}


async def test_contact_page_is_served_with_the_required_parts(client):
    r = await client.get("/c/some-token")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    page = r.text
    for label in ("I'm on it", "Can't help – pass it on", "I'm with her – she's safe", "Can't reach her"):
        assert label in page, label
    assert "Tap to ask to become Contact #" in page
    assert "/status" in page and "setTimeout(poll, 1000)" in page          # polls every second
    assert "audio.loop = true" in page and "audio.muted" in page            # looping alarm, muted until an alert
    assert "navigator.audioSession" in page and "wakeLock" in page
    assert "Alarm.unlock();" in page.split("function join(")[1][:200]       # the consent tap unlocks the alarm
    assert "a.banner" in page                                               # FINAL banner comes from the server
    assert "[hidden] { display: none !important; }" in page                 # "Live" stays hidden until polling
    assert not re.search("buzz", page, re.IGNORECASE)                       # never says the phone buzzes


async def test_hr_bridge_page_is_served(client):
    r = await client.get("/hr-bridge")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    assert "requestDevice({filters: [{services: [\"heart_rate\"]}]})" in page
    assert "getCharacteristic(\"heart_rate_measurement\")" in page and "startNotifications" in page
    assert "gattserverdisconnected" in page and "backoff" in page
    assert "fetch(\"/v1/hr\"" in page and "SIMULATED SIGNAL" in page and "simulated: true" in page
    # Live samples are posted from the notification handler, not from a timer.
    handler = page.split("function onHrm(event)")[1].split("\n  }\n")[0]
    assert "enqueue(" in handler


async def test_invite_info_for_the_consent_screen(client, db):
    w = await make_world(client, db, contacts=1, arm=False)
    inv = (await client.post("/v1/contacts", json={"name": "Judge"}, headers=w.wearer)).json()
    token = inv["web_link"].rsplit("/", 1)[1]
    r = await client.get(f"/v1/contact/{token}/invite")
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True and body["wearer"] == "Maya" and body["n"] == 2 and 0 < body["expires_in_s"] <= 600
    assert set(body) == {"valid", "wearer", "n", "expires_in_s"}           # nothing about the session
    assert (await client.post(f"/v1/contact/{token}/join", json={"name": "Judge"})).status_code == 200
    assert (await client.get(f"/v1/contact/{token}/invite")).status_code == 410   # single use
    assert (await client.get("/v1/contact/not-a-token/invite")).status_code == 410


async def test_only_the_current_level_contact_can_pass_it_on(client, db, ctx):
    w = await make_world(client, db, contacts=2)
    c1, c2 = w.contacts
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    a1 = (await client.get(f"/v1/contact/{c1['token']}/status")).json()["alert"]
    assert a1["actions"] == ["ack", "pass"] and a1["level"] == 1
    await fire(db, ctx, w.session_id, states.ACK_TIMEOUT)
    a1 = (await client.get(f"/v1/contact/{c1['token']}/status")).json()["alert"]
    a2 = (await client.get(f"/v1/contact/{c2['token']}/status")).json()["alert"]
    assert a2["actions"] == ["ack", "pass"] and a2["level"] == 2
    assert a1["actions"] == ["ack"]                                         # re-pinged: can take it on, not pass
    r = await client.post(f"/v1/contact/{c1['token']}/followup", json={"action": "pass"})
    assert r.json() == {"ok": False, "result": "ignored"}                    # the state machine agrees
    await fire(db, ctx, w.session_id, states.ACK_TIMEOUT)
    for c in (c1, c2):
        a = (await client.get(f"/v1/contact/{c['token']}/status")).json()["alert"]
        assert a["final"] and a["actions"] == ["ack"]


async def test_operator_outbox_and_recent_timers(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    assert (await client.get("/v1/admin/outbox")).status_code in (401, 503)
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    rows = (await client.get("/v1/admin/outbox", params={"session_id": w.session_id}, headers=ADMIN)).json()["outbox"]
    assert [r["id"] for r in rows] == sorted(r["id"] for r in rows)
    assert {r["recipient"] for r in rows} == {"wearer", "contact"}
    only_c1 = (await client.get("/v1/admin/outbox", params={"session_id": w.session_id,
                                                            "contact_id": w.contacts[0]["id"]},
                                headers=ADMIN)).json()["outbox"]
    assert [r["template"] for r in only_c1] == ["alert_no_response"]
    t = (await client.get("/v1/admin/timers", params={"recent": 20}, headers=ADMIN)).json()
    mine = [x for x in t["recent_fired"] if x["session_id"] == w.session_id]
    assert {x["kind"] for x in mine} >= {"CHECKIN_DUE", "PROMPT_TIMEOUT", "REMINDER_TIMEOUT"}
    assert all("lag_s" in x and x["result"] for x in mine)
    assert "recent_fired" not in (await client.get("/v1/admin/timers", headers=ADMIN)).json()
