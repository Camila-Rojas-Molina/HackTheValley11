"""RECORD-mode R lines reach the scrunchie_telemetry hypertable (BUILD_PLAN §5.8, §9 "debug telemetry").

The heartbeat reply says whether the session wants debug telemetry: "debug" in the demo profile, "off"
in the real profile (heartbeats and events only) and with no session. bridge.py forwards R lines to
POST /v1/telemetry/batch only while it is "debug" (or when started with --telemetry on).
"""
from __future__ import annotations

import json

import httpx
import pytest

from app import states
from scripts import bridge as br
from tests.conftest import ADMIN_KEY, DURESS_PIN, PIN, make_world, session_row


class TelServer:
    """Fake StillHere: heartbeat replies carry `telemetry`; telemetry batches are recorded."""

    def __init__(self, telemetry: str = "debug"):
        self.telemetry = telemetry
        self.up = True
        self.status = 200
        self.batches: list[dict] = []
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("down", request=request)
        body = json.loads(request.content or b"{}")
        self.calls.append(request.url.path)
        cmd = {"led": "off", "blink_n": 0, "challenge_id": None}
        if request.url.path == "/v1/device/heartbeat":
            return httpx.Response(200, json={"state_public": "ARMED", "server_time": "t", "cmd": cmd,
                                             "telemetry": self.telemetry})
        if request.url.path == "/v1/telemetry/batch":
            if self.status != 200:
                return httpx.Response(self.status, json={"detail": "nope"})
            self.batches.append({"body": body, "headers": dict(request.headers)})
            return httpx.Response(200, json={"ok": True, "duplicate": False, "inserted": len(body["rows"]),
                                             "dropped": 0})
        return httpx.Response(200, json={"ok": True, "result": "noted", "state_public": "ARMED", "cmd": cmd})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _bridge(srv: TelServer, telemetry: str = "auto"):
    clock, out = Clock(), []
    http = httpx.Client(base_url="http://bridge.test", transport=httpx.MockTransport(srv.handler))
    b = br.Bridge(http=http, device_id="scrunchie-1", device_key="k", write_line=out.append, clock=clock,
                  telemetry=telemetry)
    return b, clock, http


def _r_lines(b, start_ms: int, n: int) -> None:
    for i in range(n):
        b.handle_line(f"R,{start_ms + 20 * i},{i},{-i},1000,300,{310 + i}")


def test_r_lines_parse_to_rows():
    assert br.parse_line("R,81900,12,-7,998,301,305", 5.0) == ("R", [81900.0, 12.0, -7.0, 998.0, 301.0, 305.0])
    assert br.parse_line("R,81900,12,-7,998", 5.0) == ("R", [81900.0, 12.0, -7.0, 998.0])   # no FSR channels
    for bad in ("R,1,2,3", "R,x,1,2,3,4,5", "R,1,2,3,nan,4,5", "R,1,2,3,4,5,6,7"):
        assert br.parse_line(bad, 5.0)[0] is None, bad


def test_r_lines_are_forwarded_in_batches_while_the_server_wants_debug_telemetry():
    srv = TelServer("debug")
    b, clock, http = _bridge(srv)
    b.handle_line("H,b7,1,10,0,400,0")
    b.tick()                                       # the heartbeat reply turns forwarding on
    _r_lines(b, 50_000, 40)
    b.tick()
    assert srv.batches == []                       # under a second of rows: wait for more
    clock.t += 1.0
    b.tick()
    (batch,) = srv.batches
    body, headers = batch["body"], batch["headers"]
    assert body["clock"] == "device_ms" and body["profile"] == "night_out" and body["simulated"] is False
    assert body["rows"][0] == [50_000.0, 0.0, 0.0, 1000.0, 300.0, 310.0] and len(body["rows"]) == 40
    assert body["batch_id"] == "b7:50000:40"
    assert headers["authorization"] == "Bearer k" and headers["x-device-id"] == "scrunchie-1"
    assert b.stats["R"] == 40 and b.stats["tel_rows_ok"] == 40 and b._tel_rows == []
    http.close()


