"""Vultr Serverless Inference client: the moved StillHere client, re-scoped for RainbowBone in M2.

It words the facts of one scan pair (C4, contracts/ai-interface.md) for the care-team summary or the
patient explanation. Our code computed every number; the model only words them, and accept_text() runs
the RainbowBone checker (contract.check) on every reply before it can be shown.

Galit's gate: no chain tier calls this module before M6. The chain's Vultr tier (openai_compat.vultr) is
a stub until then and AI_CHAIN defaults to `template`, so the app never reaches the code below that posts.
With no key nothing here touches the network.

API: OpenAI-compatible chat completions.
    POST {VULTR_BASE_URL}/chat/completions
    Authorization: Bearer <Serverless Inference key>
    https://docs.vultr.com/products/compute/serverless-inference/management/usage/chat.md

Env (all optional; with no key every call returns the template):
    VULTR_API_KEY            the Serverless Inference subscription key (Console > Serverless >
                             Inference > Overview), not the account API key.
                             VULTR_INFERENCE_API_KEY is read when VULTR_API_KEY is unset.
    VULTR_BASE_URL           default https://api.vultrinference.com/v1
    VULTR_MODEL              default qwen3.8-flash-next (live list: GET {base}/models, no auth)
    VULTR_TIMEOUT_S          default 6 (per connect / read)
    VULTR_MAX_TOKENS         default 400
    VULTR_REASONING_EFFORT   default "minimal"; empty to leave the field out (for models without it)

The filter (accept_text), re-scoped in M2:
    kept      strips <think>...</think>, and rejects an unexpected body, a `length` finish, empty text and
              any number that is not in the facts.
    changed   the reply is the C4 JSON {headline, detail, numbers_used}, and the number, spelled-out
              quantity, wording and length rules are contract.check(), which reads its lists from
              contracts/labels.json (ai). So it still blocks "risk" and "predict", it no longer blocks
              "fall"/"fell", and cm³ and cm3 both pass (the 3 of cm3 follows a letter, so it is not a
              number). StillHere's two wording lists for the care-home cards are gone.

Fallback to the template (template.template) on: no key, timeout, connection error, any non-2xx, an
unexpected body, or a reply accept_text() rejects. 429 and 5xx are retried once after a short jittered
pause. After a failure the client skips Vultr for 60 s, so a refreshed page never waits on a dead
endpoint. Accepted texts are cached per (model, task, prompt version, facts sha256), and a rejected reply
as "use the template", so refreshes do not call the API again.

Privacy: the request holds the system prompt and the facts JSON only (facts.facts_json): numbers and
enums, with no names, DICOM tags, dates or free text.

    python -m app.ai.providers.vultr   (from api/) prints the config (never the key)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union

import httpx

from app.ai import contract
from app.ai.facts import facts_json, facts_sha256
from app.ai.template import template
from app.schemas import Facts, GeneratedText

log = logging.getLogger("rainbowbone.ai.vultr")

DEFAULT_BASE_URL = "https://api.vultrinference.com/v1"
DEFAULT_MODEL = "qwen3.8-flash-next"
DEFAULT_TIMEOUT_S = 6.0
DEFAULT_MAX_TOKENS = 400
DEFAULT_REASONING_EFFORT = "minimal"
FAILURE_COOLDOWN_S = 60.0
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
CACHE_SIZE = 256
USED_FOR = "RainbowBone care-team summary and patient explanation wording (live from M6)"

_THINK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.IGNORECASE | re.DOTALL)


class TextRejected(ValueError):
    """The model answered, but the text cannot be shown."""


@dataclass(frozen=True)
class VultrConfig:
    api_key: Optional[str]
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    reasoning_effort: Optional[str] = DEFAULT_REASONING_EFFORT

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "VultrConfig":
        e = os.environ if env is None else env

        def opt(name: str) -> Optional[str]:
            v = (e.get(name) or "").strip()
            return v or None

        def num(name: str, default: float, lo: float, hi: float) -> float:
            try:
                v = float(e.get(name) or default)
            except ValueError:
                return default
            return min(hi, max(lo, v)) if math.isfinite(v) else default

        effort = e.get("VULTR_REASONING_EFFORT")
        return cls(
            api_key=opt("VULTR_API_KEY") or opt("VULTR_INFERENCE_API_KEY"),
            base_url=(opt("VULTR_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            model=opt("VULTR_MODEL") or DEFAULT_MODEL,
            timeout_s=num("VULTR_TIMEOUT_S", DEFAULT_TIMEOUT_S, 0.5, 30.0),
            max_tokens=int(num("VULTR_MAX_TOKENS", DEFAULT_MAX_TOKENS, 32, 2000)),
            reasoning_effort=DEFAULT_REASONING_EFFORT if effort is None else (effort.strip() or None),
        )


def status(env: Optional[Mapping[str, str]] = None) -> dict:
    """For health pages and the checklist. Never includes the key."""
    cfg = VultrConfig.from_env(env)
    return {"enabled": cfg.enabled, "base_url": cfg.base_url, "model": cfg.model, "used_for": USED_FOR}


# --- request / response ---------------------------------------------------------
def request_body(task: str, facts: Facts, cfg: VultrConfig) -> dict:
    """What leaves the server: the task's system prompt and the facts JSON, nothing else."""
    body = {
        "model": cfg.model,
        "messages": contract.messages_for(task, facts_json(facts)),
        "max_tokens": cfg.max_tokens,
        "temperature": 0.2,
    }
    if cfg.reasoning_effort:
        body["reasoning_effort"] = cfg.reasoning_effort
    return body


