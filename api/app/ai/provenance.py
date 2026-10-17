"""Who answered, and what it cost: one `ai_generation` row per Chain.run (C3 contracts/db.md, C4).

M1 keeps the rows in memory. The database writer (the `ai_generation` hypertable, linked into the scan's
audit chain with action "summarize" or "regenerate") plugs in as another Recorder once the migration
exists (M2/M6); the row shape below matches the contracts/db.md columns.
"""
from __future__ import annotations

import collections
import hashlib
import json
import uuid
from typing import Optional, Protocol

from app.schemas import Generation

# List prices per 1M tokens, USD (input, output), read 2026-10-17 (contracts/jobs.md "Rates").
# A model not listed has no known price, so its cost is null; it is never guessed.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "qwen3.8-flash-next": (0.10, 0.20),
}


def cost_usd(model: Optional[str], tokens_in: Optional[int], tokens_out: Optional[int]) -> Optional[float]:
    price = PRICES_PER_MTOK.get(model or "")
    if price is None or tokens_in is None or tokens_out is None:
        return None
    return round((tokens_in * price[0] + tokens_out * price[1]) / 1_000_000, 8)


def output_sha256(gen: Generation) -> str:
    text = json.dumps({"headline": gen.text.headline, "detail": gen.text.detail}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generation_row(gen: Generation, *, patient_id: Optional[str] = None, ms: float,
                   audit_hash: Optional[str] = None) -> dict:
    """The `ai_generation` row for one Chain.run (column names from contracts/db.md)."""
    return {
        "time": gen.generated_at,
        "generation_id": str(uuid.uuid4()),
        "scan_id": gen.scan_id,
        "patient_id": patient_id,
        "task": gen.task,
        "provider": gen.source,
        "model": gen.model,
        "prompt_version": gen.prompt_version,
        "facts_sha256": gen.facts_sha256,
        "output_sha256": output_sha256(gen),
        "verdict": "accepted" if gen.verified else "rejected",
        "attempts": [a.model_dump() for a in gen.attempts],
        "ms": ms,
        "tokens_in": gen.tokens_in,
        "tokens_out": gen.tokens_out,
        "cost_usd": gen.cost_usd,
        "chain_override": gen.chain_override,
        "cache_hit": gen.cache_hit,
        "audit_hash": audit_hash,
    }


class Recorder(Protocol):
    def record(self, row: dict) -> None: ...


class MemoryRecorder:
    """Keeps the most recent rows in memory (M1, tests)."""

    def __init__(self, maxlen: int = 256):
        self.rows: collections.deque[dict] = collections.deque(maxlen=maxlen)

    def record(self, row: dict) -> None:
        self.rows.append(row)
