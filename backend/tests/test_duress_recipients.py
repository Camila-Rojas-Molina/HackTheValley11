"""Duress recipients (research 1.15): the wearer can keep a contact out of duress alerts, so an
abusive "trusted contact" never receives one. If the wearer marks no contact as a duress recipient,
every approved contact can receive it (the behaviour before this list existed).

Pure state-machine checks first, then the HTTP path.
"""
from __future__ import annotations

import random

import pytest

from app import states
from app.config import PROFILES
from app.states import (ACKED, ARMED, DURESS, FINAL, NO_RESPONSE, AlertView, Event, Snapshot, decide,
                        level_state)
from tests.conftest import PIN, DURESS_PIN, make_world, outbox, to_state

T = PROFILES["demo"]
C1, C2, C3 = 101, 102, 103          # C1 is the contact kept out of duress alerts


def snap(state=ARMED, **kw) -> Snapshot:
    base = dict(state=state, public_state=kw.pop("public_state", state), version=3, timings=T,
                contacts=(C1, C2, C3), duress_contacts=(C2, C3))
    base.update(kw)
    return Snapshot(**base)


def told(d) -> dict:
    out: dict = {}
    for n in d.notify:
        if n.to == "contact":
            out.setdefault(n.contact_id, []).append(n.template)
    return out


def first(options):
    return list(options)[0]


# --- the ladder ----------------------------------------------------------------------------------
def test_duress_ladder_is_the_chosen_contacts_or_everyone():
    assert states.duress_ladder(snap()) == (C2, C3)
    assert states.duress_ladder(snap(duress_contacts=())) == (C1, C2, C3)
    assert states.duress_ladder(snap(duress_contacts=(C3, 999))) == (C3,)       # only approved contacts
    assert states.ladder(snap(), NO_RESPONSE) == (C1, C2, C3)                    # no-response alerts unchanged


@pytest.mark.parametrize("ev", [Event(states.PIN_DURESS), Event(states.END_DURESS),
                                Event(states.SQUEEZE, pattern="duress")])
def test_a_duress_signal_alerts_the_first_duress_contact_not_the_first_contact(ev):
    d = decide(snap(), ev, first)
    assert told(d) == {C2: ["alert_duress"]}


def test_with_no_duress_contact_chosen_the_first_approved_contact_gets_it():
    d = decide(snap(duress_contacts=()), Event(states.PIN_DURESS), first)
    assert told(d) == {C1: ["alert_duress"]}


def test_duress_escalation_walks_only_the_duress_contacts():
    a1 = AlertView(kind=DURESS, level=1, masked=True, recipients=(C2,))
    d = decide(snap(level_state(1), public_state=ARMED, alert=a1), Event(states.ACK_TIMEOUT), first)
    assert d.to_state == level_state(2) and told(d) == {C3: ["alert_duress"], C2: ["alert_duress"]}
    a2 = AlertView(kind=DURESS, level=2, masked=True, recipients=(C2, C3))
    d = decide(snap(level_state(2), public_state=ARMED, alert=a2), Event(states.ACK_TIMEOUT), first)
    assert d.to_state == FINAL and set(told(d)) == {C2, C3}
    d = decide(snap(level_state(1), public_state=ARMED, alert=a1), Event(states.RENOTIFY), first)
    assert told(d) == {C2: ["alert_duress"]}


@pytest.mark.parametrize("ev", [Event(states.PIN_DURESS), Event(states.END_DURESS),
                                Event(states.SQUEEZE, pattern="duress", late=True)])
def test_turning_a_no_response_alert_into_duress_never_tells_the_excluded_contact(ev):
    a = AlertView(kind=NO_RESPONSE, level=1, recipients=(C1,))
    d = decide(snap(level_state(1), alert=a), ev, first)
    assert d.alert_kind_after == DURESS
    assert C1 not in told(d) and told(d).get(C2) == ["alert_duress"]


