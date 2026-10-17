"""scripts/bridge.py: serial line protocol <-> HTTP, without a server or a device (mock transport)."""
from __future__ import annotations

import io
import json
import queue
import threading

import httpx
import pytest

from scripts import bridge as br


class FakeServer:
    def __init__(self):
        self.up = True
        self.calls: list[tuple[str, dict, dict]] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("server down", request=request)
        body = json.loads(request.content or b"{}")
        self.calls.append((request.url.path, dict(request.headers), body))
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "nope"})
        cmd = {"led": "amber", "blink_n": 3, "challenge_id": "ab12cd"}
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"state_public": "PROMPTED", "server_time": "t", "cmd": cmd})
        return httpx.Response(200, json={"ok": True, "result": "noted", "state_public": "ARMED",
                                         "cmd": {"led": "off", "blink_n": 0, "challenge_id": None}})

    def paths(self, suffix: str) -> list:
        return [c for c in self.calls if c[0].endswith(suffix)]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def setup():
    srv, clock, out = FakeServer(), Clock(), []
    http = httpx.Client(base_url="http://bridge.test", transport=httpx.MockTransport(srv.handler))
    b = br.Bridge(http=http, device_id="scrunchie-1", device_key="k", write_line=out.append, heartbeat_s=2.0,
                  stale_s=3.0, clock=clock)
    yield b, srv, clock, out
    http.close()


def test_parse_lines():
    kind, h = br.parse_line("H,b7,42,12.5,8.0,410,0", 5.0)
    assert kind == "H" and (h.boot_id, h.seq, h.enmo60_mg, h.offbody) == ("b7", 42, 12.5, False)
    kind, e = br.parse_line('E,b7:43,squeeze,{"pattern":"duress","k":3,"device_ms":1234}', 5.0)
    assert kind == "E" and e.event_id == "b7:43" and e.duress and e.device_ms == 1234 and "device_ms" not in e.payload
    assert br.parse_line("E,b7:44,head_position_change", 5.0)[1].payload == {}
    assert br.parse_line("R,1,2,3,4,5,6", 5.0) == ("R", None)
    for bad in ("H,b7,x,1,2,3,0", "H,b7,1,2", "E,b7-1,squeeze,{}", "E,b7:1,dance,{}", "E,b7:1,squeeze,[1]", "Z,1"):
        assert br.parse_line(bad, 5.0)[0] is None, bad
    assert br.c_line({"led": "amber", "blink_n": 3, "challenge_id": "ab"}) == "C,amber,3,ab"
    assert br.c_line({"led": "off", "blink_n": 0, "challenge_id": None}) == "C,off,0,"


def test_heartbeats_only_while_device_lines_arrive(setup):
    b, srv, clock, out = setup
    b.tick()
    assert srv.paths("/heartbeat") == []                    # nothing from the device yet
    b.handle_line("H,b7,1,10,0,400,0")
    b.tick()
    path, headers, body = srv.paths("/heartbeat")[0]
    assert headers["authorization"] == "Bearer k" and headers["x-device-id"] == "scrunchie-1"
    assert headers["x-boot-id"] == "b7" and headers["x-seq"] == "1"
    assert body == {"enmo60_mg": 10.0, "offbody": False}
    assert out == ["C,amber,3,ab12cd"]                       # every heartbeat reply goes back as a C line
    clock.t += 1.0
    b.handle_line("H,b7,2,10,0,400,0")
    b.tick()
    assert len(srv.paths("/heartbeat")) == 1                 # 2 s interval
    clock.t += 1.1
    b.tick()
    assert len(srv.paths("/heartbeat")) == 2 and srv.paths("/heartbeat")[1][1]["x-seq"] == "2"
    clock.t += 3.5                                           # device unplugged: H lines stop
    b.tick()
    clock.t += 2.0
    b.tick()
    assert len(srv.paths("/heartbeat")) == 2                 # the server's lease will run out


def test_events_go_out_at_once_with_the_reply_as_a_c_line(setup):
    b, srv, clock, out = setup
    b.handle_line('E,b7:5,squeeze,{"pattern":"plain","k":2}')
    path, headers, body = srv.paths("/events")[0]
    assert body == {"event_id": "b7:5", "type": "squeeze", "payload": {"pattern": "plain", "k": 2}}
    assert headers["x-boot-id"] == "b7" and headers["x-seq"] == "5"
    assert out == ["C,off,0,"] and b.events == []


def test_buffers_while_down_then_flushes_duress_first_marked_late(setup):
    b, srv, clock, out = setup
    srv.up = False
    b.handle_line('E,b7:10,squeeze,{"pattern":"plain","k":2}')
    clock.t += 0.5
    b.handle_line('E,b7:11,head_position_change,{}')
    clock.t += 0.5
    b.handle_line('E,b7:12,squeeze,{"pattern":"duress","k":3}')
    assert [e.event_id for e in b.events] == ["b7:10", "b7:11", "b7:12"]
    # Retries back off 1, 2, 4, 8, 8 s; nothing is lost while the server is down.
    waits = []
    for _ in range(5):
        ev = b._order()[0]
        waits.append(ev.next_try - clock.t)
        clock.t = ev.next_try
        b.tick()
    assert [round(w, 1) for w in waits] == [1.0, 2.0, 4.0, 8.0, 8.0]
    assert len(b.events) == 3
    srv.up = True
    clock.t += 8.0
    b.tick()
    sent = srv.paths("/events")
    assert [c[2]["event_id"] for c in sent] == ["b7:12", "b7:10", "b7:11"]   # duress first, then oldest
    assert all(c[2]["payload"]["buffered"] is True for c in sent)           # the server treats them as late
    assert b.events == [] and out[-1] == "C,off,0,"


