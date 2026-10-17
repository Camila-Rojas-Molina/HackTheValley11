"""Gemini adapter (C4). Stub until M6: it reads its config, reports it by shape, and is always unavailable.

The live adapter (M6) uses google-genai 2.31.0 `client.aio.models.generate_content` with
`GenerateContentConfig(response_mime_type="application/json", response_json_schema=...)`, thinking level
"low" ("minimal" errors on 3.8 Flash) and automatic function calling off; a 429 retries once on
GEMINI_FALLBACK_MODEL before the chain leaves Gemini (contracts/ai-interface.md, research ai.md §4).

Env: GEMINI_API_KEY, GEMINI_MODEL. Do not also set GOOGLE_API_KEY: the SDK prefers it.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

from .base import ProviderUnavailable, Result, STUB_REASON, env_value, stub_status

DEFAULT_MODEL = "gemini-3.8-flash"
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"
DEFAULT_TIMEOUT_S = 10.0


class GeminiProvider:
    name = "gemini"

    def __init__(self, env: Optional[Mapping[str, str]] = None):
        e = os.environ if env is None else env
        self._key = env_value(e, "GEMINI_API_KEY")
        self._google_key_set = env_value(e, "GOOGLE_API_KEY") is not None
        self.model: Optional[str] = env_value(e, "GEMINI_MODEL") or DEFAULT_MODEL
        self.timeout = DEFAULT_TIMEOUT_S

    def status(self) -> dict:
        extra = {}
        if self._google_key_set:
            extra["warning"] = "GOOGLE_API_KEY is set; the SDK would use it instead of GEMINI_API_KEY"
        return stub_status(self.name, self.model, self._key, configured=self._key is not None, **extra)

    async def generate(self, messages: list[dict], schema: Optional[dict] = None,
                       tools: Optional[list[dict]] = None, image: Optional[bytes] = None,
                       timeout: float = DEFAULT_TIMEOUT_S) -> Result:
        raise ProviderUnavailable(STUB_REASON if self._key else "no GEMINI_API_KEY")