def test_r_lines_are_only_counted_when_the_server_says_off():
    srv = TelServer("off")
    b, clock, http = _bridge(srv)
    b.handle_line("H,b7,1,10,0,400,0")
    b.tick()
    _r_lines(b, 0, 600)
    clock.t += 2.0
    b.tick()
    assert srv.batches == [] and b.stats["R"] == 600 and b._tel_rows == []
    # The operator switches the session to the demo profile: forwarding starts with the next reply.
    srv.telemetry = "debug"
    clock.t += 2.0
    b.handle_line("H,b7,2,10,0,400,0")
    b.tick()
    _r_lines(b, 30_000, 10)
    clock.t += 1.0
    b.tick()
    assert len(srv.batches) == 1 and len(srv.batches[0]["body"]["rows"]) == 10
    http.close()


def test_telemetry_flag_on_and_off_override_the_server():
    on = TelServer("off")
    b, clock, http = _bridge(on, telemetry="on")
    b.handle_line("H,b7,1,10,0,400,0")
    _r_lines(b, 0, 5)
    clock.t += 1.0
    b.tick()
    assert len(on.batches) == 1
    http.close()
    off = TelServer("debug")
    b, clock, http = _bridge(off, telemetry="off")
    b.handle_line("H,b7,1,10,0,400,0")
    b.tick()
    _r_lines(b, 0, 5)
    clock.t += 1.0
    b.tick()
    assert off.batches == []
    http.close()


def test_a_full_batch_goes_out_at_once_and_an_outage_keeps_at_most_two_minutes():
    srv = TelServer("debug")
    b, clock, http = _bridge(srv)
    b.handle_line("H,b7,1,10,0,400,0")
    b.tick()
    _r_lines(b, 0, br.TEL_BATCH_ROWS)
    b.tick()
    assert len(srv.batches) == 1 and len(srv.batches[0]["body"]["rows"]) == br.TEL_BATCH_ROWS
    srv.up = False
    _r_lines(b, 100_000, br.TEL_MAX_BUFFER + 300)
    for _ in range(3):
        clock.t += 1.0
        b.tick()
    assert len(b._tel_rows) == br.TEL_MAX_BUFFER                  # the oldest rows were dropped
    assert b._tel_rows[0][0] == 100_000 + 20 * 300
    assert b.stats["tel_rows_dropped"] == 300
    srv.up = True
    for _ in range(40):
        clock.t += 1.0
        b.tick()
    sent = sum(len(x["body"]["rows"]) for x in srv.batches[1:])
    assert sent == br.TEL_MAX_BUFFER and b._tel_rows == []
    # A retried batch keeps its batch_id (the server stores it once).
    ids = [x["body"]["batch_id"] for x in srv.batches]
    assert len(ids) == len(set(ids))
    http.close()


def test_a_rejected_batch_is_dropped_not_retried_forever():
    srv = TelServer("debug")
    b, clock, http = _bridge(srv)
    b.handle_line("H,b7,1,10,0,400,0")
    b.tick()
    srv.status = 422
    _r_lines(b, 0, 10)
    clock.t += 1.0
    b.tick()
    assert b._tel_rows == [] and b.stats["tel_rows_dropped"] == 10
    http.close()


def test_events_still_go_first_and_never_wait_behind_telemetry():
    srv = TelServer("debug")
    b, clock, http = _bridge(srv)
    b.handle_line("H,b7,1,10,0,400,0")
    b.tick()
    _r_lines(b, 0, 20)
    b.handle_line('E,b7:2,squeeze,{"pattern":"plain","k":2}')
    assert srv.calls[-1] == "/v1/device/events"                  # sent at once, before any batch
    # While an event waits for the server, telemetry holds back (a slow 503 must not delay the event).
    srv.up = False
    b.handle_line('E,b7:3,squeeze,{"pattern":"duress","k":3}')
    _r_lines(b, 1_000, 20)
    srv.up = True
    clock.t += 0.5                                               # before the event's retry: nothing goes out
    n = len(srv.calls)
    b.tick()
    assert len(srv.calls) == n and len(b.events) == 1 and len(b._tel_rows) == 40
    clock.t += 1.0                                               # the event first, then the rows
    b.tick()
    assert srv.calls[n:] == ["/v1/device/events", "/v1/telemetry/batch"] and b.events == []
    http.close()


