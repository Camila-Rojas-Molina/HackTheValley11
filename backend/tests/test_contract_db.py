"""API contract tests: every route the app serves, against docs/API.md.

For each endpoint: the status codes of a good call and of the documented failures, every auth failure for
its credential (device key + X-Device-Id, wearer key, X-Admin-Key, contact capability token), and the
shape of the response (keys and types; exact key sets where the contract is exact, such as the device
replies that firmware parses).

test_every_route_has_a_contract fails when a route is added without an entry here, so the table cannot
fall behind the app.
"""
from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

import pytest

from app import states
from tests.conftest import ADMIN_KEY, DURESS_PIN, PIN, fire, make_world, to_state
from tests.contract_routes import served_routes

ADMIN = {"X-Admin-Key": ADMIN_KEY}
NOT_A_KEY = {"Authorization": "Bearer not-a-real-key"}
CONTRACTS: dict[tuple[str, str], Callable[..., Awaitable[None]]] = {}


def contract(method: str, path: str):
    def deco(fn):
        assert (method, path) not in CONTRACTS, (method, path)
        CONTRACTS[(method, path)] = fn
        return fn
    return deco


# --- shape helpers -----------------------------------------------------------------------------------
def shape(body: Any, spec: Any, where: str = "$") -> None:
    """spec: a type (or tuple of types), a dict of key -> spec (keys must be present), or a list
    holding one spec for every item."""
    if isinstance(spec, dict):
        assert isinstance(body, dict), f"{where}: expected an object, got {body!r}"
        for k, sub in spec.items():
            assert k in body, f"{where}: missing key {k!r} in {sorted(body)}"
            shape(body[k], sub, f"{where}.{k}")
    elif isinstance(spec, list):
        assert isinstance(body, list), f"{where}: expected a list, got {body!r}"
        for i, item in enumerate(body):
            shape(item, spec[0], f"{where}[{i}]")
    else:
        assert isinstance(body, spec), f"{where}: expected {spec}, got {type(body).__name__} {body!r}"


OPT_STR = (str, type(None))
NUM = (int, float)
CMD = {"led": str, "blink_n": int, "challenge_id": OPT_STR}


async def wearer_auth(client, method: str, path: str, json=None, contact_token: str | None = None) -> None:
    r = await client.request(method, path, json=json)
    assert (r.status_code, r.json()) == (401, {"detail": "wearer key required"}), (path, r.text)
    r = await client.request(method, path, json=json, headers=NOT_A_KEY)
    assert (r.status_code, r.json()) == (401, {"detail": "wrong wearer key"}), (path, r.text)
    if contact_token:  # a contact's capability token is not a wearer key
        r = await client.request(method, path, json=json, headers={"Authorization": f"Bearer {contact_token}"})
        assert r.status_code == 401, (path, r.text)


async def device_auth(client, method: str, path: str, w, json=None) -> None:
    r = await client.request(method, path, json=json)
    assert (r.status_code, r.json()) == (401, {"detail": "device key and X-Device-Id required"}), r.text
    bad = {**w.device(), "Authorization": "Bearer wrong"}
    r = await client.request(method, path, json=json, headers=bad)
    assert (r.status_code, r.json()) == (401, {"detail": "unknown device or wrong key"}), r.text
    other = {**w.device(), "X-Device-Id": "no-such-device"}
    r = await client.request(method, path, json=json, headers=other)
    assert (r.status_code, r.json()) == (401, {"detail": "unknown device or wrong key"}), r.text


async def admin_auth(client, method: str, path: str, json=None, params=None) -> None:
    for headers in ({}, {"X-Admin-Key": "wrong"}, NOT_A_KEY):
        r = await client.request(method, path, json=json, params=params, headers=headers)
        assert (r.status_code, r.json()) == (401, {"detail": "wrong X-Admin-Key"}), (path, headers, r.text)


async def demo_reset(client) -> str:
    r = await client.post("/v1/demo/reset", headers=ADMIN, json={})
    assert r.status_code == 200, r.text
    return r.json()["armed"]["session_id"]


# --- the meta test -----------------------------------------------------------------------------------
def test_every_route_has_a_contract():
    served = served_routes()
    assert len(served) >= 60
    assert sorted(served - set(CONTRACTS)) == [], "routes without a contract test"
    assert sorted(set(CONTRACTS) - served) == [], "contract tests for routes the app does not serve"


# --- health ------------------------------------------------------------------------------------------
@contract("GET", "/v1/health")
async def _health(client, db, ctx):
    r = await client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    shape(body, {"ok": bool, "db": str, "server_time": str, "telegram": str, "default_profile": str,
                 "emergency_services": str, "rule_thresholds": {"status": str, "values": dict, "notes": dict}})
    assert body["db"] == "up" and body["emergency_services"] == "SIMULATED (never placed)"
    assert not {"pending_timers", "pending_outbox", "failing_timers"} & set(body)   # public body: no counters
    op = (await client.get("/v1/health", headers=ADMIN)).json()
    shape(op, {"pending_timers": int, "pending_outbox": int, "failing_timers": int})
    wrong = (await client.get("/v1/health", headers={"X-Admin-Key": "wrong"})).json()
    assert "pending_timers" not in wrong


