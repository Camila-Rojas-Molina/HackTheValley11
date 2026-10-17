"""Masked duress, round two: the twin is a whole session, not just a public state.

A session masked by the duress PIN or a duress squeeze must look, on every wearer read (replies,
device, timeline, export, status, the record of who was alerted) and to every contact kept out of
duress alerts, exactly like a twin session that took the matching OK action (the real PIN or a plain
squeeze). Each test drives two worlds through the same inputs and compares them.
"""
from __future__ import annotations

import pytest

from app import states
from tests.conftest import DURESS_PIN, PIN, expire_leases, fire, make_world, open_alert, outbox, session_row, to_state
from tests.test_masking_db import _norm, advance, device_view, wearer_texts
from tests.test_masking_db import visible as _visible


async def visible(client, w, sid=None):
    """Her timeline, with the contact in an actor ("contact:<id>") made comparable too."""
    ids = {f"contact:{c['id']}": f"contact:C{i + 1}" for i, c in enumerate(w.contacts)}
    return [(ids.get(e[0], e[0]),) + tuple(e[1:]) for e in await _visible(client, w, sid)]


@pytest.fixture
def first_choice(ctx):
    old = ctx.choose
    ctx.choose = lambda options: list(options)[0]
    yield
    ctx.choose = old


async def squeeze(client, w, event_id, pattern, k=2):
    return await client.post("/v1/device/events", headers=w.device(),
                             json={"event_id": event_id, "type": "squeeze", "payload": {"pattern": pattern, "k": k}})


_STAMPS = ("alert_id", "session_id", "opened_at", "closed_at", "first_notified_at", "at")


def norm_record(body: dict, w) -> list:
    """GET /v1/me/alerts without ids and timestamps, contact ids made comparable."""
    def strip(v):
        if isinstance(v, dict):
            return {k: strip(x) for k, x in v.items() if k not in _STAMPS}
        if isinstance(v, list):
            return [strip(x) for x in v]
        return v
    return _norm(strip(body["alerts"]), w)


async def record(client, w) -> list:
    r = await client.get("/v1/me/alerts", headers=w.wearer)
    assert r.status_code == 200, r.text
    return norm_record(r.json(), w)


async def status_view(client, w, sid=None) -> dict:
    sid = sid or w.session_id
    body = (await client.get(f"/v1/sessions/{sid}", headers=w.wearer)).json()
    body.pop("session_id")
    body["cmd"] = {**body["cmd"], "challenge_id": bool(body["cmd"]["challenge_id"])}  # random per world
    return _norm(body, w)


async def export_view(client, w) -> list:
    out = []
    for s in (await client.get("/v1/me/export", headers=w.wearer)).json()["sessions"]:
        out.append([(e["kind"], e["from_state"], e["to_state"], _norm(e["detail"], w), e["masked"])
                    for e in s["entries"]])
    return out


def strip_heads(texts):
    return [t.split(" Timeline")[0] for t in texts]


async def same_wearer_views(client, db, a, b, step=""):
    assert await visible(client, a) == await visible(client, b), step
    assert await record(client, a) == await record(client, b), step
    assert await status_view(client, a) == await status_view(client, b), step
    assert strip_heads(await wearer_texts(db, a)) == strip_heads(await wearer_texts(db, b)), step
    assert await export_view(client, a) == await export_view(client, b), step


def _duress_kinds(entries):
    return [e for e in entries if isinstance(e[4], dict) and e[4].get("alert_kind") == states.DURESS]


