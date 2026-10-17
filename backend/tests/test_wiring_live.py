"""Wiring check: the real app (scripts/run.py) on a fresh database, every migration applied, no Telegram
token and no Vultr key. Each path is followed end to end through the running process:

* every router is mounted, and the mounted routes match docs/API.md in both directions;
* /c/{token} and /hr-bridge serve their pages;
* the timer worker, outbox sender and Telegram poller start without keys (log channel), and the timer
  worker fires a deadline on its own;
* POST /v1/hr writes hr_samples, /v1/hr/calibrate sets the baseline, and the heart-rate rule running in
  the timer worker brings the check-in forward (PULL_FORWARD) and never opens an alert;
* POST /v1/telemetry/batch ingests, and /v1/telemetry/zscore and /v1/telemetry/aggregates return data;
* the PointClickCare seed -> /v1/care/alerts -> each briefing is the vultr.briefing_text template fallback;
* scripts/bridge.py turns device lines into stored device events and answers with C lines.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from app import care, vultr
from tests.contract_routes import mounted_routes, served_routes
from tests.livesrv import ADMIN, BACKEND, KEYS, SCRIPTS, WEARER, LiveServer, device_headers, wait_for

pytestmark = pytest.mark.ops
REPO = BACKEND.parent


@pytest.fixture(scope="module")
def live(pg_base_uri, tmp_path_factory):
    srv = LiveServer(pg_base_uri, "stillhere_wiring", tmp_path_factory.mktemp("wiring")).start()
    try:
        yield srv
    finally:
        srv.stop()
        print(srv.log_text()[-3000:])


def _q(srv: LiveServer, sql: str, *args) -> list[dict]:
    with psycopg.connect(srv.db_url, row_factory=dict_row) as conn:
        return list(conn.execute(sql, args).fetchall())


def _demo_session(c) -> str:
    r = c.post("/v1/demo/reset", headers=ADMIN, json={})
    assert r.status_code == 200, r.text
    return r.json()["armed"]["session_id"]


def _state(srv, sid) -> str:
    return _q(srv, "SELECT state FROM sessions WHERE id = %s", sid)[0]["state"]


def test_every_migration_applied_and_every_router_mounted_and_documented(live):
    applied = [r["name"] for r in _q(live, "SELECT name FROM schema_migrations ORDER BY name")]
    files = sorted(p.name for p in (BACKEND / "migrations").glob("*.sql"))
    assert applied == files
    served = served_routes()
    with live.client() as c:
        spec = c.get("/openapi.json").json()
    live_api = {(m.upper(), p) for p, ops in spec["paths"].items() for m in ops}
    assert live_api == {r for r in served if not r[1].startswith(("/openapi.json", "/docs", "/redoc"))}
    modules = set(mounted_routes().values())
    for router in ("health", "device", "hr", "sessions", "contacts", "demo", "telemetry", "care"):
        assert f"app.routes.{router}" in modules, f"router app/routes/{router}.py is not mounted"
    doc = (REPO / "docs" / "API.md").read_text(encoding="utf-8")
    documented = {(m, p.split("?")[0]) for m, p in re.findall(r"`(GET|POST|PUT|DELETE|PATCH) (/[^`\s]*)`", doc)}
    assert sorted(served - documented) == [], "served but not in docs/API.md"
    assert sorted(documented - served) == [], "in docs/API.md but not served"


def test_contact_page_and_heart_rate_bridge_are_served(live):
    with live.client() as c:
        page = c.get("/c/some-invite-token")
        assert page.status_code == 200 and "StillHere contact" in page.text and "/v1/contact/" in page.text
        bridge = c.get("/hr-bridge")
        assert bridge.status_code == 200 and 'id="sim"' in bridge.text and "heart_rate_measurement" in bridge.text
        assert c.get("/docs").status_code == 200


def test_the_three_workers_run_without_any_keys(live):
    log = live.log_text()
    assert "timer worker started" in log
    assert "outbox sender started (channel: log)" in log
    assert "telegram poller idle: TELEGRAM_BOT_TOKEN is not set" in log
    with live.client() as c:
        health = c.get("/v1/health").json()
        assert health["telegram"] == "off (log channel)" and health["db"] == "up"
        sid = _demo_session(c)
        assert c.post("/v1/demo/checkin-now", headers=ADMIN).json()["state"] == "PROMPTED"
    # The outbox sender delivers the wearer's "Reminder" on the log channel ...
    sent = wait_for(lambda: _q(live, """SELECT status, channel, sent_at FROM outbox WHERE session_id = %s
                                         AND template = 'reminder' AND status = 'sent'""", sid), 10, "the reminder")
    assert sent[0]["channel"] == "log"
    # ... which starts the prompt window; the timer worker then fires the deadline on its own (12 s demo).
    wait_for(lambda: _state(live, sid) == "REMINDED", 25, "PROMPT_TIMEOUT to fire without fast-forward")
    fired = _q(live, """SELECT fired_at, fire_at, result FROM timers WHERE session_id = %s
                         AND kind = 'PROMPT_TIMEOUT' AND fired_at IS NOT NULL""", sid)
    assert len(fired) == 1 and (fired[0]["fired_at"] - fired[0]["fire_at"]).total_seconds() < 3
    with live.client() as c:
        _demo_session(c)


def test_heart_rate_ingest_feeds_the_rule_which_only_pulls_forward(live):
    with live.client() as c:
        sid = _demo_session(c)
        demo_user = _q(live, "SELECT id FROM users WHERE is_demo")[0]["id"]
        now = time.time()
        seated = [{"t": now - 55 + 5 * i, "bpm": 60} for i in range(12)]
        r = c.post("/v1/hr", headers=WEARER, json={"source": "wiring", "simulated": False, "samples": seated})
        assert r.json() == {"ok": True, "inserted": 12}
        assert _q(live, "SELECT count(*) AS n FROM hr_samples WHERE user_id = %s AND source = 'wiring'",
                  demo_user)[0]["n"] == 12
        cal = c.post("/v1/hr/calibrate", headers=WEARER, json={"window_s": 120})
        assert cal.status_code == 200 and cal.json()["baseline_bpm"] == 60.0, cal.text
        seq = 0
        deadline = time.monotonic() + 45
        while _state(live, sid) == "ARMED" and time.monotonic() < deadline:
            seq += 1
            c.post("/v1/device/heartbeat", headers=device_headers("hr", seq), json={"enmo60_mg": 5})   # still
            c.post("/v1/hr", headers=WEARER, json={"bpm": 130, "source": "wiring", "simulated": False})
            time.sleep(1)
        assert _state(live, sid) == "PROMPTED", "the heart-rate rule never brought the check-in forward"
        pf = _q(live, """SELECT actor, detail FROM audit_log WHERE session_id = %s AND kind = 'pull_forward'""", sid)
        assert [p["actor"] for p in pf] == ["rule:hr"]
        assert pf[0]["detail"]["reason"] == "hr_different_from_usual" and pf[0]["detail"]["n"] in (1, 3, 4)
        s = _q(live, "SELECT challenge_n, challenge_id, prompt_cause FROM sessions WHERE id = %s", sid)[0]
        assert s["prompt_cause"] == "challenge"
        seq += 1
        ans = c.post("/v1/device/events", headers=device_headers("hr", seq), json={
            "event_id": "hr:answer", "type": "squeeze",
            "payload": {"pattern": "plain", "k": s["challenge_n"], "challenge_id": s["challenge_id"]}})
        assert ans.json()["result"] == "noted" and ans.json()["state_public"] == "ARMED"
        for _ in range(6):                                   # still different from usual: the cooldown holds
            seq += 1
            c.post("/v1/device/heartbeat", headers=device_headers("hr", seq), json={"enmo60_mg": 5})
            c.post("/v1/hr", headers=WEARER, json={"bpm": 135, "source": "wiring"})
            time.sleep(1)
        assert _state(live, sid) == "ARMED"
        assert not _q(live, "SELECT 1 FROM alerts WHERE session_id = %s", sid)
        assert not _q(live, "SELECT 1 FROM outbox WHERE session_id = %s AND recipient = 'contact'", sid)
        _demo_session(c)


def test_telemetry_batch_then_zscore_and_aggregates_return_data(live):
    with live.client() as c:
        _demo_session(c)
        demo_user = str(_q(live, "SELECT id FROM users WHERE is_demo")[0]["id"])
        seeded = c.post("/v1/telemetry/synthetic", headers=ADMIN, json={
            "days": 0.1, "until_minutes_ago": 6, "hr_every_s": 60, "motion_every_s": 60})
        assert seeded.status_code == 200 and seeded.json()["simulated"] is True, seeded.text
        rows = [[90_000 + 20 * i, 40 + i % 7, -30, 1100 + i % 50, 300, 310] for i in range(3000)]
        r = c.post("/v1/telemetry/batch", headers=device_headers("tel", 1), json={"batch_id": "wiring-1", "rows": rows})
        assert r.json() == {"ok": True, "duplicate": False, "inserted": 3000, "dropped": 0}
        c.post("/v1/hr", headers=WEARER, json={"bpm": 75, "source": "wiring"})
        agg = c.get("/v1/telemetry/aggregates", headers=WEARER, params={"series": "motion", "resolution": "1m",
                                                                         "hours": 1}).json()
        assert agg["user_id"] == demo_user and sum(b["n"] for b in agg["buckets"]) >= 3000
        z = c.get("/v1/telemetry/zscore", headers=WEARER).json()
        metrics = {row["metric"]: row for row in z["rows"]}
        assert set(metrics) == {"hr_bpm", "motion_enmo_mg"}, z
        assert metrics["motion_enmo_mg"]["z"] is not None and metrics["motion_enmo_mg"]["baseline_minutes"] >= 30
        assert "never changes a check-in session" in z["note"]
        info = c.get("/v1/telemetry/info", headers=ADMIN).json()
        assert info["mode"] in ("plain", "timescaledb")


def test_pcc_seed_alerts_and_the_template_briefing(live):
    with live.client() as c:
        seed = c.post("/v1/demo/care-seed", headers=ADMIN, json={})
        assert seed.status_code == 200 and seed.json()["residents"] == 20
        cards = c.get("/v1/care/alerts").json()["alerts"]
    assert cards and all(card["level"] in care.FLAGGED + (care.LEVEL_NO_DATA,) for card in cards)
    no_key = {"VULTR_API_KEY": ""}
    assert vultr.status(no_key)["enabled"] is False
    for card in cards:
        assert card["briefing_source"] == "template"
        assert card["briefing"] == care.local_briefing(card)
        text = vultr.briefing_text(card, env=no_key)          # the shared PCC contract, no key: the template
        assert text == vultr.template_briefing(card) and card["display_name"] in text


def test_the_serial_bridge_turns_device_lines_into_events(live):
    with live.client() as c:
        sid = _demo_session(c)
    lines = ["H,wire,1,10,0,400,0", 'E,wire:2,squeeze,{"pattern":"plain","k":2,"device_ms":1000}',
             "E,wire:3,head_position_change,{}", "H,wire,4,10,0,400,0"]
    r = subprocess.run([sys.executable, str(SCRIPTS / "bridge.py"), "--stdin", "--url", live.base,
                        "--device-id", "scrunchie-1", "--telemetry", "off"], input="\n".join(lines) + "\n",
                       capture_output=True, text=True, cwd=BACKEND, timeout=60,
                       env={**live.env, "DEVICE_KEY": KEYS["DEVICE_KEY"]})
    assert r.returncode == 0, r.stderr
    c_lines = [ln for ln in r.stdout.splitlines() if ln.startswith("C,")]
    assert c_lines and c_lines[-1].startswith("C,amber,"), r.stdout        # head position: an early check-in
    stored = _q(live, """SELECT event_id, type, device_ms, result FROM device_events WHERE device_id = 'scrunchie-1'
                          AND event_id LIKE 'wire:%%' ORDER BY event_id""")
    assert [(e["event_id"], e["type"]) for e in stored] == [("wire:2", "squeeze"), ("wire:3", "head_position_change")]
    assert stored[0]["device_ms"] == 1000 and stored[0]["result"]["result"] == "noted"
    assert stored[1]["result"]["result"] == "prompted"
    assert _state(live, sid) == "PROMPTED"
    hb = _q(live, "SELECT boot_id, last_seq FROM devices WHERE id = 'scrunchie-1'")[0]
    assert hb["boot_id"] == "wire"
    with live.client() as c:
        _demo_session(c)


def test_the_running_servers_access_log_never_holds_a_capability_token(live):
    """Review r1 #12, on the real process: contact, invite and location tokens travel in URL paths and must
    not reach uvicorn's access log."""
    with live.client() as c:
        sid = _demo_session(c)
        inv = c.post("/v1/contacts", headers=WEARER, json={"name": "Log"}).json()
        invite = inv["web_link"].rsplit("/", 1)[1]
        c.get(f"/c/{invite}")
        c.get(f"/v1/contact/{invite}/invite")
        token = c.post(f"/v1/contact/{invite}/join", json={}).json()["contact_token"]
        c.post(f"/v1/contacts/{inv['contact_id']}/approve", headers=WEARER)
        c.post("/v1/demo/checkin-now", headers=ADMIN)
        c.post("/v1/demo/fast-forward", headers=ADMIN)
        wait_for(lambda: _state(live, sid) == "REMINDED", 15, "REMINDED")
        c.post("/v1/demo/fast-forward", headers=ADMIN)
        wait_for(lambda: _state(live, sid) == "ESCALATED_L1", 15, "ESCALATED_L1")
        link = c.get(f"/v1/contact/{token}/status").json()["alert"]["location"]["link"]
        share = link.rsplit("/l/", 1)[1]
        assert c.get(f"/l/{share}").status_code == 200
        c.post(f"/v1/contact/{token}/ack", json={})
        c.delete(f"/v1/contacts/{inv['contact_id']}", headers=WEARER)
        _demo_session(c)
    time.sleep(0.5)
    log = live.log_text()
    assert '"GET /c/*** HTTP/1.1" 200' in log and "/v1/contact/***/status" in log and "GET /l/*** " in log
    for secret in (invite, token, share):
        assert secret not in log