# --- device ------------------------------------------------------------------------------------------
@contract("POST", "/v1/device/heartbeat")
async def _heartbeat(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await device_auth(client, "POST", "/v1/device/heartbeat", w, json={})
    r = await client.post("/v1/device/heartbeat", headers=w.device(), json={"wear_mode": "hair", "enmo60_mg": 12.5,
                                                                           "offbody": False})
    assert r.status_code == 200 and r.headers["content-type"] == "application/json"
    assert set(r.json()) == {"cmd", "server_time", "state_public", "telemetry"}
    shape(r.json(), {"cmd": CMD, "server_time": str, "state_public": str, "telemetry": str})
    assert set(r.json()["cmd"]) == {"led", "blink_n", "challenge_id"}
    assert r.content == r.content.replace(b": ", b":")          # canonical JSON: no spaces
    assert (await client.post("/v1/device/heartbeat", headers=w.device())).status_code == 200  # body optional
    for bad in ({"enmo60_mg": -1}, {"enmo60_mg": 20001}, {"wear_mode": "ankle"}):
        assert (await client.post("/v1/device/heartbeat", headers=w.device(), json=bad)).status_code == 422
    assert (await client.post("/v1/device/heartbeat", headers=w.device(seq=2 ** 63), json={})).status_code == 422


@contract("POST", "/v1/device/events")
async def _events(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    ev = {"event_id": "c:1", "type": "squeeze", "payload": {"pattern": "plain", "k": 2}}
    await device_auth(client, "POST", "/v1/device/events", w, json=ev)
    r = await client.post("/v1/device/events", headers=w.device(), json=ev)
    assert r.status_code == 200
    assert set(r.json()) == {"cmd", "ok", "result", "state_public"}
    shape(r.json(), {"cmd": CMD, "ok": bool, "result": str, "state_public": str})
    assert r.json()["result"] in ("noted", "retry", "prompted", "ignored", "cooldown", "deadline_set", "no_session")
    again = await client.post("/v1/device/events", headers=w.device(), json=ev)
    assert again.content == r.content                                # idempotent replay, byte for byte
    for bad in ({"event_id": "", "type": "squeeze"}, {"event_id": "c:2", "type": "spin"},
                {"event_id": "c:3", "type": "squeeze", "device_ms": -1}):
        assert (await client.post("/v1/device/events", headers=w.device(), json=bad)).status_code == 422, bad
    inf = await client.post("/v1/device/events", headers={**w.device(), "Content-Type": "application/json"},
                            content=b'{"event_id":"c:4","type":"squeeze","payload":{"k":1e999}}')
    assert inf.status_code == 422                                    # infinity: never a 500


# --- heart rate --------------------------------------------------------------------------------------
@contract("GET", "/hr-bridge")
async def _hr_bridge(client, db, ctx):
    r = await client.get("/hr-bridge")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert 'id="sim"' in r.text and "/v1/hr" in r.text


@contract("POST", "/v1/hr")
async def _hr(client, db, ctx):
    w = await make_world(client, db, contacts=0)
    body = {"source": "contract", "simulated": True, "samples": [{"bpm": 71, "rr_ms": [845.0]}]}
    await wearer_auth(client, "POST", "/v1/hr", json=body)
    r = await client.post("/v1/hr", headers=w.wearer, json=body)
    assert (r.status_code, r.json()) == (200, {"ok": True, "inserted": 1})
    single = await client.post("/v1/hr", headers=w.wearer, json={"bpm": 72, "source": "contract",
                                                                 "ts": "2026-10-17T07:33:53Z"})
    assert single.json() == {"ok": True, "inserted": 1}
    r = await client.post("/v1/hr", headers=w.device(), json=body)                 # the device key works too
    assert r.status_code == 200
    bad_dev = {**w.device(), "Authorization": "Bearer wrong"}
    assert (await client.post("/v1/hr", headers=bad_dev, json=body)).status_code == 401
    other = str(uuid.uuid4())
    assert (await client.post("/v1/hr", headers=w.wearer, json={**body, "user_id": other})).status_code == 403
    for bad in ({"source": "x", "samples": []}, {"source": "x", "samples": [{"bpm": 19}]},
                {"source": "x", "samples": [{"bpm": 251}]}, {"source": "x", "samples": [{"bpm": 70, "rr_ms": [70000]}]}):
        assert (await client.post("/v1/hr", headers=w.wearer, json=bad)).status_code == 422, bad


@contract("GET", "/v1/hr/status")
async def _hr_status(client, db, ctx):
    w = await make_world(client, db, contacts=0)
    await wearer_auth(client, "GET", "/v1/hr/status")
    r = await client.get("/v1/hr/status", headers=w.wearer)
    assert r.status_code == 200
    shape(r.json(), {"label": str, "stale": bool, "stale_after_s": NUM, "display_only": bool, "last_at": OPT_STR,
                     "age_s": (int, float, type(None)), "sources": list, "duplicate_sources": bool, "note": str})
    assert r.json()["label"] == "no heart rate yet" and r.json()["display_only"] is True
    await client.post("/v1/hr", headers=w.wearer, json={"bpm": 70, "source": "contract"})
    r = await client.get("/v1/hr/status", headers=w.device())
    assert r.json()["label"] == "HR live"
    shape(r.json()["sources"], [{"source": str, "samples_60s": int, "last_at": str, "simulated": bool}])


@contract("POST", "/v1/hr/calibrate")
async def _hr_calibrate(client, db, ctx):
    w = await make_world(client, db, contacts=0)
    await wearer_auth(client, "POST", "/v1/hr/calibrate", json={})
    r = await client.post("/v1/hr/calibrate", headers=w.wearer, json={"window_s": 300})
    assert r.status_code == 409                                       # fewer than 5 independent readings
    async with db.connection() as conn:
        for i in range(6):
            await conn.execute("""INSERT INTO hr_samples (time, user_id, source, simulated, bpm)
                                  VALUES (now() - make_interval(secs => %s), %s, 'contract', false, 70)""",
                               (5 * i + 1, w.user_id))
    r = await client.post("/v1/hr/calibrate", headers=w.wearer, json={"window_s": 300})
    assert r.status_code == 200
    shape(r.json(), {"ok": bool, "baseline_bpm": NUM, "samples": int, "readings": int, "simulated": bool})
    assert r.json()["baseline_bpm"] == 70.0
    for bad in ({"window_s": 9}, {"window_s": 3601}):
        assert (await client.post("/v1/hr/calibrate", headers=w.wearer, json=bad)).status_code == 422


# --- wearer ------------------------------------------------------------------------------------------
@contract("POST", "/v1/sessions")
async def _arm(client, db, ctx):
    w = await make_world(client, db, contacts=1, arm=False)
    await wearer_auth(client, "POST", "/v1/sessions", json={"profile": "demo"})
    r = await client.post("/v1/sessions", headers=w.wearer, json={"preset": "night_out", "mode": "hair",
                                                                  "profile": "demo"})
    assert r.status_code == 200
    shape(r.json(), {"session_id": str, "state_public": str, "profile": str, "interval_s": int,
                     "end_at": OPT_STR, "who_can_be_alerted": [{"n": int, "name": str, "contact_id": int}]})
    assert r.json()["state_public"] == "ARMED"
    dup = await client.post("/v1/sessions", headers=w.wearer, json={"profile": "demo"})
    assert dup.status_code == 409 and dup.json()["detail"]["error"] == "already_armed"
    other = await make_world(client, db, contacts=0, arm=False)
    r = await client.post("/v1/sessions", headers=other.wearer, json={"device_id": w.device_id})
    assert (r.status_code, r.json()) == (404, {"detail": "no such device"})
    for bad in ({"preset": "party"}, {"interval_s": 9}, {"window_multiplier": 11}, {"mode": "ankle"}):
        assert (await client.post("/v1/sessions", headers=other.wearer, json=bad)).status_code == 422, bad


async def _session_routes_common(client, db, path_fmt: str, method: str = "GET", json=None) -> None:
    """Auth failures, another wearer's session (404) and a malformed id (404)."""
    w = await make_world(client, db, contacts=1)
    other = await make_world(client, db, contacts=0)
    path = path_fmt.format(sid=w.session_id)
    await wearer_auth(client, method, path, json=json, contact_token=w.contacts[0]["token"])
    r = await client.request(method, path, headers=other.wearer, json=json)
    assert (r.status_code, r.json()) == (404, {"detail": "no such session"}), r.text
    r = await client.request(method, path_fmt.format(sid="not-a-uuid"), headers=w.wearer, json=json)
    assert r.status_code == 404


@contract("GET", "/v1/sessions/{session_id}")
async def _session_status(client, db, ctx):
    await _session_routes_common(client, db, "/v1/sessions/{sid}")
    w = await make_world(client, db, contacts=1)
    r = await client.get(f"/v1/sessions/{w.session_id}", headers=w.wearer)
    assert r.status_code == 200
    shape(r.json(), {"session_id": str, "state_public": str, "cmd": CMD, "link_state": str, "profile": str,
                     "mode": str, "who_can_be_alerted": list})


@contract("POST", "/v1/sessions/{session_id}/checkin")
async def _checkin(client, db, ctx):
    await _session_routes_common(client, db, "/v1/sessions/{sid}/checkin", "POST", {})
    w = await make_world(client, db, contacts=1)
    r = await client.post(f"/v1/sessions/{w.session_id}/checkin", headers=w.wearer,
                          json={"lat": 43.78, "lon": -79.18, "accuracy_m": 25})
    assert r.status_code == 200 and r.json() == {"message": "Noted ✓", "ok": True, "state_public": "ARMED"}
    assert r.content == b'{"message":"Noted \xe2\x9c\x93","ok":true,"state_public":"ARMED"}'
    for bad in ({"lat": 91}, {"lon": -181}):
        assert (await client.post(f"/v1/sessions/{w.session_id}/checkin", headers=w.wearer, json=bad)).status_code == 422


@contract("POST", "/v1/sessions/{session_id}/arrived")
async def _arrived(client, db, ctx):
    await _session_routes_common(client, db, "/v1/sessions/{sid}/arrived", "POST")
    w = await make_world(client, db, contacts=1)
    r = await client.post(f"/v1/sessions/{w.session_id}/arrived", headers=w.wearer)
    assert r.status_code == 409                                      # not a walking-home session
    walk = await make_world(client, db, contacts=1, arm=False)
    sid = (await client.post("/v1/sessions", headers=walk.wearer, json={"preset": "walking_home",
                                                                       "profile": "demo"})).json()["session_id"]
    r = await client.post(f"/v1/sessions/{sid}/arrived", headers=walk.wearer)
    assert (r.status_code, r.json()) == (200, {"message": "Ended ✓", "ok": True, "state_public": "ENDED"})


@pytest.mark.parametrize("action,reply", [("cancel", {"message": "Cancelled ✓", "ok": True, "state_public": "ARMED"}),
                                          ("end", {"message": "Ended ✓", "ok": True, "state_public": "ENDED"})])
async def test_pin_replies_follow_the_contract(client, db, ctx, action, reply):
    real, duress = await make_world(client, db, contacts=1), await make_world(client, db, contacts=1)
    a = await client.post(f"/v1/sessions/{real.session_id}/{action}", headers=real.wearer, json={"pin": PIN})
    b = await client.post(f"/v1/sessions/{duress.session_id}/{action}", headers=duress.wearer, json={"pin": DURESS_PIN})
    assert a.status_code == b.status_code == 200 and a.json() == reply and a.content == b.content
    wrong = await client.post(f"/v1/sessions/{real.session_id}/{action}", headers=real.wearer, json={"pin": "0000"})
    assert (wrong.status_code, wrong.json()) == (403, {"error": "wrong_pin", "ok": False})
    for bad in ({}, {"pin": ""}, {"pin": "1" * 33}):
        assert (await client.post(f"/v1/sessions/{real.session_id}/{action}", headers=real.wearer,
                                  json=bad)).status_code == 422


@contract("POST", "/v1/sessions/{session_id}/cancel")
async def _cancel(client, db, ctx):
    await _session_routes_common(client, db, "/v1/sessions/{sid}/cancel", "POST", {"pin": PIN})


@contract("POST", "/v1/sessions/{session_id}/end")
async def _end(client, db, ctx):
    await _session_routes_common(client, db, "/v1/sessions/{sid}/end", "POST", {"pin": PIN})


@contract("GET", "/v1/sessions/{session_id}/timeline")
async def _timeline(client, db, ctx):
    await _session_routes_common(client, db, "/v1/sessions/{sid}/timeline")
    w = await make_world(client, db, contacts=1)
    r = await client.get(f"/v1/sessions/{w.session_id}/timeline", headers=w.wearer)
    assert r.status_code == 200
    shape(r.json(), {"session_id": str, "entries": [{"at": str, "actor": str, "kind": str, "from_state": OPT_STR,
                                                     "to_state": OPT_STR, "detail": dict}]})
    assert r.json()["entries"][0]["kind"] == "armed"


@contract("POST", "/v1/sessions/{session_id}/pin")
async def _pin_timeline(client, db, ctx):
    await _session_routes_common(client, db, "/v1/sessions/{sid}/pin", "POST", {"pinned": True})
    w = await make_world(client, db, contacts=0)
    r = await client.post(f"/v1/sessions/{w.session_id}/pin", headers=w.wearer, json={"pinned": True})
    assert (r.status_code, r.json()) == (200, {"ok": True, "session_id": w.session_id, "pinned": True})
    r = await client.post(f"/v1/sessions/{w.session_id}/pin", headers=w.wearer, json={"pinned": False})
    assert r.json()["pinned"] is False


@contract("GET", "/v1/me/export")
async def _export(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await wearer_auth(client, "GET", "/v1/me/export", contact_token=w.contacts[0]["token"])
    r = await client.get("/v1/me/export", headers=w.wearer)
    assert r.status_code == 200
    shape(r.json(), {"user": str, "sessions": [{"session_id": str, "created_at": str, "ended_at": OPT_STR,
                                                "chain_ok": bool, "pinned": bool, "head_hash": str, "entries": list}],
                     "account_chain_ok": bool, "account_head_hash": OPT_STR, "note": str, "retention": str})
    assert all(s["chain_ok"] for s in r.json()["sessions"])
    html = await client.get("/v1/me/export", headers=w.wearer, params={"format": "html"})
    assert html.status_code == 200 and html.headers["content-type"].startswith("text/html")
    assert "Chain verified" in html.text and "<script" not in html.text


@contract("GET", "/v1/me/alerts")
async def _me_alerts(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await wearer_auth(client, "GET", "/v1/me/alerts")
    assert (await client.get("/v1/me/alerts", headers=w.wearer)).json() == {"alerts": []}
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    r = await client.get("/v1/me/alerts", headers=w.wearer)
    assert r.status_code == 200
    shape(r.json(), {"alerts": [{"alert_id": int, "session_id": str, "kind": str, "level": int, "final": bool,
                                 "status": str, "opened_at": str, "closed_at": OPT_STR, "resolved_by": OPT_STR,
                                 "alerted": [{"contact_id": int, "name": str, "first_notified_at": str}],
                                 "acknowledged": list}]})


@contract("DELETE", "/v1/me/data")
async def _me_data(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await wearer_auth(client, "DELETE", "/v1/me/data")
    r = await client.delete("/v1/me/data", headers=w.wearer)
    assert r.status_code == 200
    shape(r.json(), {"ok": bool, "erased": {"timeline_entries": int, "hr_samples": int, "telemetry_rows": int,
                                            "rollup_rows": int}})


@contract("POST", "/v1/me/telegram-link")
async def _telegram_link(client, db, ctx):
    w = await make_world(client, db, contacts=0)
    await wearer_auth(client, "POST", "/v1/me/telegram-link")
    r = await client.post("/v1/me/telegram-link", headers=w.wearer)
    assert r.status_code == 200
    shape(r.json(), {"t_me_link": OPT_STR, "start_token": str, "expires_in_s": int})
    assert r.json()["expires_in_s"] == 600


# --- the wearer's contacts --------------------------------------------------------------------------
@contract("POST", "/v1/contacts")
async def _contacts_create(client, db, ctx):
    w = await make_world(client, db, contacts=0, arm=False)
    await wearer_auth(client, "POST", "/v1/contacts", json={"name": "Sam"})
    r = await client.post("/v1/contacts", headers=w.wearer, json={"name": "Sam", "priority": 3})
    assert r.status_code == 200
    shape(r.json(), {"contact_id": int, "priority": int, "web_link": str, "t_me_link": OPT_STR, "qr_png": str,
                     "expires_at": str})
    assert r.json()["qr_png"].startswith("data:image/png;base64,") and "/c/" in r.json()["web_link"]
    for bad in ({"name": "x" * 61}, {"name": "a", "priority": 0}, {"name": "a", "priority": 1001}):
        assert (await client.post("/v1/contacts", headers=w.wearer, json=bad)).status_code == 422, bad


@contract("GET", "/v1/contacts")
async def _contacts_list(client, db, ctx):
    w = await make_world(client, db, contacts=1, pending=1)
    await wearer_auth(client, "GET", "/v1/contacts")
    r = await client.get("/v1/contacts", headers=w.wearer)
    assert r.status_code == 200
    shape(r.json(), {"contacts": [{"contact_id": int, "name": str, "priority": int, "channel": OPT_STR,
                                   "status": str, "duress_eligible": bool}],
                     "duress_recipients": {"mode": str, "contact_ids": list}})
    assert sorted(c["status"] for c in r.json()["contacts"]) == ["approved", "pending_approval"]


@contract("POST", "/v1/contacts/{contact_id}/approve")
async def _approve(client, db, ctx):
    w = await make_world(client, db, contacts=0, pending=1)
    cid = w.pending[0]["id"]
    await wearer_auth(client, "POST", f"/v1/contacts/{cid}/approve")
    other = await make_world(client, db, contacts=0)
    assert (await client.post(f"/v1/contacts/{cid}/approve", headers=other.wearer)).status_code == 404
    r = await client.post(f"/v1/contacts/{cid}/approve", headers=w.wearer)
    assert r.status_code == 200
    shape(r.json(), {"ok": bool, "contact_id": int, "n": int, "name": str})
    invited = (await client.post("/v1/contacts", headers=w.wearer, json={"name": "J"})).json()["contact_id"]
    assert (await client.post(f"/v1/contacts/{invited}/approve", headers=w.wearer)).status_code == 404  # not joined


@contract("PUT", "/v1/contacts/{contact_id}/duress")
async def _duress(client, db, ctx):
    w = await make_world(client, db, contacts=2)
    cid = w.contacts[1]["id"]
    await wearer_auth(client, "PUT", f"/v1/contacts/{cid}/duress", json={"eligible": True})
    r = await client.put(f"/v1/contacts/{cid}/duress", headers=w.wearer, json={"eligible": True})
    assert (r.status_code, r.json()) == (200, {"ok": True, "contact_id": cid, "duress_eligible": True})
    other = await make_world(client, db, contacts=0)
    assert (await client.put(f"/v1/contacts/{cid}/duress", headers=other.wearer,
                             json={"eligible": True})).status_code == 404
    assert (await client.put(f"/v1/contacts/{cid}/duress", headers=w.wearer, json={})).status_code == 422


@contract("DELETE", "/v1/contacts")
async def _contacts_delete_all(client, db, ctx):
    w = await make_world(client, db, contacts=2, pending=1)
    await wearer_auth(client, "DELETE", "/v1/contacts")
    r = await client.delete("/v1/contacts", headers=w.wearer)
    assert (r.status_code, r.json()) == (200, {"ok": True, "removed": 3})
    assert (await client.get(f"/v1/contact/{w.contacts[0]['token']}/status")).status_code == 404


@contract("DELETE", "/v1/contacts/{contact_id}")
async def _contacts_delete_one(client, db, ctx):
    w = await make_world(client, db, contacts=2)
    cid = w.contacts[0]["id"]
    await wearer_auth(client, "DELETE", f"/v1/contacts/{cid}")
    other = await make_world(client, db, contacts=0)
    assert (await client.delete(f"/v1/contacts/{cid}", headers=other.wearer)).status_code == 404
    r = await client.delete(f"/v1/contacts/{cid}", headers=w.wearer)
    assert (r.status_code, r.json()) == (200, {"ok": True})
    assert (await client.get(f"/v1/contact/{w.contacts[0]['token']}/status")).status_code == 404
    assert (await client.get(f"/v1/contact/{w.contacts[1]['token']}/status")).status_code == 200


# --- contacts' own pages (capability tokens) ---------------------------------------------------------
@contract("GET", "/c/{token}")
async def _contact_page(client, db, ctx):
    r = await client.get("/c/anything")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert "StillHere contact" in r.text


@contract("GET", "/v1/contact/{token}/invite")
async def _invite(client, db, ctx):
    w = await make_world(client, db, contacts=0, arm=False)
    inv = (await client.post("/v1/contacts", headers=w.wearer, json={"name": "Sam"})).json()
    tok = inv["web_link"].rsplit("/", 1)[1]
    r = await client.get(f"/v1/contact/{tok}/invite")
    assert r.status_code == 200
    shape(r.json(), {"valid": bool, "wearer": str, "n": int, "expires_in_s": int})
    assert set(r.json()) == {"valid", "wearer", "n", "expires_in_s"}           # no session data
    assert (await client.get("/v1/contact/unknown-token/invite")).status_code == 410
    await client.post(f"/v1/contact/{tok}/join", json={})
    used = await client.get(f"/v1/contact/{tok}/invite")
    assert (used.status_code, used.json()) == (410, {"valid": False})


@contract("POST", "/v1/contact/{token}/join")
async def _join(client, db, ctx):
    w = await make_world(client, db, contacts=0, arm=False)
    inv = (await client.post("/v1/contacts", headers=w.wearer, json={"name": "Sam"})).json()
    tok = inv["web_link"].rsplit("/", 1)[1]
    assert (await client.post(f"/v1/contact/{tok}/join", json={"name": "x" * 61})).status_code == 422
    r = await client.post(f"/v1/contact/{tok}/join", json={"name": "Sam"})
    assert r.status_code == 200
    shape(r.json(), {"ok": bool, "status": str, "contact_token": str, "message": str})
    assert r.json()["status"] == "pending_approval"
    assert (await client.post(f"/v1/contact/{tok}/join", json={})).status_code == 410       # single use
    assert (await client.post("/v1/contact/unknown/join", json={})).status_code == 410


@contract("GET", "/v1/contact/{token}/status")
async def _contact_status(client, db, ctx):
    w = await make_world(client, db, contacts=1, pending=1)
    assert (await client.get("/v1/contact/unknown/status")).status_code == 404
    tok = w.contacts[0]["token"]
    r = await client.get(f"/v1/contact/{tok}/status")
    assert (r.status_code, r.json()) == (200, {"alert": None, "approved": True})     # nothing before an alert
    assert r.headers["cache-control"] == "no-store"
    assert (await client.get(f"/v1/contact/{w.pending[0]['token']}/status")).json() == {"alert": None,
                                                                                        "approved": False}
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    r = await client.get(f"/v1/contact/{tok}/status")
    shape(r.json(), {"approved": bool, "alert": {"id": int, "kind": str, "title": str, "wearer": str, "level": int,
                                                 "final": bool, "banner": OPT_STR, "status": str,
                                                 "handled_by": OPT_STR, "you_acked": bool,
                                                 "location": (dict, type(None)), "actions": list, "opened_at": str}})
    assert r.json()["alert"]["actions"] == ["ack", "pass"]
    shape(r.json()["alert"]["location"], {"as_of": str, "age_min": int, "link": str, "simulated": bool})


@contract("POST", "/v1/contact/{token}/ack")
async def _ack(client, db, ctx):
    w = await make_world(client, db, contacts=1, pending=1)
    tok = w.contacts[0]["token"]
    assert (await client.post("/v1/contact/unknown/ack", json={})).status_code == 404
    assert (await client.post(f"/v1/contact/{w.pending[0]['token']}/ack", json={})).status_code == 404
    assert (await client.post(f"/v1/contact/{tok}/ack", json={})).status_code == 409             # no alert
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    r = await client.post(f"/v1/contact/{tok}/ack", json={})
    assert (r.status_code, r.json()) == (200, {"ok": True, "result": "acked"})
    again = await client.post(f"/v1/contact/{tok}/ack", json={})
    assert again.json() == {"ok": True, "result": "already_handled"}


@contract("POST", "/v1/contact/{token}/followup")
async def _followup(client, db, ctx):
    w = await make_world(client, db, contacts=2)
    tok = w.contacts[0]["token"]
    assert (await client.post("/v1/contact/unknown/followup", json={"action": "safe"})).status_code == 404
    assert (await client.post(f"/v1/contact/{tok}/followup", json={"action": "safe"})).status_code == 409
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    assert (await client.post(f"/v1/contact/{tok}/followup", json={"action": "hug"})).status_code == 422
    early = await client.post(f"/v1/contact/{tok}/followup", json={"action": "safe"})
    assert early.json() == {"ok": False, "result": "ignored"}           # safe before taking it on: nothing
    await client.post(f"/v1/contact/{tok}/ack", json={})
    r = await client.post(f"/v1/contact/{tok}/followup", json={"action": "safe"})
    assert (r.status_code, r.json()) == (200, {"ok": True, "result": "resolved"})
    w2 = await make_world(client, db, contacts=2)
    await to_state(client, db, ctx, w2, "ESCALATED_L1")
    r = await client.post(f"/v1/contact/{w2.contacts[0]['token']}/followup", json={"action": "pass"})
    assert r.json() == {"ok": True, "result": "passed"}
    await client.post(f"/v1/contact/{w2.contacts[1]['token']}/ack", json={})
    r = await client.post(f"/v1/contact/{w2.contacts[1]['token']}/followup", json={"action": "cant_reach"})
    assert r.json() == {"ok": True, "result": "escalated"}


@contract("GET", "/l/{share_token}")
async def _location(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    assert (await client.get("/l/unknown-token")).status_code == 404
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    link = (await client.get(f"/v1/contact/{w.contacts[0]['token']}/status")).json()["alert"]["location"]["link"]
    path = "/l/" + link.rsplit("/l/", 1)[1]
    r = await client.get(path)
    assert r.status_code == 200
    shape(r.json(), {"lat": NUM, "lon": NUM, "accuracy_m": (int, float, type(None)), "simulated": bool, "as_of": str,
                     "age_min": int, "map": str, "expires_at": OPT_STR})
    assert r.json()["expires_at"] is None                              # open alert: no expiry yet
    await client.post(f"/v1/sessions/{w.session_id}/cancel", headers=w.wearer, json={"pin": PIN})   # false alarm
    gone = await client.get(path)
    assert (gone.status_code, gone.json()) == (410, {"error": "gone", "detail": "This location link has expired."})


# --- operator and demo (X-Admin-Key) -----------------------------------------------------------------
@contract("GET", "/v1/admin/timers")
async def _admin_timers(client, db, ctx):
    await admin_auth(client, "GET", "/v1/admin/timers")
    r = await client.get("/v1/admin/timers", headers=ADMIN, params={"recent": 3})
    assert r.status_code == 200
    shape(r.json(), {"timers": [{"id": int, "session_id": str, "kind": str, "fire_at": str, "in_s": NUM,
                                 "expected_version": (int, type(None)), "attempts": int}],
                     "recent_fired": [{"kind": str, "lag_s": NUM, "result": OPT_STR}]})
    assert (await client.get("/v1/admin/timers", headers=ADMIN, params={"recent": 201})).status_code == 422


@contract("GET", "/v1/admin/outbox")
async def _admin_outbox(client, db, ctx):
    await admin_auth(client, "GET", "/v1/admin/outbox")
    r = await client.get("/v1/admin/outbox", headers=ADMIN, params={"recipient": "wearer", "limit": 5})
    assert r.status_code == 200
    shape(r.json(), {"outbox": [{"id": int, "template": str, "text": str, "status": str, "attempts": int,
                                 "dedupe_key": str, "recipient": str}]})
    assert all(o["recipient"] == "wearer" for o in r.json()["outbox"]) and len(r.json()["outbox"]) <= 5
    for bad in ({"limit": 0}, {"limit": 1001}, {"recipient": "everyone"}):
        assert (await client.get("/v1/admin/outbox", headers=ADMIN, params=bad)).status_code == 422


@contract("GET", "/v1/admin/overview")
async def _admin_overview(client, db, ctx):
    await admin_auth(client, "GET", "/v1/admin/overview")
    r = await client.get("/v1/admin/overview", headers=ADMIN)
    assert r.status_code == 200
    shape(r.json(), {"sessions": [{"id": str, "state": str, "public_state": str, "link_state": str,
                                   "public_link_state": str, "version": int}],
                     "alerts": list, "cover_alerts": list, "outbox": list})


@contract("POST", "/v1/demo/checkin-now")
async def _checkin_now(client, db, ctx):
    await admin_auth(client, "POST", "/v1/demo/checkin-now")
    await demo_reset(client)
    r = await client.post("/v1/demo/checkin-now", headers=ADMIN)
    assert (r.status_code, r.json()) == (200, {"ok": True, "result": "prompted", "state": "PROMPTED"})
    await client.post("/v1/demo/reset", headers=ADMIN, json={"arm": False})
    assert (await client.post("/v1/demo/checkin-now", headers=ADMIN)).status_code == 409    # nothing armed
    await demo_reset(client)


@contract("POST", "/v1/demo/inject")
async def _inject(client, db, ctx):
    await admin_auth(client, "POST", "/v1/demo/inject", json={"kind": "offbody"})
    await demo_reset(client)
    r = await client.post("/v1/demo/inject", headers=ADMIN, json={"kind": "head_position"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "reason": "head_position_change",
                        "label": "head position change → early check-in", "result": "prompted"}
    assert (await client.post("/v1/demo/inject", headers=ADMIN, json={"kind": "spill"})).status_code == 422
    await demo_reset(client)


@contract("POST", "/v1/demo/hr-source")
async def _hr_source(client, db, ctx):
    await admin_auth(client, "POST", "/v1/demo/hr-source", json={"mode": "live"})
    r = await client.post("/v1/demo/hr-source", headers=ADMIN, json={"mode": "sim:steady"})
    assert r.status_code == 200
    shape(r.json(), {"ok": bool, "hr_source": {"mode": str, "since": str}, "simulated": bool})
    assert r.json()["simulated"] is True
    live = await client.post("/v1/demo/hr-source", headers=ADMIN, json={"mode": "live"})
    assert live.json()["simulated"] is False
    assert (await client.post("/v1/demo/hr-source", headers=ADMIN, json={"mode": "sim:x"})).status_code == 422


@contract("POST", "/v1/demo/profile")
async def _profile(client, db, ctx):
    await admin_auth(client, "POST", "/v1/demo/profile", json={"profile": "demo"})
    await demo_reset(client)
    r = await client.post("/v1/demo/profile", headers=ADMIN, json={"profile": "real"})
    assert (r.status_code, r.json()) == (200, {"ok": True, "profile": "real"})
    assert (await client.post("/v1/demo/profile", headers=ADMIN, json={"profile": "x"})).status_code == 422
    await client.post("/v1/demo/profile", headers=ADMIN, json={"profile": "demo"})
    await demo_reset(client)


@contract("POST", "/v1/demo/fast-forward")
async def _fast_forward(client, db, ctx):
    await admin_auth(client, "POST", "/v1/demo/fast-forward")
    await demo_reset(client)
    r = await client.post("/v1/demo/fast-forward", headers=ADMIN)
    assert (r.status_code, r.json()) == (200, {"ok": True, "fired_next": "CHECKIN_DUE",
                                               "label": "fast-forward (demo)"})
    await demo_reset(client)


@contract("POST", "/v1/demo/judge-invite")
async def _judge_invite(client, db, ctx):
    await admin_auth(client, "POST", "/v1/demo/judge-invite")
    r = await client.post("/v1/demo/judge-invite", headers=ADMIN)
    assert r.status_code == 200
    shape(r.json(), {"contact_id": int, "priority": int, "web_link": str, "t_me_link": OPT_STR, "qr_png": str,
                     "expires_at": str})


@contract("POST", "/v1/demo/reset")
async def _reset(client, db, ctx):
    await admin_auth(client, "POST", "/v1/demo/reset", json={})
    r = await client.post("/v1/demo/reset", headers=ADMIN, json={"mode": "hair", "hr_source": "live", "arm": True})
    assert r.status_code == 200
    shape(r.json(), {"ok": bool, "closed_sessions": int, "armed": {"session_id": str, "state_public": str}})
    assert (await client.post("/v1/demo/reset", headers=ADMIN, json={"mode": "ankle"})).status_code == 422


# --- telemetry (Tiger Data) --------------------------------------------------------------------------
@contract("POST", "/v1/telemetry/batch")
async def _batch(client, db, ctx):
    w = await make_world(client, db, contacts=0)
    body = {"batch_id": f"c-{uuid.uuid4().hex[:6]}", "rows": [[81000, 12, -30, 1004, 812, 640], [81020, 15, -28, 1001]]}
    await wearer_auth(client, "POST", "/v1/telemetry/batch", json=body)
    r = await client.post("/v1/telemetry/batch", headers=w.device(), json=body)
    assert (r.status_code, r.json()) == (200, {"ok": True, "duplicate": False, "inserted": 2, "dropped": 0})
    again = await client.post("/v1/telemetry/batch", headers=w.device(), json=body)
    assert again.json() == {"ok": True, "duplicate": True, "inserted": 2, "dropped": 0}
    assert (await client.post("/v1/telemetry/batch", headers=w.wearer, json={**body, "batch_id": "w1"})).status_code == 200
    assert (await client.post("/v1/telemetry/batch", headers=w.wearer,
                              json={**body, "device_id": "not-mine"})).status_code == 403
    assert (await client.post("/v1/telemetry/batch", headers=w.wearer,
                              json={**body, "user_id": str(uuid.uuid4())})).status_code == 403
    for bad in ({"rows": []}, {"rows": [[1, 2]]}, {"rows": [[1, 40000, 0, 0]]}, {"rows": [[1, 0, 0, 0]], "clock": "x"}):
        assert (await client.post("/v1/telemetry/batch", headers=w.device(), json=bad)).status_code == 422, bad


async def _reader_auth(client, path: str, w) -> None:
    await wearer_auth(client, "GET", path)
    assert (await client.get(path, headers={"X-Admin-Key": "wrong"})).status_code == 401
    r = await client.get(path, headers=w.wearer, params={"user_id": str(uuid.uuid4())})
    assert r.status_code == 403                                          # a wearer reads only her own data


@contract("GET", "/v1/telemetry/zscore")
async def _zscore(client, db, ctx):
    w = await make_world(client, db, contacts=0)
    await _reader_auth(client, "/v1/telemetry/zscore", w)
    r = await client.get("/v1/telemetry/zscore", headers=w.wearer)
    assert r.status_code == 200
    shape(r.json(), {"mode": str, "method": str, "window": str, "note": str, "rows": list})
    assert (await client.get("/v1/telemetry/zscore", headers=ADMIN)).status_code == 200


@contract("GET", "/v1/telemetry/aggregates")
async def _aggregates(client, db, ctx):
    w = await make_world(client, db, contacts=0)
    await _reader_auth(client, "/v1/telemetry/aggregates", w)
    await client.post("/v1/hr", headers=w.wearer, json={"bpm": 70, "source": "contract"})
    r = await client.get("/v1/telemetry/aggregates", headers=w.wearer, params={"series": "hr", "resolution": "1m",
                                                                                "hours": 1})
    assert r.status_code == 200
    shape(r.json(), {"mode": str, "series": str, "resolution": str, "user_id": str,
                     "buckets": [{"bucket": str, "n": int, "mean": NUM, "simulated": bool}]})
    assert r.json()["buckets"]
    for bad in ({"hours": 49}, {"series": "x"}, {"resolution": "1w"}, {"hours": 0}):
        assert (await client.get("/v1/telemetry/aggregates", headers=w.wearer, params=bad)).status_code == 422, bad


@contract("GET", "/v1/telemetry/gaps")
async def _gaps(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await _reader_auth(client, "/v1/telemetry/gaps", w)
    r = await client.get("/v1/telemetry/gaps", headers=w.wearer, params={"hours": 1})
    assert r.status_code == 200
    shape(r.json(), {"user_id": str, "label": str, "method": str, "liveness_s": NUM, "dead_ranges": list,
                     "num_gaps": int, "downtime_s": NUM, "note": str})
    contact = {"Authorization": f"Bearer {w.contacts[0]['token']}"}
    assert (await client.get("/v1/telemetry/gaps", headers=contact)).status_code == 401
    assert (await client.get("/v1/telemetry/gaps", headers=w.wearer, params={"hours": 169})).status_code == 422


@contract("GET", "/v1/telemetry/info")
async def _info(client, db, ctx):
    await admin_auth(client, "GET", "/v1/telemetry/info")
    r = await client.get("/v1/telemetry/info", headers=ADMIN)
    assert r.status_code == 200
    shape(r.json(), {"mode": str, "server_version": str, "retention": dict,
                     "connections": {"pool_max": int, "total_max": int, "server_max_connections": int}})


@contract("POST", "/v1/telemetry/refresh")
async def _refresh(client, db, ctx):
    await admin_auth(client, "POST", "/v1/telemetry/refresh", json={"hours": 1})
    r = await client.post("/v1/telemetry/refresh", headers=ADMIN, json={"hours": 1})
    assert r.status_code == 200
    shape(r.json(), {"ok": bool, "mode": str, "refreshed": list, "from": str, "to": str})
    assert (await client.post("/v1/telemetry/refresh", headers=ADMIN, json={"hours": 48})).status_code == 422


@contract("POST", "/v1/telemetry/compression-demo")
async def _compression(client, db, ctx):
    await admin_auth(client, "POST", "/v1/telemetry/compression-demo", json={})
    assert (await client.post("/v1/telemetry/compression-demo", headers=ADMIN,
                              json={"user_id": str(uuid.uuid4())})).status_code == 404
    assert (await client.post("/v1/telemetry/compression-demo", headers=ADMIN, json={"days": 7})).status_code == 422
    async with db.connection() as conn:
        plain = (await (await conn.execute(
            "SELECT NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') AS p")).fetchone())["p"]
    if plain:  # on TimescaleDB this writes days of rows; test_timeseries_tsdb.py covers that path
        r = await client.post("/v1/telemetry/compression-demo", headers=ADMIN, json={})
        assert r.status_code == 200 and r.json()["available"] is False and r.json()["mode"] == "plain"


@contract("POST", "/v1/telemetry/synthetic")
async def _synthetic(client, db, ctx):
    w = await make_world(client, db, contacts=0)
    await admin_auth(client, "POST", "/v1/telemetry/synthetic", json={})
    assert (await client.post("/v1/telemetry/synthetic", headers=ADMIN,
                              json={"user_id": str(uuid.uuid4())})).status_code == 404
    assert (await client.post("/v1/telemetry/synthetic", headers=ADMIN, json={"days": 8})).status_code == 422
    r = await client.post("/v1/telemetry/synthetic", headers=ADMIN, json={
        "user_id": w.user_id, "days": 0.05, "hr_every_s": 60, "motion_every_s": 60})
    assert r.status_code == 200
    shape(r.json(), {"ok": bool, "user_id": str, "from": str, "to": str, "profile": str, "hr_rows": int,
                     "motion_rows": int, "simulated": bool, "refreshed": list})
    assert r.json()["simulated"] is True and r.json()["hr_rows"] > 0


# --- senior mode (synthetic) -------------------------------------------------------------------------
@contract("POST", "/v1/demo/care-seed")
async def _care_seed(client, db, ctx):
    await admin_auth(client, "POST", "/v1/demo/care-seed", json={})
    assert (await client.post("/v1/demo/care-seed", headers=ADMIN, json={"seed": -1})).status_code == 422
    r = await client.post("/v1/demo/care-seed", headers=ADMIN, json={})
    assert r.status_code == 200
    shape(r.json(), {"ok": bool, "synthetic": bool, "seed": int, "as_of": str, "residents": int, "days": int,
                     "rows": int, "levels": dict, "notice": str})
    assert r.json()["residents"] == 20 and r.json()["synthetic"] is True


@contract("GET", "/v1/care/residents")
async def _care_residents(client, db, ctx):
    r = await client.get("/v1/care/residents")                         # no key: every row is synthetic
    assert r.status_code == 200
    shape(r.json(), {"synthetic": bool, "notice": str, "residents": [{"resident_id": str, "display_name": str,
                                                                      "synthetic": bool, "score": int, "level": str}]})


@contract("GET", "/v1/care/alerts")
async def _care_alerts(client, db, ctx):
    await client.post("/v1/demo/care-seed", headers=ADMIN, json={})
    r = await client.get("/v1/care/alerts")
    assert r.status_code == 200
    card = {"resident_id": str, "display_name": str, "synthetic": bool, "score": int, "level": str, "rules": list,
            "suggested_check": str, "briefing": str, "briefing_source": str, "pattern_note": str, "delivery": dict,
            "data_quality": dict, "query_ms": NUM}
    shape(r.json(), {"alerts": [card]})
    assert r.json()["alerts"] and all(c["briefing_source"] == "template" for c in r.json()["alerts"])
    everyone = await client.get("/v1/care/alerts", params={"include_low": "true"})
    assert len(everyone.json()["alerts"]) == 20
    assert (await client.get("/v1/care/alerts", params={"include_low": "maybe"})).status_code == 422


@contract("POST", "/v1/care/alerts/{resident_id}/feedback")
async def _care_feedback_post(client, db, ctx):
    await client.post("/v1/demo/care-seed", headers=ADMIN, json={})
    rid = (await client.get("/v1/care/alerts")).json()["alerts"][0]["resident_id"]
    r = await client.post(f"/v1/care/alerts/{rid}/feedback", json={"action": "useful"})
    assert r.status_code == 200
    shape(r.json(), {"ok": bool, "synthetic": bool, "resident_id": str, "action": str, "as_of": str})
    assert (await client.post("/v1/care/alerts/R-99/feedback", json={"action": "useful"})).status_code == 404
    assert (await client.post(f"/v1/care/alerts/{rid}/feedback", json={"action": "meh"})).status_code == 422
    assert (await client.post(f"/v1/care/alerts/{rid}/feedback",
                              json={"action": "already_known", "reason": "x" * 201})).status_code == 422


@contract("GET", "/v1/care/feedback")
async def _care_feedback_get(client, db, ctx):
    r = await client.get("/v1/care/feedback")
    assert r.status_code == 200
    shape(r.json(), {"synthetic": bool, "notice": str, "counts": {"useful": int, "not_useful": int,
                                                                  "already_known": int, "snooze_3d": int}})


@contract("GET", "/v1/care/digest")
async def _care_digest(client, db, ctx):
    r = await client.get("/v1/care/digest")
    assert r.status_code == 200
    shape(r.json(), {"synthetic": bool, "budget": dict, "items": list})


# --- FastAPI's own schema pages ---------------------------------------------------------------------
@contract("GET", "/openapi.json")
async def _openapi(client, db, ctx):
    r = await client.get("/openapi.json")
    assert r.status_code == 200 and r.json()["info"]["title"] == "StillHere"
    documented = {(m.upper(), p) for p, ops in r.json()["paths"].items() for m in ops}
    assert documented == {k for k in served_routes() if k[1] not in ("/openapi.json", "/docs", "/redoc",
                                                                      "/docs/oauth2-redirect")}


@contract("GET", "/docs")
async def _docs(client, db, ctx):
    r = await client.get("/docs")
    assert r.status_code == 200 and "swagger" in r.text.lower()


@contract("GET", "/docs/oauth2-redirect")
async def _docs_redirect(client, db, ctx):
    assert (await client.get("/docs/oauth2-redirect")).status_code == 200


@contract("GET", "/redoc")
async def _redoc(client, db, ctx):
    r = await client.get("/redoc")
    assert r.status_code == 200 and "redoc" in r.text.lower()


# --- the parametrized runner -------------------------------------------------------------------------
@pytest.mark.parametrize("route", sorted(CONTRACTS), ids=lambda k: f"{k[0]} {k[1]}")
async def test_contract(client, db, ctx, route):
    await CONTRACTS[route](client, db, ctx)


async def test_a_session_that_is_escalating_still_answers_the_documented_shapes(client, db, ctx):
    """The device and wearer views keep their exact shapes during an alert and at FINAL."""
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    await fire(db, ctx, w.session_id, states.ACK_TIMEOUT)            # one contact: L1 -> FINAL
    hb = await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    assert hb.json()["state_public"] == states.FINAL and hb.json()["cmd"]["led"] == "red"
    status = (await client.get(f"/v1/contact/{w.contacts[0]['token']}/status")).json()["alert"]
    assert status["final"] is True and status["banner"] == states.FINAL_BANNER
    assert status["banner"].startswith("Emergency services: SIMULATED")