# --- a duress squeeze during an escalation is masked like every other duress signal ----------------
async def test_a_duress_squeeze_during_an_escalation_never_shows_duress_to_the_wearer(client, db, ctx, first_choice):
    plain, duress = await make_world(client, db), await make_world(client, db)
    for w in (plain, duress):
        await to_state(client, db, ctx, w, "ESCALATED_L1")
    rp, rd = await squeeze(client, plain, "b1:900", "plain"), await squeeze(client, duress, "b1:900", "duress")
    assert rp.content == rd.content
    a = await open_alert(db, duress.session_id)
    assert a["kind"] == states.DURESS and a["masked"] is True
    await same_wearer_views(client, db, plain, duress, "after the squeeze")
    # The ladder moves on in both worlds (an ack timeout, then renotifies).
    for w in (plain, duress):
        await advance(db, ctx, w.session_id, ctx.settings.timings("demo").ack_s + 1)
    await same_wearer_views(client, db, plain, duress, "after the ack timeout")
    assert (await status_view(client, plain))["state_public"] == "ESCALATED_L2"
    assert not _duress_kinds(await visible(client, duress))
    ex = (await client.get("/v1/me/export", headers=duress.wearer)).json()["sessions"][-1]["entries"]
    assert not [e for e in ex if e["detail"].get("alert_kind") == states.DURESS]
    # A duress contact acknowledging the hidden alert changes nothing she can see.
    before = await visible(client, duress), await record(client, duress), await status_view(client, duress)
    r = await client.post(f"/v1/contact/{duress.contacts[0]['token']}/ack")
    assert r.json()["result"] == "acked"
    assert (await session_row(db, duress.session_id))["state"] == states.ACKED
    assert (await visible(client, duress), await record(client, duress), await status_view(client, duress)) == before


@pytest.mark.parametrize("acked", [False, True], ids=["L1", "ACKED"])
@pytest.mark.parametrize("action", ["cancel", "end"])
@pytest.mark.parametrize("pin", [PIN, DURESS_PIN], ids=["real_pin", "duress_pin"])
async def test_a_pin_after_a_duress_squeeze_during_an_escalation_answers_like_after_a_plain_one(
        client, db, ctx, first_choice, acked, action, pin):
    plain, duress = await make_world(client, db), await make_world(client, db)
    for w in (plain, duress):
        await to_state(client, db, ctx, w, "ESCALATED_L1")
        if acked:
            assert (await client.post(f"/v1/contact/{w.contacts[0]['token']}/ack")).json()["result"] == "acked"
    assert (await squeeze(client, plain, "b1:901", "plain")).content == \
           (await squeeze(client, duress, "b1:901", "duress")).content
    rp = await client.post(f"/v1/sessions/{plain.session_id}/{action}", json={"pin": pin}, headers=plain.wearer)
    rd = await client.post(f"/v1/sessions/{duress.session_id}/{action}", json={"pin": pin}, headers=duress.wearer)
    assert rp.status_code == rd.status_code == 200
    assert rp.content == rd.content
    assert rp.json()["state_public"] == (states.ARMED if action == "cancel" else states.ENDED)
    assert await device_view(client, plain) == await device_view(client, duress)
    await same_wearer_views(client, db, plain, duress)
    a = await open_alert(db, duress.session_id)
    assert a is not None and a["kind"] == states.DURESS and a["masked"] is True and a["status"] in ("OPEN", "ACKED")


# --- her record of who was alerted matches the twin's ------------------------------------------------
async def _to_final(client, db, ctx, w):
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    await fire(db, ctx, w.session_id, states.ACK_TIMEOUT)
    await fire(db, ctx, w.session_id, states.ACK_TIMEOUT)
    assert (await session_row(db, w.session_id))["state"] == states.FINAL


async def _lost_contact_notice(client, db, ctx, w):
    await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    await expire_leases(db, ctx, w)
    await fire(db, ctx, w.session_id, states.LOST_CONTACT_NOTIFY)
    assert (await open_alert(db, w.session_id))["kind"] == states.LOST_CONTACT


@pytest.mark.parametrize("where", ["L1", "ACKED", "FINAL", "lost_contact"])
@pytest.mark.parametrize("action", ["cancel", "end"])
async def test_the_record_after_the_duress_pin_matches_the_real_pin(client, db, ctx, first_choice, where, action):
    real, duress = await make_world(client, db), await make_world(client, db)
    for w in (real, duress):
        if where == "lost_contact":
            await _lost_contact_notice(client, db, ctx, w)
        elif where == "FINAL":
            await _to_final(client, db, ctx, w)
        else:
            await to_state(client, db, ctx, w, "ESCALATED_L1")
            if where == "ACKED":
                await client.post(f"/v1/contact/{w.contacts[0]['token']}/ack")
    assert await record(client, real) == await record(client, duress) != []
    rr = await client.post(f"/v1/sessions/{real.session_id}/{action}", json={"pin": PIN}, headers=real.wearer)
    rd = await client.post(f"/v1/sessions/{duress.session_id}/{action}", json={"pin": DURESS_PIN},
                           headers=duress.wearer)
    assert rr.content == rd.content
    after = await record(client, real)
    assert after[0]["status"] == states.FALSE_ALARM and after[0]["resolved_by"] == "wearer_pin"
    assert await record(client, duress) == after
    await same_wearer_views(client, db, real, duress)


