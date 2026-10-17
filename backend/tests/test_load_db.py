"""A small concurrency and load test: 50 device events and 10 contact taps at once.

1. One session at FINAL with five contacts: 10 "I'm on it" taps (each contact twice) race 50 device events
   (40 distinct late squeezes plus 10 replays of them), all at the same time. Exactly one tap wins: one
   FINAL -> ACKED transition, one version bump, one "ack_thanks", one "handling" per other contact. Every
   distinct squeeze is stored once, gets exactly its messages, and never stands the alert down; every
   replay gets its original's bytes.
2. 25 sessions at PROMPTED each get the right squeeze twice at once (50 events): each moves to ARMED
   exactly once, with one "Noted ✓".

Then the outbox is delivered: every row goes out once, none is lost or failed. Nothing answers 5xx.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter

from app import states
from tests.conftest import deliver_all, fire, make_world, open_alert, outbox, session_row, to_state


async def _audit(db, sid) -> list[dict]:
    async with db.connection() as conn:
        return list(await (await conn.execute(
            "SELECT kind, from_state, to_state, actor FROM audit_log WHERE session_id = %s ORDER BY id",
            (sid,))).fetchall())


async def test_fifty_device_events_and_ten_contact_taps_at_once(client, db, ctx):
    w = await make_world(client, db, contacts=5)
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    for _ in range(5):
        await fire(db, ctx, w.session_id, states.ACK_TIMEOUT)
    before = await session_row(db, w.session_id)
    assert before["state"] == states.FINAL
    alert = await open_alert(db, w.session_id)
    rows_before = {o["id"] for o in await outbox(db, w.session_id)}

    def squeeze(i: int) -> dict:
        return {"event_id": f"load:{i}", "type": "squeeze",
                "payload": {"pattern": "plain" if i % 3 else "unrecognized", "k": 1 + i % 4, "buffered": i % 2 == 0}}

    device_calls = [("dev", i, squeeze(i)) for i in range(40)] + [("dev", i, squeeze(i)) for i in range(0, 40, 4)]
    tap_calls = [("tap", i, w.contacts[i % 5]["token"]) for i in range(10)]
    calls = device_calls + tap_calls
    assert len(device_calls) == 50 and len(tap_calls) == 10

    async def run(call):
        kind, i, arg = call
        if kind == "dev":
            # Each request gets its own X-Seq; the event id is what makes a replay a replay.
            return call, await client.post("/v1/device/events", headers=w.device(), json=arg)
        return call, await client.post(f"/v1/contact/{arg}/ack", json={})

    t0 = time.monotonic()
    results = await asyncio.gather(*(run(c) for c in calls))
    elapsed = time.monotonic() - t0
    assert all(r.status_code == 200 for _, r in results), [(c, r.status_code, r.text) for c, r in results
                                                          if r.status_code != 200]

    # Exactly one tap won; everyone else was told someone is handling it.
    taps = Counter(r.json()["result"] for (kind, _, _), r in results if kind == "tap")
    assert taps == {"acked": 1, "already_handled": 9}, taps
    after = await session_row(db, w.session_id)
    assert after["state"] == states.ACKED and after["version"] == before["version"] + 1
    moves = [e for e in await _audit(db, w.session_id) if e["to_state"] == states.ACKED
             and e["from_state"] != states.ACKED]
    assert len(moves) == 1, moves
    a = await open_alert(db, w.session_id)
    assert a["id"] == alert["id"] and a["status"] == "ACKED"
    acker = a["acked_by"]

    # Device events: one row per distinct id, replays byte-identical, nothing stood the alert down.
    by_id: dict[str, list[bytes]] = {}
    for (kind, i, body), r in results:
        if kind == "dev":
            by_id.setdefault(body["event_id"], []).append(r.content)
            assert r.json()["result"] == "noted" and states.is_alerting(r.json()["state_public"]), r.text
    assert len(by_id) == 40
    assert all(len(set(bodies)) == 1 for bodies in by_id.values())
    async with db.connection() as conn:
        stored = (await (await conn.execute(
            "SELECT count(*) AS n, count(DISTINCT event_id) AS d FROM device_events WHERE device_id = %s",
            (w.device_id,))).fetchone())
    assert (stored["n"], stored["d"]) == (40, 40)

    # Outbox: exactly the rows the 40 squeezes and the one winning tap call for; no duplicates.
    new = [o for o in await outbox(db, w.session_id) if o["id"] not in rows_before]
    per = Counter((o["recipient"], o["template"]) for o in new)
    contacts = [c["id"] for c in w.contacts]
    assert per[("wearer", "noted_alerting")] == 40
    assert per[("contact", "late_device")] == 40 * 5
    assert per[("contact", "ack_thanks")] == 1 and [o["contact_id"] for o in new if o["template"] == "ack_thanks"] == [acker]
    assert per[("contact", "handling")] == 4
    assert {o["contact_id"] for o in new if o["template"] == "handling"} == set(contacts) - {acker}
    assert len({o["dedupe_key"] for o in new}) == len(new)
    assert set(per) <= {("wearer", "noted_alerting"), ("contact", "late_device"), ("contact", "ack_thanks"),
                        ("contact", "handling")}, per
    assert elapsed < 30, f"60 concurrent requests took {elapsed:.1f} s"

    # Delivered at least once, none lost.
    await deliver_all(db, ctx)
    statuses = Counter(o["status"] for o in await outbox(db, w.session_id))
    assert set(statuses) == {"sent"}, statuses


async def test_the_same_check_in_squeeze_twice_at_once_moves_each_session_once(client, db, ctx):
    worlds = await asyncio.gather(*(make_world(client, db, contacts=1) for _ in range(25)))
    for w in worlds:
        await fire(db, ctx, w.session_id, states.CHECKIN_DUE)
    marks = {w.session_id: (await session_row(db, w.session_id))["version"] for w in worlds}

    async def answer(w):
        body = {"event_id": "twice:1", "type": "squeeze", "payload": {"pattern": "plain", "k": 2}}
        return w, await client.post("/v1/device/events", headers=w.device(), json=body)

    results = await asyncio.gather(*(answer(w) for w in worlds for _ in range(2)))
    assert len(results) == 50 and all(r.status_code == 200 for _, r in results)
    replies: dict[str, set] = {}
    for w, r in results:
        replies.setdefault(w.session_id, set()).add(r.content)
    for w in worlds:
        assert len(replies[w.session_id]) == 1                       # both answers carry the same bytes
        s = await session_row(db, w.session_id)
        assert s["state"] == states.ARMED and s["version"] == marks[w.session_id] + 1
        moves = [e for e in await _audit(db, w.session_id) if (e["from_state"], e["to_state"]) == ("PROMPTED", "ARMED")]
        assert len(moves) == 1
        noted = [o for o in await outbox(db, w.session_id, recipient="wearer") if o["template"] == "noted"]
        assert len(noted) == 1
