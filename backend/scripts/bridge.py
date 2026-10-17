"""USB-serial bridge: scrunchie line protocol <-> StillHere HTTP API (BUILD_PLAN §5.8, §5.9).

device -> bridge (115200 baud, one line per message)
  H,<boot_id>,<seq>,<enmo60_mg>,<tilt_deg>,<fsr_base_mv>,<offbody>    every 1 s
  E,<boot_id>:<seq>,<type>,<json_payload>                             immediately
  R,<ms>,<ax_mg>,<ay_mg>,<az_mg>[,<fsr_a_mv>,<fsr_b_mv>]              50 Hz RECORD mode (debug telemetry)
bridge -> device
  C,<led>,<blink_n>,<challenge_id>                                    after every heartbeat / event reply

HTTP
  POST /v1/device/heartbeat  every --heartbeat-s (2 s demo, 15 s real), only while H lines keep
                             arriving: unplug the device and the server's lease runs out (LOST_CONTACT).
  POST /v1/device/events     immediately; event_id = boot_id:seq. Retried after 1, 2, 4, 8 s (then every
                             8 s) with the same id. While the server is unreachable, events wait in memory
                             and are flushed duress first, then oldest first, marked buffered=true so the
                             server treats them as late (a late squeeze never stands an alert down).
                             A squeeze carries the challenge_id of the prompt the device was showing
                             when it arrived (none if no prompt): a held-back squeeze then answers only
                             that prompt, never one issued while it waited.
  POST /v1/telemetry/batch   R lines, about once a second (or every 500 rows), clock=device_ms, only while
                             the heartbeat reply says "telemetry": "debug" (the demo profile; the real
                             profile sends heartbeats and events only), or always with --telemetry on.
                             Events never wait behind telemetry. While the server is unreachable at most
                             2 minutes of rows wait (the oldest are dropped first); a retried batch keeps
                             its batch_id, so the server stores it once.
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
import math
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
TEL_BATCH_ROWS = 500       # 10 s at 50 Hz: a batch goes out at once when it is this big
TEL_EVERY_S = 1.0          # ...or when its oldest row has waited this long
TEL_MAX_BUFFER = 6000      # 2 minutes at 50 Hz (the server's batch limit); older rows are dropped
TEL_RETRY_S = 1.0
TELEMETRY_MODES = ("auto", "on", "off")


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


def _printable(text: str) -> bool:
    return bool(text) and all(32 <= ord(ch) < 127 for ch in text)


def parse_line(line: str, now: float):
    """('H', Heartbeat) | ('E', PendingEvent) | ('R', None) | (None, reason).

    Ids must be printable ASCII: serial noise (a NUL byte, say) in a boot id or event id would make
    every send of that event fail before it leaves the bridge, and hold up the events behind it."""
    line = line.strip()
    if not line:
        return None, "empty"
    kind = line[0]
    if kind == "H":
        parts = line.split(",")
        if len(parts) != 7:
            return None, "H needs 7 fields"
        if not _printable(parts[1]):
            return None, "bad boot_id"
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
        if not _printable(event_id):
            return None, "event_id must be printable"
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
        parts = line.split(",")
        if not 5 <= len(parts) <= 7:
            return None, "R needs 4 to 6 fields"
        row = [_num(v) for v in parts[1:]]
        if any(v is None or not math.isfinite(v) for v in row):
            return None, "bad R value"
        return "R", row
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
    challenge_id: Optional[str] = None  # the prompt the device is showing (from the last C line)
    events: list = field(default_factory=list)
    # R lines: "auto" forwards them while the heartbeat reply says "telemetry": "debug" (demo profile).
    telemetry: str = "auto"
    server_telemetry: bool = False
    _tel_rows: list = field(default_factory=list)
    _tel_since: float = 0.0                       # when the oldest waiting row arrived
    _tel_next: float = float("-inf")              # no telemetry POST before this (after a failure)
    _tel_inflight: Optional[tuple] = None         # (rows, batch_id) of a batch being retried as-is
    stats: dict = field(default_factory=lambda: {"H": 0, "E": 0, "R": 0, "bad": 0, "hb_ok": 0, "hb_fail": 0,
                                                 "ev_ok": 0, "ev_retry": 0, "ev_dropped": 0,
                                                 "tel_rows_ok": 0, "tel_rows_dropped": 0, "tel_fail": 0})

    def forwarding(self) -> bool:
        return self.telemetry == "on" or (self.telemetry == "auto" and self.server_telemetry)

    # -- input ------------------------------------------------------------------
    def handle_line(self, line: str) -> None:
        kind, item = parse_line(line, self.clock())
        if kind is None:
            if item != "empty":
                self.stats["bad"] += 1
                log.warning("ignored line (%s): %s", item, line.strip()[:120])
            return
        self.stats[kind] += 1
        if kind == "R":
            if self.forwarding():
                self._queue_row(item)
            return
        if kind == "H":
            self.last_h = item
        elif kind == "E":
            if any(e.event_id == item.event_id for e in self.events):
                return  # the device repeated a line we still hold
            if item.type == "squeeze" and self.challenge_id and "challenge_id" not in item.payload:
                item.payload["challenge_id"] = self.challenge_id
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
        cmd = reply.get("cmd")
        if isinstance(cmd, dict):
            self.challenge_id = cmd.get("challenge_id") or None
        line = c_line(cmd)
        if line:
            self.write_line(line)

    def tick(self) -> None:
        now = self.clock()
        h = self.last_h
        if h is not None and now - h.at <= self.stale_s and now - self.last_hb_post >= self.heartbeat_s:
            self.last_hb_post = now
            self.post_heartbeat(h)
        self.flush()
        self.flush_telemetry()

    # -- RECORD-mode telemetry (R lines) -----------------------------------------
    def _queue_row(self, row: list) -> None:
        if not self._tel_rows:
            self._tel_since = self.clock()
        self._tel_rows.append(row)
        over = len(self._tel_rows) - TEL_MAX_BUFFER
        if over > 0:
            del self._tel_rows[:over]
            self._tel_inflight = None   # its first rows are gone: what is left goes out as a new batch
            self.stats["tel_rows_dropped"] += over

    def _drop_telemetry(self) -> None:
        self._tel_rows.clear()
        self._tel_inflight = None

    def flush_telemetry(self) -> None:
        """POST one batch of waiting R rows when it is full or its oldest row has waited TEL_EVERY_S."""
        if not self._tel_rows:
            return
        now = self.clock()
        if now < self._tel_next:
            return
        if self._tel_inflight is None:
            if len(self._tel_rows) < TEL_BATCH_ROWS and now - self._tel_since < TEL_EVERY_S:
                return
            rows = self._tel_rows[:TEL_BATCH_ROWS]
            boot = self.last_h.boot_id if self.last_h else "-"
            self._tel_inflight = (rows, f"{boot}:{int(rows[0][0])}:{len(rows)}")
        rows, batch_id = self._tel_inflight
        h = self.last_h
        body = {"rows": rows, "clock": "device_ms", "profile": "night_out", "simulated": False,
                "batch_id": batch_id}
        try:
            r = self.http.post("/v1/telemetry/batch", json=body,
                               headers=self._headers(h.boot_id if h else "-", h.seq if h else 0))
            status = r.status_code
        except httpx.HTTPError as exc:
            status, err = None, type(exc).__name__
        if status == 200 or status in PERMANENT_HTTP:
            del self._tel_rows[:len(rows)]
            self._tel_inflight = None
            self._tel_since = now
            if status == 200:
                self.stats["tel_rows_ok"] += len(rows)
            else:
                self.stats["tel_rows_dropped"] += len(rows)
                log.error("telemetry batch %s rejected with HTTP %s; dropped", batch_id, status)
            return
        self.stats["tel_fail"] += 1
        self._tel_next = now + TEL_RETRY_S
        log.warning("telemetry batch %s not delivered (%s); %d rows waiting", batch_id,
                    f"HTTP {status}" if status else err, len(self._tel_rows))

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
            reply = r.json()
        except ValueError:
            return True
        if isinstance(reply, dict):
            self.server_telemetry = reply.get("telemetry") == "debug"
            if not self.forwarding():
                self._drop_telemetry()
        self._send_cmd(reply if isinstance(reply, dict) else {})
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
    tel_fails_at_eof = 0
    while not stop.is_set():
        try:
            line = q.get(timeout=tick_s)
        except queue.Empty:
            line = ""
        if line is None:
            eof = True
            bridge._tel_since = float("-inf")   # send the last telemetry rows now
            tel_fails_at_eof = bridge.stats["tel_fail"]
        elif line:
            bridge.handle_line(line)
        bridge.tick()
        # At the end of input: events wait to be delivered; telemetry gets one try.
        if eof and not bridge.events and (not bridge._tel_rows or bridge.stats["tel_fail"] > tel_fails_at_eof):
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
    ap.add_argument("--telemetry", choices=TELEMETRY_MODES, default="auto",
                    help="forward RECORD-mode R lines: auto (when the server asks for debug telemetry), on, off")
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
                        heartbeat_s=args.heartbeat_s, wear_mode=args.wear_mode, telemetry=args.telemetry)
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
