"""Small security helpers: constant-time comparison, sha256, random tokens, canonical JSON."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any, Optional


def sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def same(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def new_token() -> str:
    """URL-safe random token (A-Z a-z 0-9 _ -)."""
    return secrets.token_urlsafe(16)


def canonical_json(data: Any) -> str:
    """Deterministic JSON (sorted keys, no spaces, UTF-8 kept): what every stored hash is computed over."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
