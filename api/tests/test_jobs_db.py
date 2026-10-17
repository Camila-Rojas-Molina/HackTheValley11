"""The job queue (C5, contracts/jobs.md): lease, extend, reap, reclaim at start-up, seconds and cost, and a
30 s job that survives the 10 s idle-in-transaction timeout because it never runs inside a transaction."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app import audit, jobs
from app.db import Database

SCAN = "myel018-L1-d273"


@pytest.fixture
async def db(app, settings):
    """A pool of its own (2 connections) so the tests see the same server state as the worker would."""
    d = Database(settings.database_url, max_size=4, name="rb-test-jobs")
    await d.open(wait=True)
    async with d.connection() as conn:
        await conn.execute("UPDATE jobs SET status = 'cancelled', claimed_by = NULL, locked_until = NULL "
                           "WHERE status IN ('queued', 'running')")
    try:
        yield d
    finally:
        await d.close()


async def _enqueue(db, kind="physics", **params):
    async with db.connection() as conn:
        return await jobs.enqueue(conn, kind, params or {"scan_id": SCAN}, scan_id=SCAN, patient_id="demo-a")


async def _job(db, job_id):
    async with db.connection() as conn:
        return await (await conn.execute("SELECT * FROM jobs WHERE job_id = %s", (job_id,))).fetchone()


async def test_claim_leases_the_oldest_job_once(db):
    a = await _enqueue(db)
    b = await _enqueue(db)
    t1, t2 = str(uuid.uuid4()), str(uuid.uuid4())
    got1, got2, got3 = await asyncio.gather(jobs.claim(db, ["physics"], t1), jobs.claim(db, ["physics"], t2),
                                            jobs.claim(db, ["physics"], str(uuid.uuid4())))
    claimed = [g for g in (got1, got2, got3) if g is not None]
    assert sorted(str(g["job_id"]) for g in claimed) == sorted([str(a["job_id"]), str(b["job_id"])])
    assert all(g["status"] == "running" and g["attempts"] == 1 and g["locked_until"] for g in claimed)
    assert await jobs.claim(db, ["segment"], t1) is None   # kinds filter


async def test_extend_pushes_the_lease_and_a_lost_lease_reports_false(db):
    j = await _enqueue(db)
    tok = str(uuid.uuid4())
    got = await jobs.claim(db, ["physics"], tok, lease_s=5)
    first = got["locked_until"]
    assert await jobs.extend(db, j["job_id"], tok, lease_s=60)
    assert (await _job(db, j["job_id"]))["locked_until"] > first
    assert not await jobs.extend(db, j["job_id"], str(uuid.uuid4()))   # someone else's token


async def test_reap_requeues_an_expired_lease_then_fails_it_when_attempts_run_out(db):
    j = await _enqueue(db)                                   # physics: max 2 attempts
    for attempt in (1, 2):
        got = await jobs.claim(db, ["physics"], str(uuid.uuid4()), lease_s=0.2)
        assert got["attempts"] == attempt
        await asyncio.sleep(0.4)
        assert await jobs.reap(db) >= 1
    row = await _job(db, j["job_id"])
    assert (row["status"], row["error"], row["claimed_by"]) == ("failed", "lease expired", None)


async def test_reclaim_at_start_up_releases_only_this_runners_orphans(db):
    mine = await _enqueue(db)
    other = await _enqueue(db)
    await jobs.claim(db, ["physics"], str(uuid.uuid4()), runner="mac-dev", lease_s=600)
    await jobs.claim(db, ["physics"], str(uuid.uuid4()), runner="vultr-vhf-4c-16gb-yto", lease_s=600)
    n = await jobs.reclaim_orphans(db, str(uuid.uuid4()), "mac-dev")
    assert n == 1
    states = {str(r["job_id"]): r["status"] for r in [await _job(db, mine["job_id"]), await _job(db, other["job_id"])]}
    assert sorted(states.values()) == ["queued", "running"]


async def test_finish_is_guarded_by_the_token_and_audited(db):
    j = await _enqueue(db)
    tok = str(uuid.uuid4())
    got = await jobs.claim(db, ["physics"], tok)
    assert not await jobs.finish(db, got, str(uuid.uuid4()), status="succeeded", result={}, seconds=1.0)
    assert await jobs.finish(db, got, tok, status="succeeded", result={"dof": 1}, seconds=1.5)
    row = await _job(db, j["job_id"])
    assert (row["status"], row["seconds"], row["result"]) == ("succeeded", 1.5, {"dof": 1})
    async with db.connection() as conn:
        rows = await audit.rows(conn, SCAN)
    assert rows[-1]["action"] == "physics" and rows[-1]["detail"]["job_id"] == str(j["job_id"])
    assert audit.verify_rows(rows)["ok"]


def test_cost_uses_the_runner_rate_and_never_prices_the_mac():
    usd, cost = jobs.cost_of("vultr-vhf-4c-16gb-yto", 7.7)
    assert usd == round(7.7 * 0.132 / 3600, 6) and cost["compute_usd_per_hour"] == 0.132 and cost["note"] is None
    usd, cost = jobs.cost_of("vultr-a16-ewr", 60.0, gpu_seconds=50.0)
    assert usd == round(50.0 * 0.471 / 3600, 6) and cost["gpu_usd_per_hour"] == 0.471
    usd, cost = jobs.cost_of("mac-dev", 30.0)
    assert usd is None and cost["note"] == "local run: no cloud cost"


async def test_runner_records_seconds_cost_and_a_failure(db):
    async def ok(job):
        return {"dof": 218_000}

    async def boom(job):
        raise ValueError("calibration missing")
    a = await _enqueue(db, "physics")
    r = jobs.Runner(db, {"physics": ok}, runner="vultr-vhf-4c-16gb-yto")
    assert await r.run_once() == "succeeded"
    row = await _job(db, a["job_id"])
    assert row["seconds"] >= 0 and row["cost"]["compute_usd_per_hour"] == 0.132 and row["cost_usd"] is not None
    b = await _enqueue(db, "segment")
    r2 = jobs.Runner(db, {"segment": boom}, runner="mac-dev")
    assert await r2.run_once() == "failed"
    row = await _job(db, b["job_id"])
    assert row["error"] == "ValueError: calibration missing" and row["cost_usd"] is None
    assert await r2.run_once() is None   # idle queue


async def test_a_30_s_job_survives_the_10_s_idle_in_transaction_timeout(db):
    """The claim commits before the job runs, and the lease is extended while it runs: a 30 s job finishes
    as succeeded on its first attempt even with a 3 s lease and a reaper running every second."""
    j = await _enqueue(db, "physics")

    async def slow(job):
        await asyncio.sleep(30)
        return {"slept_s": 30}

    runner = jobs.Runner(db, {"physics": slow}, runner="mac-dev", lease_s=3.0, extend_every_s=1.0)
    stop = asyncio.Event()

    async def reaper():
        while not stop.is_set():
            await jobs.reap(db)
            await asyncio.sleep(1.0)

    reap_task = asyncio.create_task(reaper())
    try:
        status = await runner.run_once()
    finally:
        stop.set()
        await reap_task
    row = await _job(db, j["job_id"])
    assert status == "succeeded"
    assert (row["status"], row["attempts"], row["result"]) == ("succeeded", 1, {"slept_s": 30})
    assert row["seconds"] >= 30
    async with db.connection() as conn:
        idle = await (await conn.execute(
            """SELECT count(*) AS n FROM pg_stat_activity
                WHERE datname = current_database() AND state = 'idle in transaction'""")).fetchone()
    assert idle["n"] == 0