def test_an_excluded_contact_cannot_take_pass_or_close_a_duress_alert():
    a = AlertView(kind=DURESS, level=1, masked=True, recipients=(C1, C2))
    s = snap(level_state(1), public_state=ARMED, alert=a)
    assert decide(s, Event(states.CONTACT_ACK, contact_id=C1), first).reply == "ignored"
    assert decide(s, Event(states.CONTACT_PASS, contact_id=C1), first).reply == "ignored"
    assert decide(s, Event(states.CONTACT_ACK, contact_id=C2), first).reply == "acked"
    held = AlertView(kind=DURESS, level=1, masked=True, status="ACKED", acked_by=C1, recipients=(C1, C2))
    s = snap(ACKED, public_state=ARMED, alert=held)
    d = decide(s, Event(states.CONTACT_SAFE, contact_id=C1), first)
    assert d.reply == "ignored" and not [op for op in d.alert_ops if op.op == "close"]


def test_a_duress_contact_takes_over_an_ack_held_by_an_excluded_contact():
    held = AlertView(kind=DURESS, level=1, masked=True, status="ACKED", acked_by=C1, recipients=(C1, C2))
    d = decide(snap(ACKED, public_state=ARMED, alert=held), Event(states.CONTACT_ACK, contact_id=C2), first)
    assert d.reply == "acked" and d.to_state == ACKED
    assert [(op.op, op.contact_id) for op in d.alert_ops] == [("ack", C2)]
    assert told(d) == {C2: ["ack_thanks"]}


def test_real_pin_note_and_late_squeeze_go_only_to_duress_contacts():
    a = AlertView(kind=DURESS, level=1, masked=True, recipients=(C1, C2))
    d = decide(snap(level_state(1), public_state=ARMED, alert=a), Event(states.PIN_REAL), first)
    assert set(told(d)) == {C2}
    a = AlertView(kind=DURESS, level=1, masked=False, recipients=(C1, C2))
    d = decide(snap(level_state(1), alert=a), Event(states.SQUEEZE, pattern="plain", late=True), first)
    assert set(told(d)) == {C2}


def test_random_walks_never_send_the_excluded_contact_anything_about_a_duress_alert():
    rng = random.Random(7)
    kinds = [k for k in states.ALL_EVENTS if not k.startswith("CONTACT_")]
    for _ in range(300):
        s = snap()
        for _ in range(25):
            k = rng.choice(kinds + ["CONTACT"] * 4)
            if k == "CONTACT":
                ev = Event(rng.choice([states.CONTACT_ACK, states.CONTACT_PASS, states.CONTACT_SAFE,
                                       states.CONTACT_CANT_REACH]), contact_id=rng.choice([C1, C2, C3]))
            elif k == states.SQUEEZE:
                ev = Event(k, pattern=rng.choice(states.SQUEEZE_PATTERNS), k=rng.choice([1, 2, 3, 4]))
            elif k == states.PULL_FORWARD:
                ev = Event(k, reason="head_position_change", level="MED")
            else:
                ev = Event(k)
            d = decide(s, ev, first)
            if d.alert_kind_after == DURESS or (s.alert is not None and s.alert.kind == DURESS):
                assert C1 not in told(d), (s, ev, d.notify)
            s = _next(s, d)


def _next(s: Snapshot, d) -> Snapshot:
    """Fold a decision back into a snapshot (enough for the walk above)."""
    alert = s.alert
    for op in d.alert_ops:
        if op.op == "open":
            alert = AlertView(kind=op.kind, level=op.level or 1, masked=bool(op.masked))
        elif op.op == "level" and alert is not None:
            alert = AlertView(kind=alert.kind, level=op.level, is_final=op.is_final, masked=alert.masked,
                              recipients=alert.recipients)
        elif op.op == "ack" and alert is not None:
            alert = AlertView(kind=alert.kind, level=alert.level, is_final=alert.is_final, masked=alert.masked,
                              status="ACKED", acked_by=op.contact_id, recipients=alert.recipients)
        elif op.op == "close":
            alert = None
        elif op.op == "upgrade" and alert is not None:
            alert = AlertView(kind=op.kind or alert.kind, level=alert.level, is_final=alert.is_final,
                              masked=alert.masked if op.masked is None else op.masked, status=alert.status,
                              acked_by=alert.acked_by, recipients=alert.recipients)
    if alert is not None:
        new = tuple(n.contact_id for n in d.notify if n.to == "contact" and n.contact_id not in alert.recipients)
        alert = AlertView(kind=alert.kind, level=alert.level, is_final=alert.is_final, masked=alert.masked,
                          status=alert.status, acked_by=alert.acked_by, recipients=alert.recipients + tuple(dict.fromkeys(new)))
    return Snapshot(state=d.to_state, public_state=d.public_state, version=s.version + 1, timings=T,
                    contacts=s.contacts, duress_contacts=s.duress_contacts, alert=alert,
                    challenge_n=d.challenge_n, prompt_cause=d.prompt_cause, retry_used=d.retry_used,
                    link_state=d.link_state, lost_contact_told=d.lost_contact_told)


