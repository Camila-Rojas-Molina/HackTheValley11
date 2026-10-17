"""Privacy controls from research 1.15 (kaze-evidence.md section D): a one-action "remove all
contacts" (Apple Safety Check's emergency reset), last-known location only with a set retention,
and an in-app record of who was alerted and who acknowledged."""
from __future__ import annotations

from tests.conftest import fire, make_world, outbox, session_row, to_state


# --- one action removes every contact ---------------------------------------------------------------
async def test_remove_all_contacts_in_one_action_is_silent(client, db, ctx):
    w = await make_world(client, db, contacts=2, pending=1)
    await to_state(client, db, ctx, w, "ESCALATED_L1")          # C1 has a pending alert message
    sent_before = {r["id"] for r in await outbox(db, recipient="contact") if r["status"] == "sent"}
    r = await client.delete("/v1/contacts", headers=w.wearer)
    assert r.status_code == 200 and r.json() == {"ok": True, "removed": 3}
    assert (await client.get("/v1/contacts", headers=w.wearer)).json()["contacts"] == []
    for c in w.contacts + w.pending:
        assert (await client.get(f"/v1/contact/{c['token']}/status")).status_code == 404
    rows = await outbox(db, w.session_id, recipient="contact")
    assert rows and all(r["status"] in ("failed", "sent") for r in rows)
    assert not [r for r in rows if r["status"] == "sent" and r["id"] not in sent_before]  # nothing new sent
    assert not [r for r in rows if r["template"] in ("contact_removed",)]
    async with db.connection() as conn:
        entry = await (await conn.execute(
            "SELECT detail FROM audit_log WHERE user_id = %s AND kind = 'contacts_removed_all'",
            (w.user_id,))).fetchone()
    assert entry["detail"] == {"removed": 3}
    again = await client.delete("/v1/contacts", headers=w.wearer)
    assert again.json() == {"ok": True, "removed": 0}


async def test_an_open_alert_keeps_escalating_after_every_contact_is_removed(client, db, ctx):
    """Removing contacts never stands an alert down: with nobody left the ladder reaches the
    simulated FINAL step (no call is placed)."""
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    assert (await client.delete("/v1/contacts", headers=w.wearer)).status_code == 200
    await fire(db, ctx, w.session_id, "ACK_TIMEOUT")
    assert (await session_row(db, w.session_id))["state"] == "ESCALATED_FINAL"


async def test_remove_all_needs_the_wearer_key(client, db):
    assert (await client.delete("/v1/contacts")).status_code == 401
