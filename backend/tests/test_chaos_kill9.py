"""Chaos: kill -9 the server mid-REMINDED; after restart the stored deadline still fires L1 once.

Starts a real uvicorn process with the workers on, drives the demo session to
REMINDED through the operator API, SIGKILLs the process, restarts it and waits.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from tests.conftest import fresh_database

BACKEND = Path(__file__).resolve().parent.parent
ADMIN = {"X-Admin-Key": "chaos-admin"}
WEARER = {"Authorization": "Bearer chaos-wearer"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(env: dict, port: int, log: Path) -> subprocess.Popen:
    fh = open(log, "ab")
    return subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=BACKEND, env=env, stdout=fh, stderr=subprocess.STDOUT)


def _wait_healthy(base: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/v1/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError("server did not become healthy")


def _wait_for(fn, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.mark.chaos
def test_kill9_mid_reminded_still_fires_l1_once(pg_base_uri, tmp_path):
    url = fresh_database(pg_base_uri, "stillhere_chaos")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "DATABASE_URL": url, "ADMIN_KEY": "chaos-admin", "DEVICE_KEY": "chaos-device",
           "WEARER_KEY": "chaos-wearer", "DEMO_PIN": "2468", "DEMO_DURESS_PIN": "1357",
           "APP_SECRET": "chaos-secret", "PUBLIC_BASE_URL": base, "RUN_WORKERS": "1", "AUTO_MIGRATE": "1",
           "DEMO_SEED": "1", "TELEGRAM_BOT_TOKEN": "", "STILLHERE_PROFILE": "demo"}
    log = tmp_path / "server.log"
    proc = _start(env, port, log)
    proc2 = None
    try:
        _wait_healthy(base)
        with httpx.Client(base_url=base, timeout=10) as c:
            inv = c.post("/v1/contacts", json={"name": "Sam"}, headers=WEARER).json()
            tok = inv["web_link"].rsplit("/", 1)[1]
            assert c.post(f"/v1/contact/{tok}/join", json={}).status_code == 200
            assert c.post(f"/v1/contacts/{inv['contact_id']}/approve", headers=WEARER).status_code == 200
            sid = c.post("/v1/demo/reset", headers=ADMIN, json={}).json()["armed"]["session_id"]
            assert c.post("/v1/demo/checkin-now", headers=ADMIN).json()["state"] == "PROMPTED"
            assert c.post("/v1/demo/fast-forward", headers=ADMIN).json()["fired_next"] == "PROMPT_TIMEOUT"

            def state():
                s = [x for x in c.get("/v1/admin/overview", headers=ADMIN).json()["sessions"] if x["id"] == sid]
                return s[0]["state"] if s else None
            _wait_for(lambda: state() == "REMINDED", 10, "REMINDED")

        os.kill(proc.pid, signal.SIGKILL)  # kill -9 mid-REMINDED
        proc.wait(timeout=10)
        assert proc.returncode == -signal.SIGKILL

        with psycopg.connect(url, row_factory=dict_row) as conn:
            t = conn.execute(
                """SELECT id, fire_at FROM timers WHERE session_id = %s AND kind = 'REMINDER_TIMEOUT'
                      AND fired_at IS NULL AND cancelled_at IS NULL""", (sid,)).fetchone()
        assert t is not None, "the deadline row survived the kill"

        proc2 = _start(env, port, log)
        _wait_healthy(base)

        def fired():
            with psycopg.connect(url, row_factory=dict_row) as conn:
                row = conn.execute("SELECT fired_at, fire_at, result FROM timers WHERE id = %s",
                                   (t["id"],)).fetchone()
            return row if row["fired_at"] else None
        row = _wait_for(fired, 60, "REMINDER_TIMEOUT to fire after restart")
        # fire_at may have been lowered after restart (prompt deadline = delivery + window),
        # never raised; L1 must fire within 2 s of the stored deadline.
        assert row["fire_at"] <= t["fire_at"]
        lag = (row["fired_at"] - row["fire_at"]).total_seconds()
        assert 0 <= lag <= 2.0, f"L1 fired {lag:.2f} s after the stored deadline"
        assert row["result"] == "escalated"

        with psycopg.connect(url, row_factory=dict_row) as conn:
            s = conn.execute("SELECT state FROM sessions WHERE id = %s", (sid,)).fetchone()
            l1 = conn.execute(
                """SELECT count(*) AS n FROM outbox
                    WHERE session_id = %s AND template = 'alert_no_response' AND contact_id = %s""",
                (sid, inv["contact_id"])).fetchone()
        assert s["state"] in ("ESCALATED_L1", "ESCALATED_FINAL")
        assert l1["n"] >= 1
        with psycopg.connect(url, row_factory=dict_row) as conn:
            first_l1 = conn.execute(
                """SELECT count(*) AS n FROM outbox WHERE session_id = %s AND template = 'alert_no_response'
                      AND dedupe_key LIKE %s""", (sid, f"timer:{t['id']}|%")).fetchone()
        assert first_l1["n"] == 1  # exactly one L1 message for that deadline
    finally:
        for p in (proc, proc2):
            if p is not None and p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        print(log.read_text()[-3000:] if log.exists() else "")