def accept_text(data: Any, facts: Facts) -> GeneratedText:
    """The model's {headline, detail} after the RainbowBone checker, or TextRejected with the reason.

    numbers_used is recomputed from the text, so it is always true.
    """
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise TextRejected("unexpected response body") from None
    if not isinstance(content, str):
        raise TextRejected("no text content")
    finish = choice.get("finish_reason")
    if finish == "length":  # checked before parsing: a cut-off reply is rarely valid JSON
        raise TextRejected("cut off at the token limit")
    raw = _THINK.sub(" ", content)
    if "<think>" in raw.lower():
        raise TextRejected("unfinished reasoning")
    fenced = _FENCE.match(raw)
    raw = (fenced.group(1) if fenced else raw).strip()
    if not raw:
        raise TextRejected("empty text")
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        raise TextRejected("not JSON") from None
    if not isinstance(obj, dict):
        raise TextRejected("not a JSON object")
    try:
        out = GeneratedText.model_validate(obj)
    except ValueError:  # pydantic.ValidationError; keep the reason short
        raise TextRejected("does not match the output schema") from None
    if not out.headline.strip() or not out.detail.strip():
        raise TextRejected("empty headline or detail")
    out = out.model_copy(update={"numbers_used": sorted(contract.numbers_in(f"{out.headline}\n{out.detail}"))})
    verdict = contract.check(out, facts, finish)
    if not verdict.ok:
        raise TextRejected(verdict.reason)
    return out


# --- cache and failure cooldown ------------------------------------------------------
_lock = threading.Lock()
_cache: "OrderedDict[str, Union[GeneratedText, str]]" = OrderedDict()
_down_until = 0.0


def reset_state() -> None:
    """Forget cached texts and any failure cooldown (tests, or after fixing the key)."""
    global _down_until
    with _lock:
        _cache.clear()
        _down_until = 0.0


def _key(task: str, facts: Facts, cfg: VultrConfig) -> str:
    raw = json.dumps([cfg.base_url, cfg.model, task, contract.PROMPT_VERSION, facts_sha256(facts)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cached(key: str) -> Optional[Union[GeneratedText, str]]:
    with _lock:
        text = _cache.get(key)
        if text is not None:
            _cache.move_to_end(key)
        return text


def _remember(key: str, text: Union[GeneratedText, str]) -> None:
    with _lock:
        _cache[key] = text
        _cache.move_to_end(key)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)


def _cooling_down() -> bool:
    with _lock:
        return time.monotonic() < _down_until


def _failed(reason: str) -> None:
    global _down_until
    with _lock:
        _down_until = time.monotonic() + FAILURE_COOLDOWN_S
    log.warning("vultr text fell back to the template (%s); retrying Vultr after %.0f s",
                reason, FAILURE_COOLDOWN_S)


def _headers(cfg: VultrConfig) -> dict:
    return {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}


def _timeout(cfg: VultrConfig) -> httpx.Timeout:
    return httpx.Timeout(cfg.timeout_s, connect=min(cfg.timeout_s, 3.0))


async def _call(task: str, facts: Facts, cfg: VultrConfig, key: str,
                transport: Optional[httpx.AsyncBaseTransport]) -> Optional[GeneratedText]:
    """One Vultr request (one retry on 429/5xx) and the filter. None means "use the template"."""
    try:
        body = request_body(task, facts, cfg)
        async with httpx.AsyncClient(base_url=cfg.base_url, timeout=_timeout(cfg), transport=transport) as client:
            for attempt in (1, 2):
                r = await client.post("/chat/completions", json=body, headers=_headers(cfg))
                if r.status_code in RETRY_STATUS and attempt == 1:
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    continue
                break
        if r.status_code != 200:
            _failed(f"HTTP {r.status_code}")
            return None
        text = accept_text(r.json(), facts)
        _remember(key, text)
        return text
    except TextRejected as exc:  # the endpoint works; these facts get the template
        log.warning("vultr text rejected (%s); using the template for these facts", exc)
        _remember(key, "")
    except (httpx.HTTPError, ValueError) as exc:  # transport errors, bad JSON
        _failed(type(exc).__name__)
    except Exception as exc:  # pragma: no cover - never let the wording break the page
        _failed(type(exc).__name__)
    return None


async def agenerate(task: str, facts: Facts, *, env: Optional[Mapping[str, str]] = None,
                    transport: Optional[httpx.AsyncBaseTransport] = None) -> dict:
    """{text: GeneratedText, source: 'vultr' | 'template', model}. Never blocks the event loop.

    Transport failures and rejected replies never raise; they give the template. An unknown task raises
    ValueError, and facts without a matching labels.json template raise TemplateMissing, as in the chain.
    """
    if task not in contract.TASKS:
        raise ValueError(f"unknown task {task!r}")
    cfg = VultrConfig.from_env(env)
    text: Optional[Union[GeneratedText, str]] = None
    if cfg.enabled:
        key = _key(task, facts, cfg)
        text = _cached(key)
        if text is None and not _cooling_down():
            text = await _call(task, facts, cfg, key, transport)
    if not text:  # None: not called or failed; "": these facts' model text was rejected
        return {"text": template(task, facts), "source": "template", "model": None}
    return {"text": text, "source": "vultr", "model": cfg.model}


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:  # pragma: no cover
        pass
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(status(), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