async def test_a_duress_squeeze_adds_nobody_to_her_record_while_the_alert_is_open(client, db, ctx, first_choice):
    plain, duress = await make_world(client, db), await make_world(client, db)
    for w in (plain, duress):
        await client.put(f"/v1/contacts/{w.contacts[1]['id']}/duress", headers=w.wearer, json={"eligible": True})
        await to_state(client, db, ctx, w, "ESCALATED_L1")
    assert (await squeeze(client, plain, "b1:902", "plain")).content == \
           (await squeeze(client, duress, "b1:902", "duress")).content
    assert await record(client, plain) == await record(client, duress)
    assert [x["name"] for x in (await record(client, duress))[0]["alerted"]] == ["C1"]
    # C2 (the only duress recipient) takes the hidden duress alert: still nothing she can see.
    assert (await client.post(f"/v1/contact/{duress.contacts[1]['token']}/ack")).json()["result"] == "acked"
    assert await record(client, plain) == await record(client, duress)
    await same_wearer_views(client, db, plain, duress)


# --- the twin's own schedule: its check-ins, its ladder, its lost-contact notice ---------------------
async def test_an_unanswered_cover_check_in_escalates_like_the_twin_and_ignores_the_hidden_ladder(
        client, db, ctx, first_choice):
    real, duress = await make_world(client, db, contacts=3), await make_world(client, db, contacts=3)
    rr = await client.post(f"/v1/sessions/{real.session_id}/cancel", json={"pin": PIN}, headers=real.wearer)
    rd = await client.post(f"/v1/sessions/{duress.session_id}/cancel", json={"pin": DURESS_PIN},
                           headers=duress.wearer)
    assert rr.content == rd.content
    # The hidden ladder moves (an ack timeout, then a duress contact takes it): she sees none of it.
    await fire(db, ctx, duress.session_id, states.ACK_TIMEOUT)
    await client.post(f"/v1/contact/{duress.contacts[0]['token']}/ack")
    assert (await session_row(db, duress.session_id))["state"] == states.ACKED

    async def same(step):
        assert await status_view(client, real) == await status_view(client, duress), step
        assert await device_view(client, real) == await device_view(client, duress), step
        assert await record(client, real) == await record(client, duress), step
        assert await visible(client, real) == await visible(client, duress), step
    await same("armed")
    for real_kind, cover_kind, expect in ((states.CHECKIN_DUE, states.COVER_CHECKIN_DUE, states.PROMPTED),
                                          (states.PROMPT_TIMEOUT, states.COVER_PROMPT_TIMEOUT, states.REMINDED),
                                          (states.REMINDER_TIMEOUT, states.COVER_REMINDER_TIMEOUT, "ESCALATED_L1"),
                                          (states.ACK_TIMEOUT, "COVER_ACK_TIMEOUT", "ESCALATED_L2")):
        await fire(db, ctx, real.session_id, real_kind)
        await fire(db, ctx, duress.session_id, cover_kind)
        assert (await status_view(client, real))["state_public"] == expect
        await same(expect)
    rec = await record(client, duress)
    assert [(x["kind"], x["status"], x["level"]) for x in rec] == [(states.NO_RESPONSE, "OPEN", 2)]
    assert [x["name"] for x in rec[0]["alerted"]] == ["C1", "C2"]


