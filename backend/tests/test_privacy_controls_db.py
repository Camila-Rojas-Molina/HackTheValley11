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


# --- last-known location only, deleted after a set retention -------------------------------------------
from app import engine  # noqa: E402


async def snapshots(db, user_id) -> list[dict]:
    async with db.connection() as conn:
        return list(await (await conn.execute(
            "SELECT id, simulated, captured_at FROM location_snapshots WHERE user_id = %s ORDER BY id",
            (user_id,))).fetchall())


async def test_only_the_last_known_location_is_kept(client, db):
    w = await make_world(client, db, contacts=1)
    for lat in (43.70, 43.71, 43.72):
        r = await client.post(f"/v1/sessions/{w.session_id}/checkin", headers=w.wearer,
                              json={"lat": lat, "lon": -79.4, "accuracy_m": 10})
        assert r.status_code == 200
    rows = await snapshots(db, w.user_id)
    assert len(rows) == 1                                               # no location history


async def test_a_location_released_to_an_alert_is_kept_with_it_and_the_next_one_replaces_the_rest(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, "ESCALATED_L1")                  # releases the last-known location
    released = (await snapshots(db, w.user_id))[-1]["id"]
    await client.post(f"/v1/sessions/{w.session_id}/checkin", headers=w.wearer, json={"lat": 43.8, "lon": -79.3})
    ids = [r["id"] for r in await snapshots(db, w.user_id)]
    assert released in ids and len(ids) == 2                            # the alert's copy, and the newest


async def test_real_locations_are_deleted_after_48_hours_unless_an_open_alert_holds_them(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    held = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, held, "ESCALATED_L1")
    async with db.connection() as conn:
        await conn.execute("UPDATE location_snapshots SET captured_at = now() - interval '49 hours' "
                           "WHERE user_id = ANY(%s)", ([w.user_id, held.user_id],))
        await conn.execute("""INSERT INTO location_shares (token_hash, token_nonce, snapshot_id, expires_at)
                              SELECT 'old-' || id, 'n', id, now() - interval '49 hours'
                                FROM location_snapshots WHERE user_id = %s""", (w.user_id,))
        out = await engine.purge_locations(conn)
        left = (await (await conn.execute(
            "SELECT count(*) AS n FROM location_shares WHERE token_hash LIKE 'old-%%'")).fetchone())["n"]
    assert out["snapshots"] >= 1 and left == 0                           # its expired link went with it
    assert await snapshots(db, w.user_id) == []                          # past retention: gone
    assert len(await snapshots(db, held.user_id)) == 1                   # an open alert still needs it
    assert engine.LOCATION_RETENTION_H == 48


async def test_the_simulated_demo_location_is_not_personal_data_and_stays(client, db):
    w = await make_world(client, db, contacts=0, arm=False, location=False)
    async with db.connection() as conn:
        await engine.record_location(conn, client._transport.app.state.ctx, user_id=w.user_id, session_id=None,
                                     lat=43.78, lon=-79.18, accuracy_m=50.0, source="simulated", simulated=True)
        await conn.execute("UPDATE location_snapshots SET captured_at = now() - interval '72 hours' "
                           "WHERE user_id = %s", (w.user_id,))
        await engine.purge_locations(conn)
    assert len(await snapshots(db, w.user_id)) == 1
