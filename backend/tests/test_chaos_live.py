"""Chaos against a live server process (BUILD_PLAN §6.9):

* the database goes away under a running server (a TCP proxy in front of Postgres is cut) while a prompt
  deadline is due: the API answers 503 (never a 4xx the bridge would drop), the process stays up, and once
  the database is back the server recovers by itself and the missed deadline fires once;
* the server is kill -9'd while scripts/bridge.py is running: a duress squeeze and a plain squeeze made
  during the outage wait in the bridge, go out duress first and marked buffered once a fresh server is up,
  the duress alert opens masked, and the late plain squeeze does not stand it down.

The kill -9 mid-REMINDED case is tests/test_chaos_kill9.py (and serve.sh + chaos.sh in test_ops_live.py).
"""
from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from tests.livesrv import ADMIN, BACKEND, KEYS, SCRIPTS, LiveServer, device_headers, free_port, wait_for

pytestmark = pytest.mark.chaos


class PgProxy:
    """127.0.0.1:<port> -> the Postgres server (unix socket or TCP). cut() drops every connection and stops
    listening; restore() listens again on the same port."""

    def __init__(self, base_uri: str):
        info = conninfo_to_dict(base_uri)
        host, port = info.get("host") or "/tmp", int(info.get("port") or 5432)
        self.target = (socket.AF_UNIX, f"{host}/.s.PGSQL.{port}") if host.startswith("/") else (socket.AF_INET, (host, port))
        self.port = free_port()
        self.listener: Optional[socket.socket] = None
        self.conns: list[socket.socket] = []
        self.lock = threading.Lock()
        self.accepted = 0

    def url(self, base_uri: str, dbname: str) -> str:
        info = conninfo_to_dict(base_uri)
        info.update(host="127.0.0.1", port=str(self.port), dbname=dbname, connect_timeout="3")
        return make_conninfo(**info)

    def restore(self) -> None:
        lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lst.bind(("127.0.0.1", self.port))
        lst.listen(64)
        self.listener = lst
        threading.Thread(target=self._accept, args=(lst,), daemon=True).start()

    def cut(self) -> None:
        lst, self.listener = self.listener, None
        if lst is not None:
            lst.close()
        with self.lock:
            conns, self.conns = self.conns, []
        for s in conns:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def _accept(self, lst: socket.socket) -> None:
        while True:
            try:
                client, _ = lst.accept()
            except OSError:
                return
            fam, addr = self.target
            upstream = socket.socket(fam, socket.SOCK_STREAM)
            try:
                upstream.connect(addr)
            except OSError:
                client.close()
                continue
            with self.lock:
                self.conns += [client, upstream]
                self.accepted += 1
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self._pump, args=(a, b), daemon=True).start()

    @staticmethod
    def _pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def _q(url: str, sql: str, *args) -> list[dict]:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        return list(conn.execute(sql, args).fetchall())


def _status(base: str, path: str, **kw) -> int:
    try:
        return httpx.request(kw.pop("method", "GET"), base + path, timeout=15, **kw).status_code
    except httpx.HTTPError:
        return 0


def test_database_outage_then_recovery_on_a_live_server(pg_base_uri, tmp_path):
    proxy = PgProxy(pg_base_uri)
    proxy.restore()
    name = "stillhere_chaos_dbdown"
    srv = LiveServer(pg_base_uri, name, tmp_path, database_url=proxy.url(pg_base_uri, name))
    srv.start()
    try:
        with srv.client() as c:
            sid = c.post("/v1/demo/reset", headers=ADMIN, json={}).json()["armed"]["session_id"]
            assert c.post("/v1/demo/checkin-now", headers=ADMIN).json()["state"] == "PROMPTED"
        wait_for(lambda: _q(srv.db_url, """SELECT 1 FROM outbox WHERE session_id = %s AND template = 'reminder'
                                            AND status = 'sent'""", sid), 10, "the reminder to go out")
        timer = _q(srv.db_url, """SELECT id, fire_at FROM timers WHERE session_id = %s AND kind = 'PROMPT_TIMEOUT'
                                   AND fired_at IS NULL AND cancelled_at IS NULL""", sid)[0]

        proxy.cut()                                                        # the database goes away
        t_cut = time.monotonic()
        wait_for(lambda: _status(srv.base, "/v1/health") == 503, 20, "health to report the database down")
        dev = device_headers("dbdown", 1)
        r = httpx.post(f"{srv.base}/v1/device/heartbeat", headers=dev, json={}, timeout=15)
        assert (r.status_code, r.json()) == (503, {"ok": False, "error": "database unavailable"})
        body = httpx.get(f"{srv.base}/v1/health", timeout=15).json()
        assert body["db"] == "down" and body["ok"] is False
        # Stay down until the deadline has passed, with margin.
        wait_s = max(0.0, (timer["fire_at"] - _q(srv.db_url, "SELECT now() AS n")[0]["n"]).total_seconds()) + 3
        time.sleep(wait_s)
        assert _q(srv.db_url, "SELECT fired_at FROM timers WHERE id = %s", timer["id"])[0]["fired_at"] is None
        assert srv.proc.poll() is None, "the server died with the database"
        outage = time.monotonic() - t_cut

        proxy.restore()                                                    # the database is back
        srv.wait_healthy(timeout=60)
        row = wait_for(lambda: (_q(srv.db_url, "SELECT fired_at, result FROM timers WHERE id = %s",
                                   timer["id"])[0] or None) if _q(srv.db_url, "SELECT fired_at FROM timers WHERE id = %s",
                                                                   timer["id"])[0]["fired_at"] else None,
                       60, "the missed deadline to fire after recovery")
        assert row["result"] == "prompted"                                # PROMPTED -> REMINDED, a new count
        assert _q(srv.db_url, "SELECT state FROM sessions WHERE id = %s", sid)[0]["state"] == "REMINDED"
        reminders = _q(srv.db_url, """SELECT count(*) AS n FROM outbox WHERE session_id = %s
                                       AND template = 'reminder_n' AND dedupe_key LIKE %s""", sid, f"timer:{timer['id']}|%")
        assert reminders[0]["n"] == 1                                      # fired once, one message
        r = httpx.post(f"{srv.base}/v1/device/heartbeat", headers=device_headers("dbdown", 2), json={}, timeout=15)
        assert r.status_code == 200 and r.json()["state_public"] == "REMINDED"
        ops = httpx.get(f"{srv.base}/v1/health", headers=ADMIN, timeout=15).json()
        assert ops["failing_timers"] == 0
        assert srv.proc.poll() is None and outage >= 10
    finally:
        srv.stop()
        proxy.cut()
        print(srv.log_text()[-3000:])