@pytest.mark.parametrize("action", ["cancel", "end"])
async def test_a_device_left_unplugged_after_a_duress_pin_shares_the_lost_contact_notice_like_the_twin(
        client, db, ctx, action):
    real, duress = await make_world(client, db, arm=False), await make_world(client, db, arm=False)
    for w in (real, duress):
        w.session_id = (await client.post("/v1/sessions", json={"profile": "demo"}, headers=w.wearer)).json()["session_id"]
        await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    rr = await client.post(f"/v1/sessions/{real.session_id}/{action}", json={"pin": PIN}, headers=real.wearer)
    rd = await client.post(f"/v1/sessions/{duress.session_id}/{action}", json={"pin": DURESS_PIN},
                           headers=duress.wearer)
    assert rr.content == rd.content
    await expire_leases(db, ctx, real, duress)
    await same_wearer_views(client, db, real, duress, "link lost")
    # The grace period runs out with the device still away.
    for w in (real, duress):
        await advance(db, ctx, w.session_id, ctx.settings.timings("demo").lost_contact_grace_s + 1)
    await same_wearer_views(client, db, real, duress, "notice shared")
    if action == "cancel":
        assert [(x["kind"], x["status"]) for x in await record(client, duress)] == [(states.LOST_CONTACT, "OPEN")]
        assert (await visible(client, duress))[-1][1] == "lost_contact_shared"
    for w in (real, duress):
        await client.post("/v1/device/heartbeat", headers=w.device(), json={})  # plugged back in
    await same_wearer_views(client, db, real, duress, "restored")
    assert (await session_row(db, duress.session_id))["state"].startswith("ESCALATED_")


async def test_a_duress_ended_session_keeps_the_link_state_it_ended_with(client, db, ctx):
    real, duress = await make_world(client, db, arm=False), await make_world(client, db, arm=False)
    for w in (real, duress):
        w.session_id = (await client.post("/v1/sessions", json={"profile": "demo"}, headers=w.wearer)).json()["session_id"]
        await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    for w, pin in ((real, PIN), (duress, DURESS_PIN)):
        await client.post(f"/v1/sessions/{w.session_id}/end", json={"pin": pin}, headers=w.wearer)
    assert await status_view(client, real) == await status_view(client, duress)
    await expire_leases(db, ctx, real, duress)
    assert (await session_row(db, duress.session_id))["link_state"] == states.LINK_LOST   # the hidden escalation knows
    assert await status_view(client, real) == await status_view(client, duress)
    assert (await status_view(client, duress))["link_state"] == states.LINK_OK
    for w in (real, duress):
        await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    assert await status_view(client, real) == await status_view(client, duress)


# --- contacts kept out of duress alerts see the twin -----------------------------------------------
async def _excluded_worlds(client, db, ctx):
    """Two worlds: C1 (contact #1) is kept out of duress alerts, C2 is the only duress recipient."""
    worlds = [await make_world(client, db, contacts=3), await make_world(client, db, contacts=3)]
    for w in worlds:
        await client.put(f"/v1/contacts/{w.contacts[1]['id']}/duress", headers=w.wearer, json={"eligible": True})
        await to_state(client, db, ctx, w, "ESCALATED_L1")
    return worlds


def _templates(rows):
    return [(r["template"], r["text"].split(" at ")[0]) for r in rows]


async def _new_rows(db, w, c, since):
    return [r for r in await outbox(db, w.session_id, contact_id=c["id"]) if r["id"] > since]


async def _last_id(db):
    async with db.connection() as conn:
        return (await (await conn.execute("SELECT coalesce(max(id), 0) AS m FROM outbox")).fetchone())["m"]


def _norm_page(body, w):
    a = body.get("alert")
    if a:
        a = {k: v for k, v in a.items() if k not in ("id", "opened_at")}
        if a.get("location"):
            a["location"] = {k: v for k, v in a["location"].items() if k not in ("link", "as_of", "age_min")}
        body = {**body, "alert": a}
    return _norm(body, w)


async def _link(client, c) -> str:
    loc = (await client.get(f"/v1/contact/{c['token']}/status")).json()["alert"]["location"]
    return "/l/" + loc["link"].rsplit("/l/", 1)[1]


