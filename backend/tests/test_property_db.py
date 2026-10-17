"""Property-based tests (Hypothesis) of the KAZE invariants through the real HTTP API and database.

Each example builds fresh worlds (wearer, device, approved contacts, an armed demo session) in a database of
its own, drives them with a random sequence of inputs and checks the stored result:

* sensor events, heart rate (through POST /v1/hr and the real rule evaluation), telemetry batches and the
  senior-mode seed never open an alert or move a session past PROMPTED;
* after any prefix, the real PIN and the duress PIN give byte-identical replies, and the wearer's next
  views (session status, device command, her messages, her timeline) match the real-PIN twin;
* during an escalation no squeeze, check-in, wrong PIN or out-of-turn contact tap closes the alert: only
  "she's safe" from the contact who took it on, or the real PIN, does.

Challenge counts come from ctx.choose, fixed here so twin worlds stay in step.
"""
from __future__ import annotations

import re

import httpx
import pytest
import pytest_asyncio
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from app import sensors, states
from app.config import Settings
from tests.conftest import (ADMIN_KEY, DURESS_PIN, PIN, fire, fresh_database, make_world, open_alert,
                            pending_timers, session_row)

PROP = settings(max_examples=25, deadline=None, derandomize=False,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture,
                                       HealthCheck.differing_executors])
ADMIN = {"X-Admin-Key": ADMIN_KEY}


def first(options):
    return list(options)[0]


@pytest.fixture(scope="module")
def prop_settings(pg_base_uri) -> Settings:
    url = fresh_database(pg_base_uri, "stillhere_property")
    return Settings(database_url=url, admin_key=ADMIN_KEY, device_key="prop-device-key", wearer_key="prop-wearer-key",
                    demo_pin=PIN, demo_duress_pin=DURESS_PIN, app_secret="prop-secret",
                    public_base_url="https://stillhere.test", run_workers=False, auto_migrate=True, demo_seed=True)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def papp(prop_settings):
    from app.main import create_app
    application = create_app(prop_settings, start_workers=False)
    async with application.router.lifespan_context(application):
        application.state.ctx.choose = first
        yield application


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def pclient(papp):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=papp), base_url="http://prop") as c:
        yield c


async def _end_all(db) -> None:
    """Keep the evaluated set small: earlier examples' sessions are over."""
    async with db.connection() as conn:
        await conn.execute("UPDATE sessions SET state = 'ENDED', public_state = 'ENDED' WHERE NOT EXISTS "
                           "(SELECT 1 FROM users u WHERE u.id = sessions.user_id AND u.is_demo)")
        await conn.execute("UPDATE timers SET cancelled_at = now() WHERE fired_at IS NULL AND cancelled_at IS NULL")


async def _q(db, sql, *args):
    async with db.connection() as conn:
        return list(await (await conn.execute(sql, args)).fetchall())


# --- 1. sensors, heart rate, telemetry: nothing past PROMPTED ------------------------------------------------
sensor_ops = st.one_of(
    st.tuples(st.just("event"), st.sampled_from(("head_position_change", "fall_like", "offbody", "prompt_shown"))),
    st.tuples(st.just("heartbeat"), st.floats(0, 400), st.booleans()),
    st.tuples(st.just("hr"), st.lists(st.integers(20, 250), min_size=1, max_size=12), st.booleans()),
    st.tuples(st.just("hr_deviating"), st.integers(100, 250)),
    st.tuples(st.just("hr_lost"), st.just(None)),
    st.tuples(st.just("telemetry"), st.integers(1, 40)),
    st.tuples(st.just("cooldown_over"), st.just(None)),
    st.tuples(st.just("answer"), st.just(None)),
)


