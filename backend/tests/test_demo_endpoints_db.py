"""The operator's live-demo fallbacks over HTTP (BUILD_PLAN §6.5 Operator, §8 fallbacks, §9):

* POST /v1/demo/inject {fall_like | offbody | head_position}: PULL_FORWARD only, labelled, simulated
* POST /v1/demo/hr-source {sim:steady | sim:lost | sim:climb_still | live}
* POST /v1/demo/profile {real | demo}
* every row an operator call injects is tagged actor='demo'
"""
from __future__ import annotations

import pytest

from app import rules, sensors, states
from tests.conftest import ADMIN_KEY, fire, session_row

H = {"X-Admin-Key": ADMIN_KEY}


async def _reset(client) -> str:
    r = await client.post("/v1/demo/reset", headers=H, json={})
    assert r.status_code == 200, r.text
    return r.json()["armed"]["session_id"]


async def _entries(db, sid, after_id: int = 0) -> list[dict]:
    async with db.connection() as conn:
        return list(await (await conn.execute(
            "SELECT id, actor, kind, detail FROM audit_log WHERE session_id = %s AND id > %s ORDER BY id",
            (sid, after_id))).fetchall())


async def _last_id(db) -> int:
    async with db.connection() as conn:
        return (await (await conn.execute("SELECT coalesce(max(id), 0) AS m FROM audit_log")).fetchone())["m"]


@pytest.mark.parametrize("kind,reason,level", [("fall_like", rules.REASON_FALL_LIKE, "HIGH"),
                                               ("offbody", rules.REASON_OFFBODY, "MED"),
                                               ("head_position", rules.REASON_HEAD, "MED")])
async def test_inject_only_brings_the_check_in_forward_labelled_and_simulated(client, db, ctx, kind, reason, level):
    sid = await _reset(client)
    mark = await _last_id(db)
    r = await client.post("/v1/demo/inject", headers=H, json={"kind": kind})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "reason": reason, "label": rules.REASON_LABELS[reason], "result": "prompted"}
    s = await session_row(db, sid)
    assert s["state"] == states.PROMPTED and s["prompt_cause"] == "challenge" and s["challenge_n"] in (1, 3, 4)
    (pf,) = [e for e in await _entries(db, sid, mark) if e["kind"] == "pull_forward"]
    assert pf["actor"] == "demo"
    assert pf["detail"]["reason"] == reason and pf["detail"]["level"] == level
    assert pf["detail"]["simulated"] is True                     # an operator injection is simulated motion
    # Injected again while the prompt is open, and during an alert: nothing changes, nothing escalates.
    again = await client.post("/v1/demo/inject", headers=H, json={"kind": kind})
    assert again.json()["result"] == "ignored" and (await session_row(db, sid))["state"] == states.PROMPTED
    await fire(db, ctx, sid, states.PROMPT_TIMEOUT)
    await fire(db, ctx, sid, states.REMINDER_TIMEOUT)
    escalated = (await session_row(db, sid))["state"]       # L1, or FINAL when the demo wearer has no contacts
    assert states.is_escalated(escalated)
    during = await client.post("/v1/demo/inject", headers=H, json={"kind": kind})
    assert during.json()["result"] == "ignored" and (await session_row(db, sid))["state"] == escalated


async def test_inject_rejects_an_unknown_kind(client, db, ctx):
    await _reset(client)
    assert (await client.post("/v1/demo/inject", headers=H, json={"kind": "spill"})).status_code == 422
    assert (await client.post("/v1/demo/inject", json={"kind": "fall_like"})).status_code == 401


async def _demo_user(db) -> str:
    async with db.connection() as conn:
        return str((await (await conn.execute(
            "SELECT id FROM users WHERE is_demo ORDER BY created_at LIMIT 1")).fetchone())["id"])


async def test_sim_steady_streams_badged_samples_near_the_baseline_and_never_prompts(client, db, ctx):
    sid = await _reset(client)
    r = await client.post("/v1/demo/hr-source", headers=H, json={"mode": "sim:steady"})
    assert r.json()["hr_source"]["mode"] == "sim:steady" and r.json()["simulated"] is True
    uid = await _demo_user(db)
    for _ in range(5):
        assert await sensors.simulate_hr(db, ctx) >= 1
    async with db.connection() as conn:
        rows = await (await conn.execute(
            """SELECT bpm, source, simulated FROM hr_samples WHERE user_id = %s AND time > now() - interval '1 minute'
                ORDER BY time DESC LIMIT 5""", (uid,))).fetchall()
    base = ctx.settings.demo_hr_baseline
    assert len(rows) == 5 and all(r["source"] == "sim" and r["simulated"] for r in rows)
    assert all(abs(r["bpm"] - base) <= 2.5 for r in rows)
    async with db.connection() as conn:
        await conn.execute("UPDATE sessions SET hr_deviating_since = NULL WHERE id = %s", (sid,))
    await sensors.evaluate_hr(db, ctx, force=True)
    assert (await session_row(db, sid))["state"] == states.ARMED    # steady: no early check-in
    await client.post("/v1/demo/hr-source", headers=H, json={"mode": "live"})


