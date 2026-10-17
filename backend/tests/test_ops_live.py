"""The demo-night paths, run for real (BUILD_PLAN §6.9 row 1, §8; APPROVED_PLAN "Also in"):

* scripts/simulate.py: all seven KAZE scenarios plus the PCC seed against a real server (scripts/run.py)
* scripts/bridge.py --stdin: RECORD-mode R lines land in scrunchie_telemetry through the real API
* scripts/serve.sh + scripts/chaos.sh mid-reminded: kill -9 the listener mid-REMINDED; the restart loop
  brings the server back and the stored deadline fires L1 within 2 s, with exactly one message
* c.html and hr_bridge.html in headless Chrome (skipped without Chrome)

Each starts its own server on a free port with its own database. Marked `ops` (minutes) and `chaos` (kills a
process); the fast suite is `pytest -m "not chaos and not ops"`.
"""
from __future__ import annotations

import json
import os
import shutil
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
SCRIPTS = BACKEND / "scripts"
KEYS = {"ADMIN_KEY": "ops-admin", "DEVICE_KEY": "ops-device", "WEARER_KEY": "ops-wearer", "DEMO_PIN": "2468",
        "DEMO_DURESS_PIN": "1357", "APP_SECRET": "ops-secret"}
ADMIN = {"X-Admin-Key": KEYS["ADMIN_KEY"]}
WEARER = {"Authorization": f"Bearer {KEYS['WEARER_KEY']}"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _env(url: str, port: int) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("STILLHERE_TSDB", "PYTEST"))}
    env.update(KEYS)
    env.update({"DATABASE_URL": url, "HOST": "127.0.0.1", "PORT": str(port), "PUBLIC_BASE_URL": f"http://127.0.0.1:{port}",
                "RUN_WORKERS": "1", "AUTO_MIGRATE": "1", "DEMO_SEED": "1", "TELEGRAM_BOT_TOKEN": "",
                "STILLHERE_PROFILE": "demo", "STILLHERE_URL": f"http://127.0.0.1:{port}", "PYTHON": sys.executable,
                "LOG_LEVEL": "warning"})
    return env


def _wait_healthy(base: str, timeout: float = 40) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/v1/health", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"{base} did not become healthy")


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)


def _listener_pids(port: int) -> list[int]:
    r = subprocess.run(["lsof", "-tiTCP:%d" % port, "-sTCP:LISTEN"], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split()]


@pytest.fixture(scope="module")
def live(pg_base_uri, tmp_path_factory):
    """scripts/run.py on a free port with its own database; yields (base_url, db_url, env)."""
    url = fresh_database(pg_base_uri, "stillhere_ops_live")
    port = _free_port()
    env = _env(url, port)
    log = tmp_path_factory.mktemp("ops") / "server.log"
    with open(log, "ab") as fh:
        proc = subprocess.Popen([sys.executable, str(SCRIPTS / "run.py")], cwd=BACKEND, env=env, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)
    try:
        _wait_healthy(env["STILLHERE_URL"])
        yield env["STILLHERE_URL"], url, env
    finally:
        _stop(proc)
        print(log.read_text()[-3000:] if log.exists() else "")


@pytest.mark.ops
def test_bridge_stdin_forwards_record_mode_lines_into_the_hypertable(live):
    base, url, env = live
    with httpx.Client(base_url=base, timeout=10) as c:
        assert c.post("/v1/demo/reset", headers=ADMIN, json={}).status_code == 200      # demo profile: debug
    lines = ["H,ops1,1,10,0,400,0"] + [f"R,{90_000 + 20 * i},{i % 7},{-(i % 5)},1000,300,{310 + i % 3}"
                                       for i in range(120)]
    r = subprocess.run([sys.executable, str(SCRIPTS / "bridge.py"), "--stdin", "--url", base,
                        "--device-id", "scrunchie-1"], input="\n".join(lines) + "\n", capture_output=True,
                       text=True, cwd=BACKEND, env={**env, "DEVICE_KEY": KEYS["DEVICE_KEY"]}, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0].startswith("C,off,0,")           # the heartbeat reply went back as a C line
    with psycopg.connect(url, row_factory=dict_row) as conn:
        got = conn.execute(
            """SELECT count(*) AS n, min(device_id) AS dev, bool_or(simulated) AS sim, min(profile) AS p
                 FROM scrunchie_telemetry t JOIN users u ON u.id = t.user_id WHERE u.is_demo""").fetchone()
    assert (got["n"], got["dev"], got["sim"], got["p"]) == (120, "scrunchie-1", False, "night_out")


@pytest.mark.ops
def test_simulate_runs_all_seven_kaze_scenarios_and_the_pcc_seed(live):
    base, url, env = live
    r = subprocess.run([sys.executable, str(SCRIPTS / "simulate.py")], capture_output=True, text=True, cwd=BACKEND,
                       env=env, timeout=600)
    tail = r.stdout[-4000:]
    assert r.returncode == 0, tail + r.stderr[-2000:]
    assert "8/8 steps passed" in r.stdout, tail
    for step in ("1", "2", "3", "4", "5", "6", "7", "pcc"):
        assert f"  PASS  {step} " in r.stdout, (step, tail)