def test_permanent_rejection_is_dropped_not_retried(setup):
    b, srv, clock, out = setup
    srv.status = 401
    b.handle_line('E,b7:20,squeeze,{"pattern":"plain","k":2}')
    assert b.events == [] and b.stats["ev_dropped"] == 1


def test_full_buffer_keeps_duress(setup):
    b, srv, clock, out = setup
    srv.up = False
    b.max_buffer = 3
    b.handle_line('E,b7:1,squeeze,{"pattern":"duress","k":3}')
    for i in range(2, 6):
        clock.t += 1
        b.handle_line(f"E,b7:{i},head_position_change,{{}}")
    assert len(b.events) == 3 and any(e.duress for e in b.events)


def test_stdin_mode_end_to_end():
    srv = FakeServer()
    inp = io.StringIO("H,b9,1,15,0,400,0\nE,b9:2,squeeze,{\"pattern\":\"plain\",\"k\":2}\nR,1,2,3,4,5,6\nnonsense\n")
    out = io.StringIO()
    link = br.StdinLink(inp, out)
    with httpx.Client(base_url="http://bridge.test", transport=httpx.MockTransport(srv.handler)) as http:
        b = br.Bridge(http=http, device_id="scrunchie-1", device_key="k", write_line=link.write_line)
        br.run(b, link, tick_s=0.01)
    assert len(srv.paths("/heartbeat")) == 1 and len(srv.paths("/events")) == 1
    assert out.getvalue().splitlines() == ["C,amber,3,ab12cd", "C,off,0,"]
    assert b.stats["R"] == 1 and b.stats["bad"] == 1


def test_serial_link_reads_lines_and_writes_c_lines():
    pytest.importorskip("serial")
    link = br.SerialLink("loop://")                 # pyserial loopback: what we write comes back
    q: queue.Queue = queue.Queue()
    stop = threading.Event()
    t = threading.Thread(target=link.reader, args=(q, stop), daemon=True)
    t.start()
    try:
        for _ in range(50):
            if link._ser is not None:
                break
            stop.wait(0.05)
        link.write_line("C,red,0,")
        assert q.get(timeout=2) == "C,red,0,"
    finally:
        stop.set()
        t.join(timeout=2)
        link.close()


@pytest.mark.parametrize("line", ['E,b\x007:43,squeeze,{"pattern":"plain","k":2}',
                                  'E,b7:4\x013,squeeze,{}', "H,b\x007,42,12.5,8.0,410,0"])
def test_lines_with_control_characters_in_their_ids_are_dropped(line):
    kind, reason = br.parse_line(line, 5.0)
    assert kind is None and ("boot_id" in reason or "event_id" in reason)


def test_a_dropped_noisy_line_never_holds_up_the_next_event(setup):
    b, srv, clock, out = setup
    b.handle_line('E,b\x007:1,squeeze,{"pattern":"plain","k":2}')
    b.handle_line('E,b7:2,squeeze,{"pattern":"plain","k":2}')
    assert [c[2]["event_id"] for c in srv.paths("/events")] == ["b7:2"] and b.events == []


def test_a_squeeze_carries_the_challenge_the_device_showed_when_she_made_it():
    """A squeeze held back while the server is down must not answer a prompt issued meanwhile: the
    bridge stamps it with the prompt the device was showing (none here), and the server only lets a
    held-back squeeze answer the prompt it was stamped with."""
    state = {"up": True, "cmd": {"led": "off", "blink_n": 0, "challenge_id": None}}
    sent = []

    def handler(request):
        if not state["up"]:
            raise httpx.ConnectError("down", request=request)
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("/events"):
            sent.append(body)
            return httpx.Response(200, json={"ok": True, "result": "ignored", "state_public": "PROMPTED",
                                             "cmd": state["cmd"]})
        return httpx.Response(200, json={"state_public": "x", "server_time": "t", "cmd": state["cmd"]})

    clock, out = Clock(), []
    http = httpx.Client(base_url="http://bridge.test", transport=httpx.MockTransport(handler))
    b = br.Bridge(http=http, device_id="d", device_key="k", write_line=out.append, clock=clock)
    b.handle_line("H,b7,1,5,0,0,0")
    b.tick()                                                  # the device shows no prompt
    state["up"] = False
    b.handle_line('E,b7:20,squeeze,{"pattern":"plain","k":2}')
    state.update(up=True, cmd={"led": "amber", "blink_n": 0, "challenge_id": "new123"})   # a prompt meanwhile
    clock.t += 1.0
    b.handle_line("H,b7,2,5,0,0,0")
    b.tick()
    assert out[-1] == "C,amber,0,new123" and sent and sent[0]["payload"]["buffered"] is True
    assert "challenge_id" not in sent[0]["payload"]           # made before any prompt: none
    # A live squeeze now carries the prompt the device is showing.
    b.handle_line('E,b7:21,squeeze,{"pattern":"plain","k":2}')
    assert sent[-1]["payload"] == {"pattern": "plain", "k": 2, "challenge_id": "new123"}
    http.close()