@PROP
@given(ops=st.lists(sensor_ops, min_size=1, max_size=10))
async def test_no_sensor_heart_rate_or_telemetry_input_ever_escalates(pclient, papp, ops):
    db, ctx = papp.state.db, papp.state.ctx
    await _end_all(db)
    w = await make_world(pclient, db, contacts=1)
    async with db.connection() as conn:
        await conn.execute("""INSERT INTO user_baselines (user_id, kind, value, simulated, computed_at)
                              VALUES (%s, 'hr_rest', 60, false, now())""", (w.user_id,))
    await pclient.post("/v1/device/heartbeat", headers=w.device(), json={"enmo60_mg": 5})
    for i, op in enumerate(ops):
        kind = op[0]
        if kind == "event":
            r = await pclient.post("/v1/device/events", headers=w.device(),
                                   json={"event_id": f"p:{i}", "type": op[1], "payload": {}})
            assert r.status_code == 200 and r.json()["result"] in ("prompted", "ignored", "cooldown",
                                                                   "deadline_set"), r.text
        elif kind == "heartbeat":
            r = await pclient.post("/v1/device/heartbeat", headers=w.device(),
                                   json={"enmo60_mg": op[1], "offbody": op[2]})
            assert r.status_code == 200
        elif kind in ("hr", "hr_deviating"):
            bpms = op[1] if kind == "hr" else [op[1]] * 5
            r = await pclient.post("/v1/hr", headers=w.wearer, json={
                "source": "property", "simulated": kind == "hr" and op[2],
                "samples": [{"bpm": b} for b in bpms]})
            assert r.status_code == 200 and r.json()["inserted"] == len(bpms)
            if kind == "hr_deviating":  # held long enough while still: the rule may fire
                async with db.connection() as conn:
                    await conn.execute("UPDATE sessions SET hr_deviating_since = now() - interval '1 hour' "
                                       "WHERE id = %s", (w.session_id,))
                await pclient.post("/v1/device/heartbeat", headers=w.device(), json={"enmo60_mg": 5})
            await sensors.evaluate_hr(db, ctx, force=True)
        elif kind == "hr_lost":  # heart rate stopped arriving 10 minutes ago (> hr_lost_s)
            await pclient.post("/v1/hr", headers=w.wearer, json={"bpm": 61, "source": "property"})
            async with db.connection() as conn:
                await conn.execute("UPDATE sessions SET created_at = now() - interval '20 minutes' WHERE id = %s",
                                   (w.session_id,))
                await conn.execute("UPDATE hr_samples SET time = now() - interval '10 minutes' WHERE user_id = %s",
                                   (w.user_id,))
            await sensors.evaluate_hr(db, ctx, force=True)
        elif kind == "telemetry":
            rows = [[1000 + 20 * j, j % 9, -j % 7, 1000, 300, 310] for j in range(op[1])]
            r = await pclient.post("/v1/telemetry/batch", headers=w.device(), json={"batch_id": f"pb{i}", "rows": rows})
            assert r.status_code == 200 and r.json()["ok"] is True, r.text
        elif kind == "cooldown_over":
            async with db.connection() as conn:
                await conn.execute("UPDATE sessions SET pull_forward_last = '{}'::jsonb WHERE id = %s", (w.session_id,))
        elif kind == "answer":
            s = await session_row(db, w.session_id)
            if s["state"] == states.PROMPTED:
                r = await pclient.post("/v1/device/events", headers=w.device(), json={
                    "event_id": f"p:{i}", "type": "squeeze",
                    "payload": {"pattern": "plain", "k": s["challenge_n"], "challenge_id": s["challenge_id"]}})
                assert r.json()["result"] == "noted"
        s = await session_row(db, w.session_id)
        assert s["state"] in (states.ARMED, states.PROMPTED), (op, s["state"])
        assert s["public_state"] == s["state"]
        assert await open_alert(db, w.session_id) is None
        assert not await _q(db, "SELECT 1 FROM alerts WHERE session_id = %s", w.session_id)
        assert not await _q(db, "SELECT 1 FROM outbox WHERE session_id = %s AND recipient = 'contact'", w.session_id)
    moves = await _q(db, """SELECT actor, kind, from_state, to_state, detail FROM audit_log
                             WHERE session_id = %s AND from_state IS DISTINCT FROM to_state AND to_state IS NOT NULL""",
                     w.session_id)
    for m in moves:
        assert (m["from_state"], m["to_state"]) in ((None, "ARMED"), ("ARMED", "PROMPTED"), ("PROMPTED", "ARMED")), m
        if m["to_state"] == "PROMPTED":
            assert m["kind"] == "pull_forward", m
            event(f"brought forward by {m['actor']} ({m['detail'].get('reason')})")