async def test_sim_lost_stops_the_samples_and_one_low_pull_forward_follows(client, db, ctx):
    sid = await _reset(client)
    uid = await _demo_user(db)
    await client.post("/v1/demo/hr-source", headers=H, json={"mode": "sim:steady"})
    for _ in range(3):
        await sensors.simulate_hr(db, ctx)
    r = await client.post("/v1/demo/hr-source", headers=H, json={"mode": "sim:lost"})
    assert r.json()["hr_source"]["mode"] == "sim:lost" and r.json()["simulated"] is True
    assert await sensors.simulate_hr(db, ctx) == 0                    # no samples while "lost"
    lost_after = ctx.settings.timings("demo").hr_lost_s
    async with db.connection() as conn:   # the watch went quiet more than hr_lost_s ago
        await conn.execute("UPDATE sessions SET created_at = now() - make_interval(secs => %s) WHERE id = %s",
                           (lost_after + 60.0, sid))
        await conn.execute("UPDATE hr_samples SET time = time - make_interval(secs => %s) WHERE user_id = %s",
                           (lost_after + 10.0, uid))
    mark = await _last_id(db)
    await sensors.evaluate_hr(db, ctx, force=True)
    s = await session_row(db, sid)
    assert s["state"] == states.PROMPTED and s["prompt_cause"] == "challenge"
    pf = [e for e in await _entries(db, sid, mark) if e["kind"] == "pull_forward"]
    assert [(e["actor"], e["detail"]["reason"], e["detail"]["level"]) for e in pf] == [
        ("rule:hr", rules.REASON_HR_LOST, "LOW")]
    await sensors.evaluate_hr(db, ctx, force=True)                     # once per gap
    assert len([e for e in await _entries(db, sid, mark) if e["kind"] == "pull_forward"]) == 1
    await client.post("/v1/demo/hr-source", headers=H, json={"mode": "live"})


async def test_profile_real_switches_the_timings_and_is_logged_as_demo(client, db, ctx):
    sid = await _reset(client)
    mark = await _last_id(db)
    r = await client.post("/v1/demo/profile", headers=H, json={"profile": "real"})
    assert r.status_code == 200 and r.json() == {"ok": True, "profile": "real"}
    assert (await session_row(db, sid))["profile"] == "real"
    assert (await client.post("/v1/demo/checkin-now", headers=H)).json()["state"] == states.PROMPTED
    async with db.connection() as conn:
        t = await (await conn.execute(
            """SELECT extract(epoch FROM fire_at - now()) AS s FROM timers
                WHERE session_id = %s AND kind = 'PROMPT_TIMEOUT' AND fired_at IS NULL AND cancelled_at IS NULL""",
            (sid,))).fetchone()
    real = ctx.settings.timings("real")
    assert real.prompt_window_s + real.delivery_grace_s - 5 <= float(t["s"]) <= real.prompt_window_s + real.delivery_grace_s
    entries = await _entries(db, sid, mark)
    assert ("demo", "profile_set") in [(e["actor"], e["kind"]) for e in entries]
    assert (await client.post("/v1/demo/profile", headers=H, json={"profile": "fast"})).status_code == 422
    await client.post("/v1/demo/profile", headers=H, json={"profile": "demo"})


async def test_every_row_an_operator_call_injects_is_tagged_demo(client, db, ctx):
    sid = await _reset(client)
    async with db.connection() as conn:
        first = await (await conn.execute(
            "SELECT actor FROM audit_log WHERE session_id = %s ORDER BY id LIMIT 1", (sid,))).fetchone()
    assert first["actor"] == "demo"                                    # armed by the reset
    mark = await _last_id(db)
    await client.post("/v1/demo/checkin-now", headers=H)
    await client.post("/v1/demo/fast-forward", headers=H)
    await client.post("/v1/demo/profile", headers=H, json={"profile": "demo"})
    inv = (await client.post("/v1/demo/judge-invite", headers=H)).json()
    async with db.connection() as conn:
        rows = await (await conn.execute(
            """SELECT actor, kind, session_id, detail FROM audit_log WHERE id > %s
                 AND (session_id = %s OR (detail ->> 'contact_id')::bigint = %s) ORDER BY id""",
            (mark, sid, inv["contact_id"]))).fetchall()
    by_operator = [r for r in rows if r["kind"] in ("checkin_due", "demo_fast_forward", "profile_set", "contact_invited")]
    assert {r["kind"] for r in by_operator} == {"checkin_due", "demo_fast_forward", "profile_set", "contact_invited"}
    assert all(r["actor"] == "demo" for r in by_operator), [(r["actor"], r["kind"]) for r in by_operator]
    r2 = await client.post("/v1/demo/reset", headers=H, json={})
    async with db.connection() as conn:
        reset_entry = await (await conn.execute(
            "SELECT actor FROM audit_log WHERE session_id = %s AND kind = 'demo_reset'", (sid,))).fetchone()
    assert reset_entry["actor"] == "demo" and r2.json()["closed_sessions"] >= 1
