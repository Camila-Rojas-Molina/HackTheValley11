"""The one provider interface (C4, contracts/ai-interface.md).

Every tier of the chain (Gemini, Groq, Vultr, local, template) is a Provider. A provider either returns
a complete Result or raises: ProviderUnavailable (no key, cooldown, offline, or a stub before M6) or
ProviderError(status, reason). It never returns partial text.

status() reports the key by shape only (configured, length, prefix, masked suffix); never the value.
"""
from __future__ import annotations

from typing import Literal, Mapping, NamedTuple, Optional, Protocol, runtime_checkable

ProviderName = Literal["gemini", "groq", "vultr", "local", "template"]
PROVIDER_NAMES: tuple[str, ...] = ("gemini", "groq", "vultr", "local", "template")

# Why a stub provider is unavailable before the AI layer goes live (Galit's gate: no live AI calls).
STUB_REASON = "stub: provider goes live in M6"


class Result(NamedTuple):
    text: Optional[str]            # raw text (a JSON string when a schema is given)
    json: Optional[dict]           # parsed JSON when a schema is given
    tool_calls: list[dict]         # [{"name": str, "args": dict}]
    finish_reason: Optional[str]   # "stop" | "length" | "tool_calls" | provider value
    ms: float
    tokens_in: Optional[int]
    tokens_out: Optional[int]


class ProviderUnavailable(Exception):
    """The provider cannot be called right now: no key, cooldown, offline, or a stub."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ProviderError(Exception):
    """The provider was called and failed (HTTP status, timeout, bad body)."""

    def __init__(self, status: Optional[int], reason: str):
        super().__init__(f"{status}: {reason}" if status is not None else reason)
        self.status = status
        self.reason = reason


@runtime_checkable
class Provider(Protocol):
    name: str
    model: Optional[str]
    timeout: float

    def status(self) -> dict: ...

    async def generate(self, messages: list[dict], schema: Optional[dict] = None,
                       tools: Optional[list[dict]] = None, image: Optional[bytes] = None,
                       timeout: float = 10.0) -> Result: ...


def env_value(env: Mapping[str, str], name: str) -> Optional[str]:
    """A stripped env value, or None when unset or blank."""
    v = (env.get(name) or "").strip()
    return v or None


def key_shape(key: Optional[str]) -> Optional[str]:
    """Shape of a key for status pages: prefix, masked suffix and length. Never the value.

    The prefix is at most 4 characters and never more than a quarter of the key, so a short or
    malformed key shows nothing of itself.
    """
    if not key:
        return None
    n = len(key)
    prefix = key[: min(4, n // 4)]
    return f"{prefix}****({n} chars)"


def stub_status(name: str, model: Optional[str], key: Optional[str], *, configured: bool,
                **extra) -> dict:
    """status() of a provider that is not live yet: always unavailable, key by shape only."""
    out = {"name": name, "model": model, "configured": configured, "key": key_shape(key),
           "available": False, "reason": STUB_REASON}
    out.update(extra)
    return out
