"""One OpenAI-compatible adapter for Groq, Vultr Serverless Inference and the local mlx_lm.server (C4).

Stub until M6: each provider reads its config, reports it by shape, and is always unavailable, so no
code path can reach the network (Galit's gate). The live adapter (M6) posts `{base_url}/chat/completions`
with a strict `response_format.json_schema`, retries once on 429/5xx, and reuses the moved Vultr client
(providers/vultr.py) for Vultr's config and transport.

Env:
    groq   GROQ_API_KEY, GROQ_MODEL (default openai/gpt-oss-120b); base https://api.groq.com/openai/v1
    vultr  the existing VULTR_* names, read by vultr.VultrConfig (VULTR_MODEL default qwen3.8-flash-next,
           VULTR_TIMEOUT_S default 6)
    local  LOCAL_LLM_BASE_URL, LOCAL_LLM_MODEL. No defaults: unset means not configured. The model id is
           read from GET {base}/models when LOCAL_LLM_MODEL is unset (M6).
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

from . import vultr as vultr_client
from .base import ProviderUnavailable, Result, STUB_REASON, env_value, stub_status

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"
GROQ_TIMEOUT_S = 8.0
LOCAL_TIMEOUT_S = 15.0


class OpenAICompatProvider:
    def __init__(self, name: str, *, base_url: Optional[str], model: Optional[str], api_key: Optional[str],
                 timeout: float, needs_key: bool = True):
        self.name = name
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self._key = api_key
        self._needs_key = needs_key

    @property
    def configured(self) -> bool:
        return bool(self.base_url) and (bool(self._key) or not self._needs_key)

    def status(self) -> dict:
        return stub_status(self.name, self.model, self._key, configured=self.configured,
                           base_url=self.base_url)

    async def generate(self, messages: list[dict], schema: Optional[dict] = None,
                       tools: Optional[list[dict]] = None, image: Optional[bytes] = None,
                       timeout: float = 10.0) -> Result:
        if not self.configured:
            raise ProviderUnavailable(f"{self.name} not configured")
        raise ProviderUnavailable(STUB_REASON)


def groq(env: Optional[Mapping[str, str]] = None) -> OpenAICompatProvider:
    e = os.environ if env is None else env
    return OpenAICompatProvider("groq", base_url=GROQ_BASE_URL,
                                model=env_value(e, "GROQ_MODEL") or GROQ_DEFAULT_MODEL,
                                api_key=env_value(e, "GROQ_API_KEY"), timeout=GROQ_TIMEOUT_S)


def vultr(env: Optional[Mapping[str, str]] = None) -> OpenAICompatProvider:
    cfg = vultr_client.VultrConfig.from_env(env)
    return OpenAICompatProvider("vultr", base_url=cfg.base_url, model=cfg.model, api_key=cfg.api_key,
                                timeout=cfg.timeout_s)


def local(env: Optional[Mapping[str, str]] = None) -> OpenAICompatProvider:
    e = os.environ if env is None else env
    base = env_value(e, "LOCAL_LLM_BASE_URL")
    return OpenAICompatProvider("local", base_url=base.rstrip("/") if base else None,
                                model=env_value(e, "LOCAL_LLM_MODEL"), api_key=None,
                                timeout=LOCAL_TIMEOUT_S, needs_key=False)