@settings(max_examples=3, deadline=None, suppress_health_check=[HealthCheck.too_slow,
                                                                HealthCheck.function_scoped_fixture,
                                                                HealthCheck.differing_executors])
@given(seed=st.integers(0, 2 ** 31 - 1))
async def test_the_senior_mode_seed_and_reads_never_touch_a_kaze_session(pclient, papp, seed):
    db = papp.state.db
    w = await make_world(pclient, db, contacts=1)
    count_sql = """SELECT (SELECT count(*) FROM sessions) AS s, (SELECT count(*) FROM timers) AS t,
                          (SELECT count(*) FROM outbox) AS o, (SELECT count(*) FROM alerts) AS a,
                          (SELECT count(*) FROM audit_log) AS l,
                          (SELECT string_agg(state || public_state || version, ',' ORDER BY id) FROM sessions) AS st"""
    before = (await _q(db, count_sql))[0]
    r = await pclient.post("/v1/demo/care-seed", headers=ADMIN, json={"seed": seed, "end_day": "2026-10-16"})
    assert r.status_code == 200 and r.json()["synthetic"] is True
    for path in ("/v1/care/alerts", "/v1/care/residents", "/v1/care/digest", "/v1/care/feedback"):
        assert (await pclient.get(path)).status_code == 200
    assert (await _q(db, count_sql))[0] == before
    assert (await session_row(db, w.session_id))["state"] == states.ARMED


# --- 2. real PIN and duress PIN: byte-identical after any prefix ------------------------------------------
prefix_ops = st.lists(st.one_of(
    st.tuples(st.just("fire"), st.sampled_from((states.CHECKIN_DUE, states.PROMPT_TIMEOUT, states.REMINDER_TIMEOUT,
                                                states.ACK_TIMEOUT, states.FOLLOWUP_TIMEOUT, states.RENOTIFY))),
    st.tuples(st.just("squeeze"), st.sampled_from(("right", "wrong", "unrecognized"))),
    st.tuples(st.just("sensor"), st.sampled_from(("head_position_change", "offbody"))),
    st.tuples(st.just("ack"), st.integers(0, 1)),
    st.tuples(st.just("checkin"), st.just(None)),
), min_size=0, max_size=7)
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HM = re.compile(r"\b\d\d:\d\d\b")


def _norm(text: str) -> str:
    text = _ID.sub("<id>", text)
    text = re.sub(r'"challenge_id":"[0-9a-f]+"', '"challenge_id":"<c>"', text)
    text = re.sub(r'"server_time":"[^"]+"', '"server_time":"<t>"', text)
    text = re.sub(r'"contact_id":\d+', '"contact_id":<n>', text)
    # Each session's chain head hashes its own ids and times; twins are separate sessions.
    text = re.sub(r"Timeline head: [0-9a-f]+", "Timeline head: <head>", text)
    return _HM.sub("<hh:mm>", text)


async def _apply_prefix(client, db, ctx, w, ops, tag) -> None:
    for i, (kind, arg) in enumerate(ops):
        if kind == "fire":
            if any(t["kind"] == arg for t in await pending_timers(db, w.session_id)):
                await fire(db, ctx, w.session_id, arg)
        elif kind == "squeeze":
            s = await session_row(db, w.session_id)
            k = s["challenge_n"] if (arg == "right" and s["challenge_n"]) else 4 if arg == "wrong" else 2
            pattern = "unrecognized" if arg == "unrecognized" else "plain"
            r = await client.post("/v1/device/events", headers=w.device(),
                                  json={"event_id": f"{tag}:{i}", "type": "squeeze", "payload": {"pattern": pattern, "k": k}})
            assert r.status_code == 200, r.text
        elif kind == "sensor":
            r = await client.post("/v1/device/events", headers=w.device(),
                                  json={"event_id": f"{tag}:{i}", "type": arg, "payload": {}})
            assert r.status_code == 200, r.text
        elif kind == "ack":
            await client.post(f"/v1/contact/{w.contacts[arg]['token']}/ack", json={})
        elif kind == "checkin":
            r = await client.post(f"/v1/sessions/{w.session_id}/checkin", headers=w.wearer, json={})
            assert r.status_code == 200, r.text


