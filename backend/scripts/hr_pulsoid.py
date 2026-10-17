"""Pulsoid (Apple Watch app -> Pulsoid cloud -> WebSocket) -> POST /v1/hr. source: pulsoid-ws.

The backup heart-rate path: a separate failure domain from the Mac's Bluetooth. Messages look like
{"measured_at": <unix ms>, "data": {"heart_rate": N}}; measured_at becomes ts. Pulsoid's docs say
the connection can drop at any time, so it reconnects with backoff (1 s doubling to 30 s).

  ../.venv/bin/python scripts/hr_pulsoid.py          # from backend/

Env: PULSOID_TOKEN (a token with data:heart_rate:read; put it in backend/.env with /env-key, never
in chat), STILLHERE_URL, WEARER_KEY. The token travels in the Authorization header, never in the
URL, and no log line contains it: reconnects log the exception type only.

Never run it together with /hr-bridge or scripts/hr_bleak.py: it refuses to start while another
source posted in the last 15 s (--force to skip that check). Not tested here against Pulsoid (no
account or token): the message parser and the posting path are covered by tests/test_hr_bridges.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
from scripts.hr_common import Config, Poster, load_dotenv_if_present, log  # noqa: E402

SOURCE = "pulsoid-ws"
WS_URL = "wss://dev.pulsoid.net/api/v1/data/real_time"
MAX_BACKOFF_S = 30


def ws_headers(env: Optional[Mapping[str, str]] = None) -> dict:
    e = os.environ if env is None else env
    token = (e.get("PULSOID_TOKEN") or "").strip()
    if not token:
        raise SystemExit("PULSOID_TOKEN is not set (put it in backend/.env with /env-key; it is never printed)")
    return {"Authorization": f"Bearer {token}"}


def parse_message(raw) -> Optional[dict]:
    """The POST /v1/hr body for one Pulsoid message, or None if it is not a heart-rate reading."""
    try:
        m = json.loads(raw)
        bpm = m["data"]["heart_rate"]
        ts = datetime.fromtimestamp(m["measured_at"] / 1000, timezone.utc).isoformat()
    except (ValueError, KeyError, TypeError, OverflowError, OSError):
        return None
    if isinstance(bpm, bool) or not isinstance(bpm, int) or not 20 <= bpm <= 250:
        return None
    return {"ts": ts, "bpm": bpm, "rr_ms": [], "source": SOURCE, "simulated": False}


def log_reconnect(exc: BaseException, wait_s: float) -> None:
    log.warning("pulsoid: reconnect in %g s (%s)", wait_s, type(exc).__name__)


async def run(cfg: Config, headers: dict, force: bool) -> None:  # pragma: no cover - needs Pulsoid
    from websockets.asyncio.client import connect

    async with Poster(cfg) as poster:
        await poster.guard(SOURCE, force=force)
        back = 1
        while True:
            try:
                async with connect(WS_URL, additional_headers=headers) as ws:
                    back = 1
                    async for raw in ws:
                        await poster.post(parse_message(raw))
            except Exception as exc:
                log_reconnect(exc, back)
                await asyncio.sleep(back)
                back = min(back * 2, MAX_BACKOFF_S)


def main(argv: Optional[list] = None) -> int:  # pragma: no cover - needs Pulsoid
    ap = argparse.ArgumentParser(description="Pulsoid WebSocket heart rate -> POST /v1/hr (source pulsoid-ws)")
    ap.add_argument("--force", action="store_true", help="start even if another source is posting")
    args = ap.parse_args(argv)
    load_dotenv_if_present()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg, headers = Config.from_env(), ws_headers()
    try:
        asyncio.run(run(cfg, headers, args.force))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
