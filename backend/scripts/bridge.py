"""USB-serial bridge: scrunchie line protocol <-> StillHere HTTP API (BUILD_PLAN §5.8, §5.9).

device -> bridge (115200 baud, one line per message)
  H,<boot_id>,<seq>,<enmo60_mg>,<tilt_deg>,<fsr_base_mv>,<offbody>    every 1 s
  E,<boot_id>:<seq>,<type>,<json_payload>                             immediately
  R,...                                                               50 Hz RECORD mode: counted, not forwarded
bridge -> device
  C,<led>,<blink_n>,<challenge_id>                                    after every heartbeat / event reply

HTTP
  POST /v1/device/heartbeat  every --heartbeat-s (2 s demo, 15 s real), only while H lines keep
                             arriving: unplug the device and the server's lease runs out (LOST_CONTACT).
  POST /v1/device/events     immediately; event_id = boot_id:seq. Retried after 1, 2, 4, 8 s (then every
                             8 s) with the same id. While the server is unreachable, events wait in memory
                             and are flushed duress first, then oldest first, marked buffered=true so the
                             server treats them as late (a late squeeze never stands an alert down).
  Headers: Authorization: Bearer <device key>, X-Device-Id, X-Boot-Id, X-Seq.

Usage (from backend/):
  python scripts/bridge.py --port /dev/cu.usbserial-0001     # real device; needs pyserial
  python scripts/bridge.py --stdin                           # type or pipe protocol lines (no hardware)
Env: STILLHERE_URL (default http://localhost:8000), DEVICE_KEY, DEMO_DEVICE_ID (default scrunchie-1).
The serial port is reopened automatically when the device is unplugged and plugged back in.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx

try:  # optional: only needed for a real serial port
    import serial  # type: ignore
except ImportError:  # pragma: no cover - depends on the environment
    serial = None

log = logging.getLogger("stillhere.bridge")

RETRY_STEPS_S = (1.0, 2.0, 4.0, 8.0)
EVENT_TYPES = {"squeeze", "head_position_change", "offbody", "prompt_shown", "fall_like"}
PERMANENT_HTTP = {400, 401, 403, 404, 409, 413, 422}


@dataclass
class Heartbeat:
    boot_id: str
    seq: int
    enmo60_mg: float
    tilt_deg: Optional[float]
    fsr_base_mv: Optional[float]
    offbody: bool
    at: float


@dataclass
class PendingEvent:
    event_id: str
    boot_id: str
    seq: int
    type: str
    payload: dict
    queued_at: float
    device_ms: Optional[int] = None
    attempts: int = 0
    next_try: float = 0.0
    buffered: bool = False

    @property
    def duress(self) -> bool:
        return self.type == "squeeze" and (self.payload or {}).get("pattern") == "duress"


def _num(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_line(line: str, now: float):
    """('H', Heartbeat) | ('E', PendingEvent) | ('R', None) | (None, reason)."""
    line = line.strip()
    if not line:
        return None, "empty"
    kind = line[0]
    if kind == "H":
        parts = line.split(",")
        if len(parts) != 7:
            return None, "H needs 7 fields"
        try:
            seq = int(parts[2])
        except ValueError:
            return None, "bad seq"
        enmo = _num(parts[3])
        if enmo is None or enmo < 0:
            return None, "bad enmo60_mg"
        off = parts[6].strip().lower() in ("1", "true", "t", "yes")
        return "H", Heartbeat(parts[1], seq, enmo, _num(parts[4]), _num(parts[5]), off, now)
    if kind == "E":
        parts = line.split(",", 3)
        if len(parts) < 3:
            return None, "E needs event_id and type"
        event_id, etype = parts[1].strip(), parts[2].strip()
        boot_id, sep, seq_s = event_id.rpartition(":")
        if not sep or not boot_id:
            return None, "event_id must be boot_id:seq"
        try:
            seq = int(seq_s)
        except ValueError:
            return None, "bad event seq"
        if etype not in EVENT_TYPES:
            return None, f"unknown event type {etype!r}"
        payload: dict = {}
        if len(parts) == 4 and parts[3].strip():
            try:
                payload = json.loads(parts[3])
            except ValueError:
                return None, "payload is not JSON"
            if not isinstance(payload, dict):
                return None, "payload must be a JSON object"
        device_ms = payload.pop("device_ms", None) if isinstance(payload.get("device_ms"), int) else None
        return "E", PendingEvent(event_id, boot_id, seq, etype, payload, now, device_ms=device_ms)
    if kind == "R":
        return "R", None
    return None, f"unknown line type {kind!r}"


def c_line(cmd: Optional[dict]) -> Optional[str]:
    if not isinstance(cmd, dict):
        return None
    return f"C,{cmd.get('led') or 'off'},{int(cmd.get('blink_n') or 0)},{cmd.get('challenge_id') or ''}"


@dataclass
class Bridge:
    http: httpx.Client
    device_id: str
    device_key: str
    write_line: Callable[[str], None]
    heartbeat_s: float = 2.0
    stale_s: float = 3.0
    wear_mode: Optional[str] = None
    max_buffer: int = 256
    clock: Callable[[], float] = time.monotonic
    last_h: Optional[Heartbeat] = None
    last_hb_post: float = float("-inf")
    events: list = field(default_factory=list)
    stats: dict = field(default_factory=lambda: {"H": 0, "E": 0, "R": 0, "bad": 0, "hb_ok": 0, "hb_fail": 0,
                                                 "ev_ok": 0, "ev_retry": 0, "ev_dropped": 0})

    # -- input ------------------------------------------------------------------
    def handle_line(self, line: str) -> None:
        kind, item = parse_line(line, self.clock())
        if kind is None:
            if item != "empty":
                self.stats["bad"] += 1
                log.warning("ignored line (%s): %s", item, line.strip()[:120])
            return
        self.stats[kind] += 1
        if kind == "H":
            self.last_h = item
        elif kind == "E":
            if any(e.event_id == item.event_id for e in self.events):
                return  # the device repeated a line we still hold
            self.events.append(item)
            if len(self.events) > self.max_buffer:
                # Keep duress events; drop the oldest other event.
                victim = next((e for e in self.events if not e.duress), self.events[0])
                self.events.remove(victim)
                self.stats["ev_dropped"] += 1
                log.warning("event buffer full; dropped %s", victim.event_id)
            self.flush()

    # -- output -----------------------------------------------------------------
    def _headers(self, boot_id: str, seq: int) -> dict:
        return {"Authorization": f"Bearer {self.device_key}", "X-Device-Id": self.device_id,
                "X-Boot-Id": boot_id, "X-Seq": str(seq)}

    def _send_cmd(self, reply: dict) -> None:
        line = c_line(reply.get("cmd"))
        if line:
            self.write_line(line)

    def tick(self) -> None:
        now = self.clock()
        h = self.last_h
        if h is not None and now - h.at <= self.stale_s and now - self.last_hb_post >= self.heartbeat_s:
            self.last_hb_post = now
            self.post_heartbeat(h)
        self.flush()

    def post_heartbeat(self, h: Heartbeat) -> bool:
        body: dict = {"enmo60_mg": h.enmo60_mg, "offbody": h.offbody}
        if self.wear_mode:
            body["wear_mode"] = self.wear_mode
        try:
            r = self.http.post("/v1/device/heartbeat", json=body, headers=self._headers(h.boot_id, h.seq))
        except httpx.HTTPError as exc:
            self.stats["hb_fail"] += 1
            log.warning("heartbeat failed: %s", type(exc).__name__)
            return False
        if r.status_code != 200:
            self.stats["hb_fail"] += 1
            log.warning("heartbeat: HTTP %s", r.status_code)
            return False
        self.stats["hb_ok"] += 1
        try:
            self._send_cmd(r.json())
        except ValueError:
            pass
        return True

    def _order(self) -> list:
        return sorted(self.events, key=lambda e: (not e.duress, e.queued_at, e.seq))

    def flush(self) -> None:
        """Send due events in order (duress first, then oldest). Stops at the first failure."""
        now = self.clock()
        for ev in self._order():
            if ev.next_try > now:
                return  # keep the order: nothing jumps a waiting event
            payload = dict(ev.payload)
            if ev.buffered:
                payload["buffered"] = True
            body = {"event_id": ev.event_id, "type": ev.type, "payload": payload}
            if ev.device_ms is not None:
                body["device_ms"] = ev.device_ms
            try:
                r = self.http.post("/v1/device/events", json=body, headers=self._headers(ev.boot_id, ev.seq))
                status = r.status_code
            except httpx.HTTPError as exc:
                r, status = None, None
                err = type(exc).__name__
            if status == 200:
                self.events.remove(ev)
                self.stats["ev_ok"] += 1
                try:
                    self._send_cmd(r.json())
                except ValueError:
                    pass
                continue
            if status in PERMANENT_HTTP:
                self.events.remove(ev)
                self.stats["ev_dropped"] += 1
                log.error("event %s rejected with HTTP %s; dropped", ev.event_id, status)
                continue
            # Server down or overloaded: keep it, retry 1, 2, 4, 8, 8... s, same event_id.
            # Everything still waiting is now delayed, so all of it goes out marked buffered (late).
            for waiting in self.events:
                waiting.buffered = True
            ev.next_try = now + RETRY_STEPS_S[min(ev.attempts, len(RETRY_STEPS_S) - 1)]
            ev.attempts += 1
            self.stats["ev_retry"] += 1
            log.warning("event %s not delivered (%s); %d waiting", ev.event_id,
                        f"HTTP {status}" if status else err, len(self.events))
            return


# --- transports ------------------------------------------------------------------
class StdinLink:
    """Protocol lines from stdin; C lines to stdout. For tests and rehearsals without hardware."""

    def __init__(self, inp=None, out=None):
        self.inp = inp or sys.stdin
        self.out = out or sys.stdout

    def reader(self, q: "queue.Queue", stop: threading.Event) -> None:
        for line in self.inp:
            if stop.is_set():
                break
            q.put(line)
        q.put(None)  # EOF

    def write_line(self, line: str) -> None:
        self.out.write(line + "\n")
        self.out.flush()


class SerialLink:
    """A serial port that is reopened whenever the device is unplugged and plugged back in."""

    def __init__(self, port: str, baud: int = 115200, reopen_s: float = 1.0):
        if serial is None:
            raise SystemExit("pyserial is not installed: .venv/bin/pip install pyserial")
        self.port, self.baud, self.reopen_s = port, baud, reopen_s
        self._ser = None
        self._lock = threading.Lock()

    def _open(self):
        if self.port.startswith(("loop://", "socket://", "rfc2217://")):
            return serial.serial_for_url(self.port, baudrate=self.baud, timeout=0.2)
        return serial.Serial(self.port, self.baud, timeout=0.2)

    def reader(self, q: "queue.Queue", stop: threading.Event) -> None:
        buf = b""
        waiting_logged = False
        while not stop.is_set():
            if self._ser is None:
                try:
                    ser = self._open()
                except (serial.SerialException, OSError) as exc:
                    if not waiting_logged:
                        log.warning("waiting for %s (%s)", self.port, exc)
                        waiting_logged = True
                    stop.wait(self.reopen_s)
                    continue
                with self._lock:
                    self._ser = ser
                waiting_logged = False
                buf = b""
                log.info("serial %s open at %d baud", self.port, self.baud)
            try:
                chunk = self._ser.read(256)
            except (serial.SerialException, OSError) as exc:
                log.warning("serial %s lost (%s); reopening", self.port, exc)
                self.close()
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                q.put(raw.decode("utf-8", errors="replace").rstrip("\r"))

    def write_line(self, line: str) -> None:
        with self._lock:
            ser = self._ser
        if ser is None:
            return
        try:
            ser.write((line + "\n").encode("ascii", errors="replace"))
        except (serial.SerialException, OSError) as exc:
            log.warning("serial write failed (%s)", exc)

    def close(self) -> None:
        with self._lock:
            ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass


def run(bridge: Bridge, link, stop: Optional[threading.Event] = None, tick_s: float = 0.1) -> None:
    stop = stop or threading.Event()
    q: "queue.Queue" = queue.Queue()
    t = threading.Thread(target=link.reader, args=(q, stop), name="bridge-reader", daemon=True)
    t.start()
    last_report = time.monotonic()
    eof = False
    while not stop.is_set():
        try:
            line = q.get(timeout=tick_s)
        except queue.Empty:
            line = ""
        if line is None:
            eof = True
        elif line:
            bridge.handle_line(line)
        bridge.tick()
        if eof and not bridge.events:
            break
        if eof:
            time.sleep(tick_s)
        if time.monotonic() - last_report >= 30:
            last_report = time.monotonic()
            log.info("stats %s, %d events waiting", bridge.stats, len(bridge.events))
    stop.set()


def main(argv: Optional[list] = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    except ImportError:
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--port", help="serial port, e.g. /dev/cu.usbserial-0001 or COM3")
    src.add_argument("--stdin", action="store_true", help="read protocol lines from stdin, write C lines to stdout")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--url", default=os.environ.get("STILLHERE_URL", "http://localhost:8000"))
    ap.add_argument("--device-id", default=os.environ.get("DEMO_DEVICE_ID", "scrunchie-1"))
    ap.add_argument("--heartbeat-s", type=float, default=2.0, help="2 for the demo profile, 15 for real")
    ap.add_argument("--wear-mode", choices=["wrist", "hair"], default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s bridge: %(message)s")
    for noisy in ("httpx", "httpcore"):  # one INFO line per request otherwise
        logging.getLogger(noisy).setLevel(logging.WARNING)
    key = os.environ.get("DEVICE_KEY")
    if not key:
        print("DEVICE_KEY is not set", file=sys.stderr)
        return 2
    link = StdinLink() if args.stdin else SerialLink(args.port, args.baud)
    with httpx.Client(base_url=args.url.rstrip("/"), timeout=5.0) as http:
        bridge = Bridge(http=http, device_id=args.device_id, device_key=key, write_line=link.write_line,
                        heartbeat_s=args.heartbeat_s, wear_mode=args.wear_mode)
        log.info("bridge %s -> %s as %s", args.port or "stdin", args.url, args.device_id)
        try:
            run(bridge, link)
        except KeyboardInterrupt:
            pass
        finally:
            if isinstance(link, SerialLink):
                link.close()
    if bridge.events:
        log.warning("%d events were still waiting at exit", len(bridge.events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