class _Bridge:
    """scripts/bridge.py --stdin as a child process, fed line by line."""

    def __init__(self, base: str, env: dict, log: Path):
        self.proc = subprocess.Popen([sys.executable, str(SCRIPTS / "bridge.py"), "--stdin", "--url", base,
                                      "--device-id", "scrunchie-1", "--heartbeat-s", "1", "--telemetry", "off"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(log, "ab"),
                                     text=True, bufsize=1, cwd=BACKEND, env={**env, "DEVICE_KEY": KEYS["DEVICE_KEY"]})
        self.out: list[str] = []
        self.seq = 0
        self.stop = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._heartbeats, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.out.append(line.strip())

    def _heartbeats(self) -> None:
        while not self.stop.is_set():
            self.send(f"H,cb,{self.next_seq()},10,0,400,0")
            self.stop.wait(0.5)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def send(self, line: str) -> None:
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass

    def close(self, timeout: float = 30) -> int:
        self.stop.set()
        time.sleep(0.6)
        self.proc.stdin.close()
        return self.proc.wait(timeout=timeout)


def test_the_bridge_holds_events_while_the_server_is_down_and_flushes_them_duress_first(pg_base_uri, tmp_path):
    srv = LiveServer(pg_base_uri, "stillhere_chaos_bridge", tmp_path).start()
    bridge = None
    try:
        with srv.client() as c:
            sid = c.post("/v1/demo/reset", headers=ADMIN, json={}).json()["armed"]["session_id"]
            assert c.post("/v1/demo/checkin-now", headers=ADMIN).json()["state"] == "PROMPTED"
        bridge = _Bridge(srv.base, srv.env, tmp_path / "bridge.log")
        wait_for(lambda: any(ln.startswith("C,amber,0,") for ln in bridge.out), 15, "the device to show the prompt")
        challenge = _q(srv.db_url, "SELECT challenge_id FROM sessions WHERE id = %s", sid)[0]["challenge_id"]

        srv.kill9()                                                        # the server is gone
        bridge.send(f"E,cb:{bridge.next_seq()},squeeze," + '{"pattern":"plain","k":2}')
        bridge.send(f"E,cb:{bridge.next_seq()},squeeze," + '{"pattern":"duress","k":3}')
        time.sleep(4)                                                      # retries fail; both wait in the bridge
        assert not _q(srv.db_url, "SELECT 1 FROM device_events WHERE device_id = 'scrunchie-1' AND type = 'squeeze'")

        srv.start()                                                        # a fresh server process
        events = wait_for(lambda: (lambda rows: rows if len(rows) == 2 else None)(_q(
            srv.db_url, """SELECT event_id, payload, received_at, result FROM device_events
                            WHERE device_id = 'scrunchie-1' AND type = 'squeeze' ORDER BY received_at""")),
            30, "both held squeezes to arrive")
        assert [e["payload"]["pattern"] for e in events] == ["duress", "plain"]       # duress first
        assert all(e["payload"]["buffered"] is True for e in events)
        assert all(e["payload"]["challenge_id"] == challenge for e in events)       # what the device showed
        assert events[0]["result"]["state_public"] == "ARMED"             # masked: reads like a plain squeeze
        s = _q(srv.db_url, "SELECT state, public_state FROM sessions WHERE id = %s", sid)[0]
        assert s["state"] == "ESCALATED_L1" or s["state"] == "ESCALATED_FINAL"
        assert s["public_state"] in ("ARMED", "PROMPTED")
        alert = _q(srv.db_url, """SELECT kind, masked, status FROM alerts WHERE session_id = %s AND NOT cover
                                   AND status IN ('OPEN', 'ACKED')""", sid)
        assert alert == [{"kind": "DURESS", "masked": True, "status": "OPEN"}]  # the late squeeze stood nothing down
        assert bridge.close() == 0
    finally:
        if bridge is not None and bridge.proc.poll() is None:
            bridge.proc.kill()
        srv.stop()
        print(srv.log_text()[-3000:])