# --- over HTTP ---------------------------------------------------------------------------------------
async def test_the_wearer_chooses_duress_recipients_and_the_list_shows_it(client, db):
    w = await make_world(client, db, contacts=2, arm=False)
    body = (await client.get("/v1/contacts", headers=w.wearer)).json()
    assert body["duress_recipients"]["mode"] == "all approved contacts (none chosen)"
    assert all(c["duress_eligible"] is False for c in body["contacts"])
    c2 = w.contacts[1]["id"]
    r = await client.put(f"/v1/contacts/{c2}/duress", headers=w.wearer, json={"eligible": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "contact_id": c2, "duress_eligible": True}
    body = (await client.get("/v1/contacts", headers=w.wearer)).json()
    assert body["duress_recipients"] == {"mode": "chosen", "contact_ids": [c2]}
    other = await make_world(client, db, contacts=0, arm=False)
    assert (await client.put(f"/v1/contacts/{c2}/duress", headers=other.wearer,
                             json={"eligible": True})).status_code == 404


async def test_a_duress_pin_never_reaches_the_excluded_contact_over_http(client, db, ctx):
    w = await make_world(client, db, contacts=2)
    c1, c2 = w.contacts
    assert (await client.put(f"/v1/contacts/{c2['id']}/duress", headers=w.wearer,
                             json={"eligible": True})).status_code == 200
    before = (await client.get(f"/v1/contact/{c1['token']}/status")).content
    r = await client.post(f"/v1/sessions/{w.session_id}/cancel", headers=w.wearer, json={"pin": DURESS_PIN})
    assert r.status_code == 200 and r.json()["state_public"] == ARMED
    rows = await outbox(db, w.session_id, recipient="contact")
    assert {(r["contact_id"], r["template"]) for r in rows} == {(c2["id"], "alert_duress")}
    # The excluded contact's page is byte-for-byte what it was before the alert, and taking it fails
    # the same way it does when no alert exists.
    assert (await client.get(f"/v1/contact/{c1['token']}/status")).content == before
    assert (await client.post(f"/v1/contact/{c1['token']}/ack")).status_code == 409
    s2 = (await client.get(f"/v1/contact/{c2['token']}/status")).json()
    assert s2["alert"]["kind"] == DURESS and "ack" in s2["alert"]["actions"]


async def test_a_no_response_alert_turned_duress_goes_quiet_for_the_excluded_contact(client, db, ctx):
    w = await make_world(client, db, contacts=2)
    c1, c2 = w.contacts
    await client.put(f"/v1/contacts/{c2['id']}/duress", headers=w.wearer, json={"eligible": True})
    await to_state(client, db, ctx, w, "ESCALATED_L1")                        # C1 told: no response
    assert (await client.get(f"/v1/contact/{c1['token']}/status")).json()["alert"]["kind"] == NO_RESPONSE
    r = await client.post(f"/v1/sessions/{w.session_id}/cancel", headers=w.wearer, json={"pin": DURESS_PIN})
    real_twin = r.content
    assert r.status_code == 200
    assert (await client.get(f"/v1/contact/{c1['token']}/status")).json() == {"alert": None, "approved": True}
    rows = [r for r in await outbox(db, w.session_id, recipient="contact") if r["template"] == "alert_duress"]
    assert {r["contact_id"] for r in rows} == {c2["id"]}
    # The wearer's reply is still the real-PIN reply, byte for byte.
    w2 = await make_world(client, db, contacts=2)
    await to_state(client, db, ctx, w2, "ESCALATED_L1")
    real = await client.post(f"/v1/sessions/{w2.session_id}/cancel", headers=w2.wearer, json={"pin": PIN})
    assert real.content == real_twin