@pytest.mark.chaos
@pytest.mark.skipif(sys.platform == "win32", reason="serve.sh and chaos.sh need bash and lsof")
def test_serve_sh_restarts_after_kill9_and_chaos_mid_reminded_passes(pg_base_uri, tmp_path):
    url = fresh_database(pg_base_uri, "stillhere_ops_serve")
    port = _free_port()
    env = _env(url, port)
    base = env["STILLHERE_URL"]
    log = tmp_path / "serve.log"
    with open(log, "ab") as fh:
        serve = subprocess.Popen(["bash", str(SCRIPTS / "serve.sh")], cwd=BACKEND, env=env, stdout=fh,
                                 stderr=subprocess.STDOUT, start_new_session=True)
    try:
        _wait_healthy(base)
        first = _listener_pids(port)
        assert first and serve.pid not in first                    # the loop is not the listener
        with httpx.Client(base_url=base, timeout=10) as c:            # Contact #1 for the demo wearer
            inv = c.post("/v1/contacts", json={"name": "Sam"}, headers=WEARER).json()
            tok = inv["web_link"].rsplit("/", 1)[1]
            assert c.post(f"/v1/contact/{tok}/join", json={}).status_code == 200
            assert c.post(f"/v1/contacts/{inv['contact_id']}/approve", headers=WEARER).status_code == 200
        r = subprocess.run(["bash", str(SCRIPTS / "chaos.sh"), "mid-reminded", str(port)], capture_output=True,
                           text=True, env=env, timeout=240)
        assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
        assert "chaos: PASS kill -9 mid-REMINDED" in r.stdout and "-> ESCALATED_L1, 1 message(s)" in r.stdout, r.stdout
        assert "server is down; waiting for serve.sh to restart it" in r.stdout
        assert serve.poll() is None                                   # the loop survived
        _wait_healthy(base)
        second = _listener_pids(port)
        assert second and set(second).isdisjoint(first)               # a fresh server process
    finally:
        _stop(serve)
        for pid in _listener_pids(port):                               # nothing left listening
            os.kill(pid, signal.SIGKILL)
        text = log.read_text() if log.exists() else ""
        print(text[-3000:])
    assert "server exited with code" in text and "restarting in 1 s" in text


# --- the two browser pages in a real browser (headless Chrome over the DevTools protocol) -------------------
# What this covers: c.html's consent tap, the looping alarm started muted inside it, polling, the red ringing
# alert, "I'm on it" and "she's safe"; hr_bridge.html's SIM fallback posting to /v1/hr. What it cannot: iOS
# Safari's mute switch and audio session, a phone's speaker, and Web Bluetooth to HeartCast on an iPhone.
CHROME = next((c for c in (os.environ.get("STILLHERE_CHROME"), shutil.which("google-chrome"), shutil.which("chromium"),
                           "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome") if c and os.path.exists(c)), None)
needs_chrome = pytest.mark.skipif(CHROME is None, reason="Chrome is not installed (set STILLHERE_CHROME)")

# Records every Audio() the page makes, so the test can read muted / paused.
AUDIO_HOOK = """(() => { const A = window.Audio; window.__audios = [];
  window.Audio = function (...args) { const a = new A(...args); window.__audios.push(a); return a; };
  window.Audio.prototype = A.prototype; })();"""


