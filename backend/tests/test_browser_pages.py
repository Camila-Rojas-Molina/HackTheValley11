"""Browser tests of the two pages, with Playwright for Python driving the installed Google Chrome
(channel="chrome"; no browser download), against a live server (scripts/run.py) on its own database.

c.html (the contact and judge page):
  consent screen -> join (the tap also unlocks the looping alarm, muted) -> waiting for approval -> calm
  -> red ringing alert with "I'm on it" / "Can't help – pass it on" -> ack -> follow-up buttons ->
  "she's safe" -> calm; and the FINAL step with its "Emergency services: SIMULATED" banner; a used invite.
hr_bridge.html:
  SIM mode posts badged simulated heart rate to POST /v1/hr with the wearer key.

What a desktop browser cannot show: iOS Safari's mute switch and audio session, a phone speaker, and Web
Bluetooth to HeartCast on an iPhone. Skipped (with the reason) when Chrome cannot be launched; the pages'
JavaScript still runs in Node in tests/test_pages_js.py.
"""
from __future__ import annotations

import json
import time

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.livesrv import ADMIN, KEYS, WEARER, LiveServer, wait_for

pytestmark = pytest.mark.ops

AUDIO_HOOK = """(() => { const A = window.Audio; window.__audios = [];
  window.Audio = function (...args) { const a = new A(...args); window.__audios.push(a); return a; };
  window.Audio.prototype = A.prototype; })();"""


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is not installed (.venv/bin/pip install -r backend/requirements.txt)")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=True, timeout=60_000)
        except Exception as exc:  # no Google Chrome on this machine
            pytest.skip(f"Google Chrome could not be launched by Playwright: {type(exc).__name__}")
        yield b
        b.close()


@pytest.fixture(scope="module")
def live(pg_base_uri, tmp_path_factory):
    srv = LiveServer(pg_base_uri, "stillhere_browser", tmp_path_factory.mktemp("browser"), log_level="warning")
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 430, "height": 932})
    ctx.add_init_script(AUDIO_HOOK)
    pg = ctx.new_page()
    pg.set_default_timeout(20_000)
    yield pg
    ctx.close()


def _q(srv, sql, *args):
    with psycopg.connect(srv.db_url, row_factory=dict_row) as conn:
        return list(conn.execute(sql, args).fetchall())


def _state(srv, sid) -> str:
    return _q(srv, "SELECT state FROM sessions WHERE id = %s", sid)[0]["state"]


def _escalate_to_l1(c, srv, sid) -> None:
    assert c.post("/v1/demo/checkin-now", headers=ADMIN).json()["state"] == "PROMPTED"
    assert c.post("/v1/demo/fast-forward", headers=ADMIN).json()["fired_next"] == "PROMPT_TIMEOUT"
    wait_for(lambda: _state(srv, sid) == "REMINDED", 15, "REMINDED")
    assert c.post("/v1/demo/fast-forward", headers=ADMIN).json()["fired_next"] == "REMINDER_TIMEOUT"
    wait_for(lambda: _state(srv, sid) == "ESCALATED_L1", 15, "ESCALATED_L1")


def _join_on_the_page(page, c, name: str) -> dict:
    inv = c.post("/v1/contacts", json={"name": ""}, headers=WEARER).json()
    page.goto(inv["web_link"])
    page.wait_for_selector("#join")
    body = page.inner_text("body")
    assert "invited you to be a trusted contact" in body and "You'll see nothing unless" in body
    page.fill("#name", name)
    page.click("#join")                                   # consent tap: join + unlock the alarm
    page.wait_for_function("document.body.innerText.includes('Waiting for')")
    page.wait_for_function("document.body.innerText.includes('Alarm sound on')")
    assert page.evaluate("window.__audios.map(a => ({loop: a.loop, muted: a.muted, paused: a.paused}))") == [
        {"loop": True, "muted": True, "paused": False}]  # looping, silent until an alert
    assert "/c/" in page.url and inv["web_link"].rsplit("/", 1)[1] not in page.url  # the invite token is gone
    return inv