async def _wearer_side(client, db, w) -> dict:
    status = await client.get(f"/v1/sessions/{w.session_id}", headers=w.wearer)
    hb = await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    timeline = (await client.get(f"/v1/sessions/{w.session_id}/timeline", headers=w.wearer)).json()["entries"]
    texts = [r["text"] for r in await _q(db, """SELECT text FROM outbox WHERE session_id = %s AND recipient = 'wearer'
                                                 ORDER BY id""", w.session_id)]
    # Each world has its own contacts, so compare a contact by its position in the world, not its serial id.
    pos = {str(c["id"]): f"<contact {i}>" for i, c in enumerate(w.contacts)}
    detail = lambda d: {k: pos.get(str(v), v) if k == "contact_id" else v for k, v in d.items()}
    return {"status": _norm(status.text), "heartbeat": _norm(hb.text),
            "timeline": [(e["kind"], e["from_state"], e["to_state"], _norm(str(sorted(detail(e["detail"]).items()))))
                         for e in timeline],
            "wearer_texts": [_norm(t) for t in texts]}


@PROP
@given(ops=prefix_ops, action=st.sampled_from(("cancel", "end")), after=st.sampled_from(("none", "squeeze", "checkin")))
# Found by CI: an escalation before the PIN names each world's own first contact (different serial ids).
@example(ops=[("fire", states.CHECKIN_DUE), ("fire", states.PROMPT_TIMEOUT), ("fire", states.REMINDER_TIMEOUT)],
         action="cancel", after="none")
async def test_real_and_duress_pin_are_byte_identical_after_any_prefix(pclient, papp, ops, action, after):
    db, ctx = papp.state.db, papp.state.ctx
    await _end_all(db)
    real = await make_world(pclient, db, contacts=2)
    duress = await make_world(pclient, db, contacts=2)
    await _apply_prefix(pclient, db, ctx, real, ops, "r")
    await _apply_prefix(pclient, db, ctx, duress, ops, "d")
    assert await _wearer_side(pclient, db, real) == await _wearer_side(pclient, db, duress)
    a = await pclient.post(f"/v1/sessions/{real.session_id}/{action}", headers=real.wearer, json={"pin": PIN})
    b = await pclient.post(f"/v1/sessions/{duress.session_id}/{action}", headers=duress.wearer, json={"pin": DURESS_PIN})
    assert (a.status_code, a.content) == (b.status_code, b.content)
    assert a.status_code == 200
    if after == "squeeze":
        x = await pclient.post("/v1/device/events", headers=real.device(),
                               json={"event_id": "after", "type": "squeeze", "payload": {"pattern": "plain", "k": 2}})
        y = await pclient.post("/v1/device/events", headers=duress.device(),
                               json={"event_id": "after", "type": "squeeze", "payload": {"pattern": "plain", "k": 2}})
        assert (x.status_code, _norm(x.text)) == (y.status_code, _norm(y.text))
    elif after == "checkin":
        x = await pclient.post(f"/v1/sessions/{real.session_id}/checkin", headers=real.wearer, json={})
        y = await pclient.post(f"/v1/sessions/{duress.session_id}/checkin", headers=duress.wearer, json={})
        assert (x.status_code, x.content) == (y.status_code, y.content)
    assert await _wearer_side(pclient, db, real) == await _wearer_side(pclient, db, duress)
    # Masked: the duress world reads like the real one while a hidden duress alert is open underneath.
    hidden = await _q(db, "SELECT kind, masked FROM alerts WHERE session_id = %s AND status IN ('OPEN', 'ACKED') "
                          "AND NOT cover", duress.session_id)
    assert hidden and all(h["kind"] == "DURESS" and h["masked"] for h in hidden)