# --- server side -------------------------------------------------------------------------------------
async def test_heartbeat_reply_says_debug_telemetry_only_in_the_demo_profile(client, db, ctx):
    w = await make_world(client, db, contacts=1, arm=False)
    hb = (await client.post("/v1/device/heartbeat", headers=w.device(), json={})).json()
    assert hb["telemetry"] == "off"                               # no session
    r = await client.post("/v1/sessions", json={"profile": "demo"}, headers=w.wearer)
    w.session_id = r.json()["session_id"]
    assert (await client.post("/v1/device/heartbeat", headers=w.device(), json={})).json()["telemetry"] == "debug"
    await client.post(f"/v1/sessions/{w.session_id}/end", json={"pin": PIN}, headers=w.wearer)
    r = await client.post("/v1/sessions", json={"profile": "real"}, headers=w.wearer)
    w.session_id = r.json()["session_id"]
    assert (await client.post("/v1/device/heartbeat", headers=w.device(), json={})).json()["telemetry"] == "off"


async def test_demo_profile_switch_turns_debug_telemetry_on_and_off(client, db, ctx):
    h = {"X-Admin-Key": ADMIN_KEY}
    await client.post("/v1/demo/reset", headers=h, json={})
    dev = {"Authorization": "Bearer demo-device-key", "X-Device-Id": ctx.settings.demo_device_id,
           "X-Boot-Id": "tel", "X-Seq": "1"}
    assert (await client.post("/v1/device/heartbeat", headers=dev, json={})).json()["telemetry"] == "debug"
    assert (await client.post("/v1/demo/profile", headers=h, json={"profile": "real"})).status_code == 200
    dev["X-Seq"] = "2"
    assert (await client.post("/v1/device/heartbeat", headers=dev, json={})).json()["telemetry"] == "off"
    await client.post("/v1/demo/profile", headers=h, json={"profile": "demo"})
    dev["X-Seq"] = "3"
    assert (await client.post("/v1/device/heartbeat", headers=dev, json={})).json()["telemetry"] == "debug"


async def test_telemetry_flag_never_tells_a_duress_end_from_a_real_end(client, db, ctx):
    e, f = await make_world(client, db), await make_world(client, db)
    await client.post(f"/v1/sessions/{e.session_id}/end", json={"pin": PIN}, headers=e.wearer)
    await client.post(f"/v1/sessions/{f.session_id}/end", json={"pin": DURESS_PIN}, headers=f.wearer)
    assert (await session_row(db, f.session_id))["state"] == "ESCALATED_L1"     # masked, still escalating
    hb_e = (await client.post("/v1/device/heartbeat", headers=e.device(), json={})).json()
    hb_f = (await client.post("/v1/device/heartbeat", headers=f.device(), json={})).json()
    hb_e.pop("server_time"), hb_f.pop("server_time")
    assert hb_e == hb_f == {"state_public": states.ENDED, "cmd": {"led": "off", "blink_n": 0, "challenge_id": None},
                            "telemetry": "off"}


async def test_forwarded_rows_land_in_the_hypertable_for_the_devices_wearer(client, db, ctx):
    """What the bridge posts, end to end through the API (the bridge itself is covered above and by
    tests/test_ops_live.py against a real server)."""
    w = await make_world(client, db, contacts=0)
    rows = [[60_000 + 20 * i, i, -i, 1000, 300, 310] for i in range(50)]
    r = await client.post("/v1/telemetry/batch", headers=w.device(),
                          json={"rows": rows, "clock": "device_ms", "profile": "night_out", "simulated": False,
                                "batch_id": "b1:60000:50"})
    assert r.status_code == 200 and r.json()["inserted"] == 50, r.text
    again = await client.post("/v1/telemetry/batch", headers=w.device(),
                              json={"rows": rows, "clock": "device_ms", "batch_id": "b1:60000:50"})
    assert again.json()["duplicate"] is True
    async with db.connection() as conn:
        got = await (await conn.execute(
            """SELECT count(*) AS n, min(device_id) AS dev, bool_or(simulated) AS sim, min(profile) AS p
                 FROM scrunchie_telemetry WHERE user_id = %s""", (w.user_id,))).fetchone()
    assert (got["n"], got["dev"], got["sim"], got["p"]) == (50, w.device_id, False, "night_out")


@pytest.mark.parametrize("value", ["debug", "off"])
def test_heartbeat_reply_value_is_documented(value):
    from pathlib import Path
    api = (Path(__file__).resolve().parents[2] / "docs" / "API.md").read_text(encoding="utf-8")
    assert f'"telemetry":"{value}"' in api.replace(" ", "")