def test_contact_page_consent_join_alert_ack_follow_up_and_safe(live, page):
    with live.client() as c:
        sid = c.post("/v1/demo/reset", headers=ADMIN, json={}).json()["armed"]["session_id"]
        inv = _join_on_the_page(page, c, "Judge")
        try:
            assert _q(live, "SELECT name, approved_at FROM contacts WHERE id = %s", inv["contact_id"])[0]["name"] == "Judge"
            assert c.post(f"/v1/contacts/{inv['contact_id']}/approve", headers=WEARER).status_code == 200
            page.wait_for_function("document.body.innerText.includes('trusted contact')")
            assert not page.evaluate("document.body.classList.contains('alarm')")      # nothing before an alert

            _escalate_to_l1(c, live, sid)
            page.wait_for_function("document.body.classList.contains('alarm') && document.body.classList.contains('pulse')")
            assert page.title() == "ALERT – StillHere"
            assert page.evaluate("window.__audios.map(a => [a.muted, a.paused])") == [[False, False]]   # it rings
            assert page.eval_on_selector_all("button[data-x]", "bs => bs.map(b => b.dataset.x)") == ["ack", "pass"]
            assert "hasn't responded" in page.inner_text("h1") and "not a confirmed emergency" in page.inner_text("h1")
            assert "Last known location" in page.inner_text("body")

            page.click("button[data-x=ack]")                                         # "I'm on it"
            page.wait_for_selector("button[data-x=safe]")
            assert page.eval_on_selector_all("button[data-x]", "bs => bs.map(b => b.dataset.x)") == ["safe", "cant_reach"]
            assert page.evaluate("window.__audios[0].muted") is True                   # taken on: quiet
            assert _state(live, sid) == "ACKED"                                        # an ack alone closes nothing

            page.click("button[data-x=safe]")                                        # "I'm with her – she's safe"
            page.wait_for_function("!document.body.classList.contains('alarm')")
            page.wait_for_function("document.body.innerText.includes('trusted contact')")
            assert _state(live, sid) == "ARMED"
            assert _q(live, "SELECT status, resolved_by FROM alerts WHERE session_id = %s", sid) == [
                {"status": "RESOLVED", "resolved_by": f"contact:{inv['contact_id']}"}]
        finally:
            c.delete(f"/v1/contacts/{inv['contact_id']}", headers=WEARER)


def test_contact_page_final_step_shows_the_simulated_banner(live, page):
    with live.client() as c:
        sid = c.post("/v1/demo/reset", headers=ADMIN, json={}).json()["armed"]["session_id"]
        inv = _join_on_the_page(page, c, "Sam")
        try:
            c.post(f"/v1/contacts/{inv['contact_id']}/approve", headers=WEARER)
            page.wait_for_function("document.body.innerText.includes('trusted contact')")
            _escalate_to_l1(c, live, sid)
            page.wait_for_selector("button[data-x=pass]")
            page.click("button[data-x=pass]")                     # the only contact passes it on: FINAL
            wait_for(lambda: _state(live, sid) == "ESCALATED_FINAL", 15, "ESCALATED_FINAL")
            page.wait_for_selector(".banner[role=alert]")
            banner = page.inner_text(".banner")
            assert banner.startswith("Emergency services: SIMULATED") and "would contact 911" in banner
            assert page.text_content(".kicker") == "Final step"            # shown upper-case by CSS
            assert page.evaluate("document.body.classList.contains('alarm')")
            assert _q(live, """SELECT detail FROM audit_log WHERE session_id = %s
                                AND kind = 'emergency_services_simulated'""", sid)[0]["detail"]["placed"] is False
            page.click("button[data-x=ack]")
            page.wait_for_selector("button[data-x=safe]")
            page.click("button[data-x=safe]")
            page.wait_for_function("!document.body.classList.contains('alarm')")
            assert page.locator(".banner").count() == 0
            assert _state(live, sid) == "ARMED"
        finally:
            c.delete(f"/v1/contacts/{inv['contact_id']}", headers=WEARER)


def test_contact_page_turns_down_a_used_invite(live, page):
    with live.client() as c:
        inv = c.post("/v1/contacts", json={"name": "Jo"}, headers=WEARER).json()
        tok = inv["web_link"].rsplit("/", 1)[1]
        assert c.post(f"/v1/contact/{tok}/join", json={}).status_code == 200
        try:
            page.goto(inv["web_link"])
            page.wait_for_function("document.body.innerText.includes('This invite link has expired')")
            assert page.locator("#join").count() == 0
        finally:
            c.delete(f"/v1/contacts/{inv['contact_id']}", headers=WEARER)


def test_hr_bridge_sim_mode_posts_badged_simulated_heart_rate(live, page):
    page.goto(f"{live.base}/hr-bridge")
    page.wait_for_selector("#sim")
    assert page.evaluate("window.isSecureContext") is True                        # Web Bluetooth needs it
    page.fill("#key", KEYS["WEARER_KEY"])
    page.dispatch_event("#key", "change")
    with page.expect_request(lambda r: r.url.endswith("/v1/hr") and r.method == "POST") as req:
        page.click("#sim")
    posted = json.loads(req.value.post_data)
    assert posted["simulated"] is True and posted["samples"] and 20 <= posted["samples"][0]["bpm"] <= 250
    assert req.value.headers["authorization"] == f"Bearer {KEYS['WEARER_KEY']}"
    page.wait_for_function("Number(document.querySelector('#n-sent').textContent) >= 3", timeout=30_000)
    assert page.inner_text("#badge") == "SIMULATED SIGNAL"
    page.click("#sim")                                                            # stop
    time.sleep(0.5)
    rows = _q(live, """SELECT count(*) AS n, bool_and(simulated) AS sim, min(source) AS source FROM hr_samples h
                        JOIN users u ON u.id = h.user_id WHERE u.is_demo AND h.source = %s""", posted["source"])[0]
    assert rows["n"] >= 3 and rows["sim"] is True
