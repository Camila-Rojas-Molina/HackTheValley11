"""Shared plumbing for the headless heart-rate bridges (scripts/hr_bleak.py, scripts/hr_pulsoid.py).

Both post into the same pluggable endpoint, POST /v1/hr, with the wearer key. Run only one bridge
at a time, and not together with the /hr-bridge page: every reading would be posted twice. Each
bridge checks GET /v1/hr/status before it starts and refuses while another source posted in the
last 15 s (--force skips the check).

Env: STILLHERE_URL (default http://localhost:8000) and WEARER_KEY (the wearer's bearer key; the
server's own WEARER_KEY for the demo wearer). backend/.env is read if present. Neither the key nor
any token is ever printed: failures log the HTTP status or the exception type only.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

import httpx

log = logging.getLogger("stillhere.hr_bridge")

LIVE_S = 15.0   # another source with a sample this recent counts as running (GET /v1/hr/status)


def load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:  # pragma: no cover
        pass


@dataclass(frozen=True)
class Config:
    base_url: str
    wearer_key: str

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        e = os.environ if env is None else env
        key = (e.get("WEARER_KEY") or "").strip()
        if not key:
            raise SystemExit("WEARER_KEY is not set: the bridge posts to /v1/hr with the wearer's key "
                             "(put it in backend/.env with /env-key; it is never printed)")
        return cls(base_url=(e.get("STILLHERE_URL") or "http://localhost:8000").rstrip("/"), wearer_key=key)

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.wearer_key}"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Poster:
    """POST /v1/hr and GET /v1/hr/status. Never raises on a network error; never logs the key."""

    def __init__(self, cfg: Config, *, transport: Optional[httpx.AsyncBaseTransport] = None, timeout: float = 5.0):
        self.cfg = cfg
        self._client = httpx.AsyncClient(base_url=cfg.base_url, headers=cfg.headers(), timeout=timeout,
                                         transport=transport)

    async def __aenter__(self) -> "Poster":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def post(self, sample: Optional[dict]) -> bool:
        if sample is None:
            return False
        try:
            r = await self._client.post("/v1/hr", json=sample)
        except httpx.HTTPError as exc:
            log.warning("POST /v1/hr failed: %s", type(exc).__name__)
            return False
        if r.status_code != 200:
            log.warning("POST /v1/hr answered %s", r.status_code)
            return False
        return True

    async def others_live(self, my_source: str) -> list[str]:
        """Sources other than ours that posted for this wearer in the last LIVE_S seconds."""
        try:
            r = await self._client.get("/v1/hr/status")
        except httpx.HTTPError as exc:
            log.warning("GET /v1/hr/status failed: %s", type(exc).__name__)
            return []
        if r.status_code != 200:
            log.warning("GET /v1/hr/status answered %s", r.status_code)
            return []
        now = datetime.now(timezone.utc)
        out = []
        for s in r.json().get("sources", []):
            last = datetime.fromisoformat(s["last_at"])
            if s["source"] != my_source and (now - last).total_seconds() <= LIVE_S:
                out.append(s["source"])
        return out

    async def guard(self, my_source: str, *, force: bool) -> None:
        """Refuse to start while another bridge is posting for the same wearer."""
        if force:
            return
        others = await self.others_live(my_source)
        if others:
            raise SystemExit(f"another heart-rate source is posting for this wearer ({', '.join(others)}): "
                             "run only one bridge at a time (stop the other, or pass --force)")