class _Page:
    def __init__(self, ws_url: str):
        from websockets.sync.client import connect
        self._conn = connect(ws_url, max_size=None, open_timeout=10)
        self.ws = self._conn.__enter__()
        self.n = 0

    def call(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=30))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise AssertionError(msg["error"])
                return msg.get("result", {})

    def js(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise AssertionError(r["exceptionDetails"])
        return r["result"].get("value")

    def wait(self, expr: str, timeout: float, what: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = self.js(expr)
            if v:
                return v
            time.sleep(0.2)
        raise AssertionError(f"timed out waiting for {what}: {self.js('document.body.innerText')[:500]}")

    def tap(self, selector: str) -> None:
        """A trusted click (counts as a user gesture, which the audio unlock needs)."""
        x, y = self.wait(f"""(() => {{ const e = document.querySelector({json.dumps(selector)});
            if (!e) return null; e.scrollIntoView({{block: 'center'}}); const r = e.getBoundingClientRect();
            return [r.x + r.width / 2, r.y + r.height / 2]; }})()""", 10, selector)
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)

    def close(self):
        self._conn.__exit__(None, None, None)


@pytest.fixture
def chrome(tmp_path):
    profile = tmp_path / "chrome-profile"
    proc = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                             "--no-first-run", "--no-default-browser-check", "--disable-gpu",
                             "--window-size=430,932", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 30
        while not (port_file.exists() and port_file.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.1)
        port = int(port_file.read_text().split()[0])
        pages = [t for t in httpx.get(f"http://127.0.0.1:{port}/json", timeout=5).json() if t["type"] == "page"]
        page = _Page(pages[0]["webSocketDebuggerUrl"])
        page.call("Page.enable")
        page.call("Runtime.enable")
        page.call("Page.addScriptToEvaluateOnNewDocument", source=AUDIO_HOOK)
        yield page
        page.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _state(c, sid):
    return next((s["state"] for s in c.get("/v1/admin/overview", headers=ADMIN).json()["sessions"] if s["id"] == sid), None)


@pytest.mark.ops
@needs_chrome
def test_contact_page_joins_rings_red_on_an_alert_and_takes_both_taps_in_chrome(live, chrome):
    base, url, env = live
    with httpx.Client(base_url=base, timeout=10) as c:
        sid = c.post("/v1/demo/reset", headers=ADMIN, json={}).json()["armed"]["session_id"]
        inv = c.post("/v1/contacts", json={"name": ""}, headers=WEARER).json()
        try:
            chrome.call("Page.navigate", url=inv["web_link"])
            chrome.wait("!!document.querySelector('#join')", 15, "the consent screen")
            assert "nothing unless" in chrome.js("document.body.innerText")
            chrome.js("document.querySelector('#name').value = 'Judge'")
            chrome.tap("#join")                                         # consent tap: join + unlock the alarm
            chrome.wait("document.body.innerText.includes('Waiting for')", 15, "the pending screen")
            chrome.wait("document.body.innerText.includes('Alarm sound on')", 10, "the alarm unlock")
            audio = chrome.js("window.__audios.map(a => ({loop: a.loop, muted: a.muted, paused: a.paused}))")
            assert audio == [{"loop": True, "muted": True, "paused": False}]   # looping, silent, ready
            assert c.post(f"/v1/contacts/{inv['contact_id']}/approve", headers=WEARER).status_code == 200
            chrome.wait("document.body.innerText.includes('trusted contact')", 10, "the calm screen")
            assert not chrome.js("document.body.classList.contains('alarm')")

            assert c.post("/v1/demo/checkin-now", headers=ADMIN).json()["state"] == "PROMPTED"
            c.post("/v1/demo/fast-forward", headers=ADMIN)                      # -> REMINDED
            deadline = time.monotonic() + 15
            while _state(c, sid) != "REMINDED" and time.monotonic() < deadline:
                time.sleep(0.2)
            c.post("/v1/demo/fast-forward", headers=ADMIN)                      # -> ESCALATED_L1, Contact #1
            chrome.wait("document.body.classList.contains('alarm') && document.body.classList.contains('pulse')",
                        20, "the red alert")
            assert chrome.js("document.title") == "ALERT – StillHere"
            assert chrome.js("window.__audios.map(a => [a.muted, a.paused])") == [[False, False]]   # it rings
            assert chrome.js("[...document.querySelectorAll('button[data-x]')].map(b => b.dataset.x)") == ["ack", "pass"]
            chrome.tap("button[data-x=ack]")                                    # "I'm on it"
            chrome.wait("!!document.querySelector('button[data-x=safe]')", 10, "the follow-up buttons")
            assert chrome.js("window.__audios[0].muted") is True                # acknowledged: quiet
            assert _state(c, sid) == "ACKED"
            chrome.tap("button[data-x=safe]")                                   # "I'm with her – she's safe"
            chrome.wait("!document.body.classList.contains('alarm')", 10, "the calm screen after she's safe")
            assert _state(c, sid) == "ARMED"
        finally:
            c.delete(f"/v1/contacts/{inv['contact_id']}", headers=WEARER)       # silent; simulate.py needs none


@pytest.mark.ops
@needs_chrome
def test_hr_bridge_sim_fallback_posts_badged_simulated_heart_rate_in_chrome(live, chrome):
    base, url, env = live
    chrome.call("Page.navigate", url=f"{base}/hr-bridge")
    chrome.wait("!!document.querySelector('#sim')", 15, "the heart-rate bridge")
    assert chrome.js("window.isSecureContext") is True                          # Web Bluetooth needs it
    chrome.js(f"""(() => {{ const k = document.querySelector('#key'); k.value = {json.dumps(KEYS['WEARER_KEY'])};
                 k.dispatchEvent(new Event('change')); }})()""")
    chrome.tap("#sim")
    chrome.wait("Number(document.querySelector('#n-sent').textContent) >= 3", 20, "three posted samples")
    assert chrome.js("document.querySelector('#badge').textContent") == "SIMULATED SIGNAL"
    chrome.tap("#sim")                                                           # stop
    with psycopg.connect(url, row_factory=dict_row) as conn:
        got = conn.execute(
            """SELECT count(*) AS n, bool_and(simulated) AS sim FROM hr_samples h JOIN users u ON u.id = h.user_id
                WHERE u.is_demo AND h.source = 'hr_bridge_sim'""").fetchone()
    assert got["n"] >= 3 and got["sim"] is True