async def test_a_contact_kept_out_of_duress_alerts_sees_the_real_pin_world_after_the_duress_pin(client, db, ctx):
    real, duress = await _excluded_worlds(client, db, ctx)
    links = {}
    for w in (real, duress):
        ex, trusted, gone = w.contacts
        links[w.user_id] = {"ex": await _link(client, ex)}
        assert (await client.post(f"/v1/contact/{ex['token']}/followup", json={"action": "pass"})).json()["result"] == "passed"
        assert (await client.post(f"/v1/contact/{trusted['token']}/followup", json={"action": "pass"})).json()["result"] == "passed"
        links[w.user_id]["gone"] = await _link(client, gone)
        assert (await client.delete(f"/v1/contacts/{gone['id']}", headers=w.wearer)).status_code == 200
    since = await _last_id(db)
    rr = await client.post(f"/v1/sessions/{real.session_id}/cancel", json={"pin": PIN}, headers=real.wearer)
    rd = await client.post(f"/v1/sessions/{duress.session_id}/cancel", json={"pin": DURESS_PIN},
                           headers=duress.wearer)
    assert rr.content == rd.content
    ex_r = _templates(await _new_rows(db, real, real.contacts[0], since))
    ex_d = _templates(await _new_rows(db, duress, duress.contacts[0], since))
    assert ex_r == ex_d == [("false_alarm", "StillHere: False alarm: Maya cancelled with her PIN")]
    for who in ("ex", "gone"):
        codes = [(await client.get(links[w.user_id][who])).status_code for w in (real, duress)]
        assert codes == [410, 410], who
    for w in (real, duress):
        assert (await client.get(f"/v1/contact/{w.contacts[0]['token']}/status")).json() == \
            {"alert": None, "approved": True}
    # The duress recipient has the help request, with a location link that works.
    d2 = (await client.get(f"/v1/contact/{duress.contacts[1]['token']}/status")).json()["alert"]
    assert d2["kind"] == states.DURESS
    assert (await client.get("/l/" + d2["location"]["link"].rsplit("/l/", 1)[1])).status_code == 200


async def test_a_contact_kept_out_of_duress_alerts_sees_the_plain_squeeze_world_after_a_duress_squeeze(
        client, db, ctx, first_choice):
    plain, duress = await _excluded_worlds(client, db, ctx)
    since = await _last_id(db)
    assert (await squeeze(client, plain, "b1:903", "plain")).content == \
           (await squeeze(client, duress, "b1:903", "duress")).content
    pages = [_norm_page((await client.get(f"/v1/contact/{w.contacts[0]['token']}/status")).json(), w)
             for w in (plain, duress)]
    assert pages[0] == pages[1] and pages[0]["alert"]["kind"] == states.NO_RESPONSE
    assert _templates(await _new_rows(db, plain, plain.contacts[0], since)) == \
           _templates(await _new_rows(db, duress, duress.contacts[0], since))
    acks = [await client.post(f"/v1/contact/{w.contacts[0]['token']}/ack") for w in (plain, duress)]
    assert [(a.status_code, a.json()) for a in acks] == [(200, {"ok": True, "result": "acked"})] * 2
    assert _templates(await _new_rows(db, plain, plain.contacts[0], since)) == \
           _templates(await _new_rows(db, duress, duress.contacts[0], since))
    pages = [_norm_page((await client.get(f"/v1/contact/{w.contacts[0]['token']}/status")).json(), w)
             for w in (plain, duress)]
    assert pages[0] == pages[1] and pages[0]["alert"]["status"] == "ACKED"
    await same_wearer_views(client, db, plain, duress)
    # Underneath, the duress alert is still with C2 and only C2 can close it.
    real_alert = await open_alert(db, duress.session_id)
    assert real_alert["kind"] == states.DURESS and real_alert["acked_by"] is None
    r = await client.post(f"/v1/contact/{duress.contacts[0]['token']}/followup", json={"action": "safe"})
    assert r.json()["result"] == "resolved"                      # the twin's alert, as in the plain world
    assert (await open_alert(db, duress.session_id))["kind"] == states.DURESS
    assert (await session_row(db, duress.session_id))["state"].startswith("ESCALATED_")