# --- 3. during an escalation only "she's safe" (from the acker) or the real PIN closes the alert ---------
alert_actions = st.lists(st.one_of(
    st.tuples(st.just("squeeze"), st.sampled_from(("plain", "unrecognized")), st.integers(1, 4), st.booleans()),
    st.tuples(st.just("checkin"), st.none(), st.none(), st.none()),
    st.tuples(st.just("wrong_pin"), st.sampled_from(("cancel", "end")), st.none(), st.none()),
    st.tuples(st.just("contact"), st.sampled_from(("ack", "pass", "safe", "cant_reach")), st.integers(0, 2), st.none()),
    st.tuples(st.just("fire"), st.sampled_from((states.ACK_TIMEOUT, states.FOLLOWUP_TIMEOUT, states.RENOTIFY)),
              st.none(), st.none()),
    st.tuples(st.just("real_pin"), st.sampled_from(("cancel", "end")), st.none(), st.none()),
), min_size=1, max_size=10)


@PROP
@given(actions=alert_actions)
async def test_only_she_is_safe_or_the_real_pin_ever_closes_an_escalation(pclient, papp, actions):
    db, ctx = papp.state.db, papp.state.ctx
    await _end_all(db)
    w = await make_world(pclient, db, contacts=2, pending=1)  # pending contact: joined, never approved
    for kind in (states.CHECKIN_DUE, states.PROMPT_TIMEOUT, states.REMINDER_TIMEOUT):
        await fire(db, ctx, w.session_id, kind)
    tokens = [c["token"] for c in w.contacts] + [w.pending[0]["token"]]
    for i, (kind, a, b, c) in enumerate(actions):
        before = await open_alert(db, w.session_id)
        assert before is not None
        acker = before["acked_by"]
        closing = False
        if kind == "squeeze":
            r = await pclient.post("/v1/device/events", headers=w.device(), json={
                "event_id": f"s:{i}", "type": "squeeze", "payload": {"pattern": a, "k": b, "buffered": c}})
            assert r.status_code == 200 and r.json()["result"] == "noted", r.text
        elif kind == "checkin":
            assert (await pclient.post(f"/v1/sessions/{w.session_id}/checkin", headers=w.wearer, json={})).status_code == 200
        elif kind == "wrong_pin":
            r = await pclient.post(f"/v1/sessions/{w.session_id}/{a}", headers=w.wearer, json={"pin": "0000"})
            assert r.status_code == 403
        elif kind == "contact":
            path = "/ack" if a == "ack" else "/followup"
            r = await pclient.post(f"/v1/contact/{tokens[b]}{path}", json={} if a == "ack" else {"action": a})
            assert r.status_code in (200, 404, 409), r.text
            if b == 2:
                assert r.status_code == 404  # never approved: no access at all
            contact_id = w.contacts[b]["id"] if b < 2 else None
            closing = a == "safe" and r.status_code == 200 and str(acker) == str(contact_id) \
                and before["status"] == "ACKED"
        elif kind == "fire":
            if any(t["kind"] == a for t in await pending_timers(db, w.session_id)):
                await fire(db, ctx, w.session_id, a)
        elif kind == "real_pin":
            r = await pclient.post(f"/v1/sessions/{w.session_id}/{a}", headers=w.wearer, json={"pin": PIN})
            assert r.status_code == 200
            closing = True
        after = await open_alert(db, w.session_id)
        s = await session_row(db, w.session_id)
        if closing:
            assert after is None, (kind, a)
            closed = (await _q(db, "SELECT status FROM alerts WHERE id = %s", before["id"]))[0]["status"]
            assert closed == ("FALSE_ALARM" if kind == "real_pin" else "RESOLVED")
            return
        assert after is not None and after["id"] == before["id"], (kind, a, b)
        assert states.is_alerting(s["state"]), (kind, a, s["state"])
