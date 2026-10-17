"""The job queue (C5, contracts/jobs.md), adapted from the StillHere outbox lease pattern.

1. claim(): one short transaction, `FOR UPDATE SKIP LOCKED`, sets a lease (locked_until) and commits.
2. The job runs outside any transaction: db.py sets idle_in_transaction_session_timeout = 10 s, so work
   inside an open transaction would be killed.
3. extend(): while it runs, a heartbeat pushes locked_until forward every EXTEND_EVERY_S. A zero-row update
   means the lease was lost (reaped or cancelled): the runner stops and discards the result.
4. finish(): a guarded update (WHERE claimed_by = token) stores status, result or error, seconds and cost,
   and appends the step to the scan's audit chain.
5. reap() returns expired leases to 'queued' (attempts left) or 'failed' with error "lease expired";
   reclaim_orphans() does the same at worker start-up for jobs this runner held before it was restarted.

No CPU-bound job runs in the API's event loop: Runner is used by the worker process (worker/, M5).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import audit
from .security import canonical_json

log = logging.getLogger("rainbowbone.jobs")

KINDS = ("segment", "physics", "summarize", "synth_series")
LEASE_S = 60.0
EXTEND_EVERY_S = 20.0
TIMEOUT_S = {"segment": 900.0, "physics": 120.0, "summarize": 60.0, "synth_series": 900.0}
MAX_ATTEMPTS = {"segment": 2, "physics": 2, "summarize": 1, "synth_series": 1}

# List prices read 2026-10-17 (contracts/jobs.md "Rates"). A runner without a cloud rate (the Mac) is never
# priced as if it ran on Vultr: its cost_usd is null.
COMPUTE_USD_PER_HOUR = {"vultr-vhf-4c-16gb-yto": 0.132}
GPU_USD_PER_HOUR_PREFIX = {"vultr-a16-": 0.471}
LOCAL_NOTE = "local run: no cloud cost"


def _sha(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def cost_of(runner: Optional[str], seconds: float, *, gpu_seconds: float = 0.0, tokens_in: Optional[int] = None,
            tokens_out: Optional[int] = None, token_usd: Optional[float] = None) -> tuple[Optional[float], dict]:
    """(cost_usd, CostBreakdown dict) for one job, re-derivable from the rates it names."""
    compute_rate = COMPUTE_USD_PER_HOUR.get(runner or "")
    gpu_rate = next((r for p, r in GPU_USD_PER_HOUR_PREFIX.items() if (runner or "").startswith(p)), None)
    breakdown = {"compute_seconds": round(seconds, 3), "compute_usd_per_hour": compute_rate,
                 "gpu_seconds": round(gpu_seconds, 3) if gpu_rate is not None else None,
                 "gpu_usd_per_hour": gpu_rate, "tokens_in": tokens_in, "tokens_out": tokens_out,
                 "token_usd": token_usd, "note": None}
    if compute_rate is None and gpu_rate is None:
        breakdown["note"] = LOCAL_NOTE
        return (round(token_usd, 6) if token_usd else None), breakdown
    usd = 0.0
    if compute_rate is not None:
        usd += seconds * compute_rate / 3600.0
    if gpu_rate is not None:
        usd += gpu_seconds * gpu_rate / 3600.0
    usd += token_usd or 0.0
    return round(usd, 6), breakdown


async def enqueue(conn, kind: str, params: dict, *, scan_id: Optional[str] = None, patient_id: Optional[str] = None,
                  run_id: Optional[str] = None) -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown job kind {kind!r}")
    cur = await conn.execute(
        """INSERT INTO jobs (kind, params, scan_id, patient_id, run_id)
           VALUES (%s, %s::jsonb, %s, %s, %s) RETURNING *""",
        (kind, json.dumps(params), scan_id, patient_id, run_id))
    return await cur.fetchone()


async def claim(db, kinds: tuple[str, ...] | list[str], token: str, *, runner: Optional[str] = None,
                device: Optional[str] = None, lease_s: float = LEASE_S) -> Optional[dict]:
    """The oldest queued job of these kinds, leased to `token`, or None. Committed before it returns."""
    async with db.connection() as conn:
        cur = await conn.execute(
            """UPDATE jobs SET status = 'running', claimed_by = %(token)s,
                      locked_until = now() + make_interval(secs => %(lease)s),
                      attempts = attempts + 1, started_at = coalesce(started_at, now()),
                      runner = coalesce(%(runner)s, runner), device = coalesce(%(device)s, device)
                WHERE job_id = (SELECT job_id FROM jobs
                                 WHERE status = 'queued' AND kind = ANY(%(kinds)s)
                                 ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
            RETURNING *""",
            {"token": token, "lease": float(lease_s), "kinds": list(kinds), "runner": runner, "device": device})
        return await cur.fetchone()


async def extend(db, job_id, token: str, lease_s: float = LEASE_S) -> bool:
    """Push the lease forward. False: the lease was lost (reaped, cancelled or finished elsewhere)."""
    async with db.connection() as conn:
        cur = await conn.execute(
            """UPDATE jobs SET locked_until = now() + make_interval(secs => %s)
                WHERE job_id = %s AND status = 'running' AND claimed_by = %s""",
            (float(lease_s), job_id, token))
        return cur.rowcount == 1


async def finish(db, job: dict, token: str, *, status: str, result: Optional[dict] = None,
                 error: Optional[str] = None, seconds: float, cost_usd: Optional[float] = None,
                 cost: Optional[dict] = None, actor: str = "worker") -> bool:
    """Store the outcome if this token still holds the lease. False: the lease was lost, nothing stored."""
    if status not in ("succeeded", "failed"):
        raise ValueError("finish() takes succeeded or failed")
    async with db.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """UPDATE jobs SET status = %s, result = %s::jsonb, error = %s, finished_at = now(),
                          seconds = %s, cost_usd = %s, cost = %s::jsonb, locked_until = NULL
                    WHERE job_id = %s AND status = 'running' AND claimed_by IS NOT DISTINCT FROM %s
                RETURNING job_id""",
                (status, json.dumps(result) if result is not None else None, (error or None) and error[:2000],
                 seconds, cost_usd, json.dumps(cost) if cost is not None else None, job["job_id"], token))
            if cur.rowcount != 1:
                return False
            if job.get("scan_id"):
                await audit.append(conn, scan_id=job["scan_id"], patient_id=job.get("patient_id"),
                                   run_id=job.get("run_id"), actor=actor, action=job["kind"],
                                   detail={"job_id": str(job["job_id"]), "status": status,
                                           "params_sha256": _sha(job.get("params") or {}),
                                           "result_sha256": _sha(result) if result is not None else None,
                                           "error": error})
    return True


_EXPIRE_SQL = """
UPDATE jobs SET status = CASE WHEN attempts >= %(max)s THEN 'failed' ELSE 'queued' END,
                error = CASE WHEN attempts >= %(max)s THEN 'lease expired' ELSE error END,
                finished_at = CASE WHEN attempts >= %(max)s THEN now() ELSE NULL END,
                claimed_by = NULL, locked_until = NULL
 WHERE status = 'running' AND kind = %(kind)s AND {where}"""


async def reap(db) -> int:
    """Expired leases: back to 'queued' while attempts remain, else 'failed' ("lease expired")."""
    n = 0
    async with db.connection() as conn:
        for kind in KINDS:
            cur = await conn.execute(_EXPIRE_SQL.format(where="locked_until < now()"),
                                     {"max": MAX_ATTEMPTS[kind], "kind": kind})
            n += cur.rowcount
    return n


async def reclaim_orphans(db, token: str, runner: str) -> int:
    """At worker start-up: jobs this runner held under an earlier token (it was killed) are released now,
    without waiting for their lease. Jobs of other runners are left to reap()."""
    n = 0
    async with db.connection() as conn:
        for kind in KINDS:
            cur = await conn.execute(
                _EXPIRE_SQL.format(where="runner = %(runner)s AND claimed_by IS DISTINCT FROM %(token)s"),
                {"max": MAX_ATTEMPTS[kind], "kind": kind, "runner": runner, "token": token})
            n += cur.rowcount
    return n


Handler = Callable[[dict], Awaitable[dict]]


@dataclass
class Runner:
    """Claims and runs jobs for one worker process. Handlers take the job row and return its result dict
    (optionally with `_gpu_seconds`, `_tokens_in`, `_tokens_out`, `_token_usd` for the cost)."""
    db: Any
    handlers: dict[str, Handler]
    runner: str = "mac-dev"
    device: Optional[str] = None
    lease_s: float = LEASE_S
    extend_every_s: float = EXTEND_EVERY_S
    timeouts: dict[str, float] = field(default_factory=lambda: dict(TIMEOUT_S))
    token: str = field(default_factory=lambda: str(uuid.uuid4()))
    _started: bool = False

    async def run_once(self) -> Optional[str]:
        """Reap, claim one job, run it, finish it. Returns the final status, or None when the queue is idle."""
        if not self._started:
            await reclaim_orphans(self.db, self.token, self.runner)
            self._started = True
        await reap(self.db)
        job = await claim(self.db, tuple(self.handlers), self.token, runner=self.runner, device=self.device,
                          lease_s=self.lease_s)
        if job is None:
            return None
        return await self._run(job)

    async def _heartbeat(self, job: dict, lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self.extend_every_s)
            if not await extend(self.db, job["job_id"], self.token, self.lease_s):
                log.warning("job %s: lease lost; its result will be discarded", job["job_id"])
                lost.set()
                return

    async def _run(self, job: dict) -> str:
        t0 = time.monotonic()
        lost = asyncio.Event()
        beat = asyncio.create_task(self._heartbeat(job, lost))
        result: Optional[dict] = None
        error: Optional[str] = None
        try:
            result = await asyncio.wait_for(self.handlers[job["kind"]](job), timeout=self.timeouts[job["kind"]])
        except asyncio.TimeoutError:
            error = f"timeout after {self.timeouts[job['kind']]:.0f} s"
        except Exception as exc:  # a handler bug fails this job, never the worker
            log.exception("job %s (%s) failed", job["job_id"], job["kind"])
            error = f"{type(exc).__name__}: {exc}"
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
        seconds = time.monotonic() - t0
        if lost.is_set():
            return "lost"
        extras = {k: (result or {}).pop(k, None) for k in ("_gpu_seconds", "_tokens_in", "_tokens_out", "_token_usd")}
        cost_usd, cost = cost_of(self.runner, seconds, gpu_seconds=extras["_gpu_seconds"] or 0.0,
                                 tokens_in=extras["_tokens_in"], tokens_out=extras["_tokens_out"],
                                 token_usd=extras["_token_usd"])
        status = "failed" if error else "succeeded"
        ok = await finish(self.db, job, self.token, status=status, result=result, error=error,
                          seconds=round(seconds, 3), cost_usd=cost_usd, cost=cost)
        return status if ok else "lost"
