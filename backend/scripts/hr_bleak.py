"""HeartCast (iPhone, standard BLE Heart Rate Service) -> POST /v1/hr, headless. source: heartcast-ble.

The Apple Watch runs a HeartCast workout; the iPhone advertises the Heart Rate Service (0x180D) and
notifies the Heart Rate Measurement characteristic (0x2A37) about once a second, while the Watch
itself updates about every 5 s, so most notifications repeat the last value. The server counts at
most one reading per 5 s in its minimum-sample checks. BLE packets carry no timestamp: ts is the
time this Mac received the reading.

  ../.venv/bin/python scripts/hr_bleak.py            # from backend/; scans for the first HRS device
  HR_DEVICE=<uuid> ../.venv/bin/python scripts/hr_bleak.py   # pin the phone printed on the first run

Setup (research pack, hr-live.md section 4): HeartCast open and in the foreground on an unlocked
iPhone, Start pressed on the Watch, Low Power Mode off. Allow the macOS Bluetooth prompt for the
terminal the first time. At a hackathon other heart-rate straps are nearby: note the printed
address (a macOS UUID, not a MAC address) and pin it with HR_DEVICE.

Never run it together with /hr-bridge or scripts/hr_pulsoid.py: it refuses to start while another
source posted in the last 15 s (--force to skip that check). Env: STILLHERE_URL, WEARER_KEY,
HR_DEVICE (optional). Needs bleak (requirements.txt, macOS only). Not tested here against a phone:
the parser and the posting path are covered by tests/test_hr_bridges.py.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from scripts.hr_common import Config, Poster, load_dotenv_if_present, log, utc_now_iso  # noqa: E402

SOURCE = "heartcast-ble"
HRS = "0000180d-0000-1000-8000-00805f9b34fb"   # Heart Rate Service
HRM = "00002a37-0000-1000-8000-00805f9b34fb"   # Heart Rate Measurement
SILENCE_S = 15.0                               # no notification for this long: reconnect


@dataclass(frozen=True)
class Reading:
    bpm: int
    rr_ms: list = field(default_factory=list)
    rr_synthetic: bool = False


def parse_hrm(data: bytes) -> Reading:
    """Heart Rate Measurement (0x2A37), as hr_bridge.html's parseHRM. Flags bit 0: 16-bit bpm;
    bit 3: Energy Expended present (skipped); bit 4: RR intervals in 1/1024 s. RR equal to 60000/bpm
    was computed by the relay, not measured, and is flagged rr_synthetic."""
    flags = data[0]
    if flags & 0x01:
        bpm, i = int.from_bytes(data[1:3], "little"), 3
    else:
        bpm, i = data[1], 2
    if flags & 0x08:
        i += 2
    rr = []
    if flags & 0x10:
        rr = [int.from_bytes(data[j:j + 2], "little") * 1000 / 1024 for j in range(i, len(data) - 1, 2)]
    synthetic = bool(rr) and bpm > 0 and all(abs(r - 60000 / bpm) <= 1 for r in rr)
    return Reading(bpm=bpm, rr_ms=rr, rr_synthetic=synthetic)


def sample(r: Reading) -> Optional[dict]:
    """The POST /v1/hr body for one notification, or None for "no reading" (watch off the wrist)."""
    if not 20 <= r.bpm <= 250:
        return None
    return {"ts": utc_now_iso(), "bpm": r.bpm, "rr_ms": [round(x, 1) for x in r.rr_ms] or None,
            "rr_synthetic": r.rr_synthetic if r.rr_ms else None, "source": SOURCE, "simulated": False}


async def run(cfg: Config, pin: Optional[str], force: bool) -> None:  # pragma: no cover - needs a phone
    from bleak import BleakClient, BleakScanner

    async with Poster(cfg) as poster:
        await poster.guard(SOURCE, force=force)
        while True:
            dev = await BleakScanner.find_device_by_address(pin, timeout=20) if pin else None
            if dev is None:
                dev = await BleakScanner.find_device_by_filter(lambda d, a: True, timeout=20, service_uuids=[HRS])
            if dev is None:
                log.warning("no Heart Rate Service advertiser: HeartCast in the foreground and the Watch workout "
                            "started?")
                continue
            log.warning("found %s %s (pin it with HR_DEVICE=%s)", dev.name, dev.address, dev.address)
            q: asyncio.Queue = asyncio.Queue()
            try:
                async with BleakClient(dev) as client:
                    await client.start_notify(HRM, lambda _, data: q.put_nowait(parse_hrm(bytes(data))))
                    while True:
                        reading = await asyncio.wait_for(q.get(), SILENCE_S)
                        await poster.post(sample(reading))
            except Exception as exc:   # disconnect, timeout, Bluetooth error: scan again
                log.warning("reconnecting: %s", type(exc).__name__)
                await asyncio.sleep(2)


def main(argv: Optional[list] = None) -> int:  # pragma: no cover - needs a phone
    ap = argparse.ArgumentParser(description="HeartCast BLE heart rate -> POST /v1/hr (source heartcast-ble)")
    ap.add_argument("--force", action="store_true", help="start even if another source is posting")
    args = ap.parse_args(argv)
    load_dotenv_if_present()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = Config.from_env()
    try:
        asyncio.run(run(cfg, os.environ.get("HR_DEVICE") or None, args.force))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
