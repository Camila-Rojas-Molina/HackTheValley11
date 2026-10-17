"""Vultr Serverless Inference client. Used ONLY to word PointClickCare care-team briefings.

The PCC rule engine decides everything on the alert card (score, level, rules,
suggested check) before this module runs; the model only turns those findings
into two or three plain sentences for a care team. Nothing here can change a
KAZE state, timer, alert or message, and no module on the KAZE check-in or
escalation path imports this one (backend/tests/test_vultr.py checks both ways).

API: OpenAI-compatible chat completions.
    POST {VULTR_BASE_URL}/chat/completions
    Authorization: Bearer <Serverless Inference key>
    https://docs.vultr.com/products/compute/serverless-inference/management/usage/chat.md

Env (all optional; with no key every call returns template_briefing(card)):
    VULTR_API_KEY            the Serverless Inference subscription key (Console > Serverless >
                             Inference > Overview), not the account API key.
                             VULTR_INFERENCE_API_KEY is read when VULTR_API_KEY is unset.
    VULTR_BASE_URL           default https://api.vultrinference.com/v1
    VULTR_MODEL              default qwen3.8-flash-next (live list: GET {base}/models, no auth)
    VULTR_TIMEOUT_S          default 6 (per connect / read)
    VULTR_MAX_TOKENS         default 400
    VULTR_REASONING_EFFORT   default "minimal"; empty to leave the field out (for models without it)

Fallback to the template on: no key, timeout, connection error, any non-2xx, an
unexpected body, empty, cut-off or over-long text, text that breaks the wording
rules, or a number that is not on the card.
429 and 5xx are retried once after a short jittered pause. After a failure the
client skips Vultr for 60 s, so a refreshed page never waits on a dead endpoint.
Texts are cached per card (a rejected text as "use the template"), so refreshes do
not call the API again.

Privacy: only the rule findings leave the server. resident_id, display_name and
any existing briefing are removed, and the name is replaced inside free text.
The name is added back locally in front of the returned text.

    python -m app.vultr          (from backend/) prints the config (never the key) and a
                                 sample briefing with its source: vultr or template
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
from typing import Any, Mapping, Optional

import httpx

log = logging.getLogger("stillhere.vultr")

DEFAULT_BASE_URL = "https://api.vultrinference.com/v1"
DEFAULT_MODEL = "qwen3.8-flash-next"
DEFAULT_TIMEOUT_S = 6.0
DEFAULT_MAX_TOKENS = 400
DEFAULT_REASONING_EFFORT = "minimal"
FAILURE_COOLDOWN_S = 60.0
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_CHARS = 700
CACHE_SIZE = 256
USED_FOR = "PointClickCare care-team briefings only"

LEVEL_PHRASES = {
    "low": "no change worth acting on",
    "worth a look this shift": "worth a look this shift",
    "review this shift": "review this shift with the nurse",
    "watch": "worth watching",              # earlier level names, for cards stored before them
    "check today": "check in person today",
}
RULE_FIELDS = ("rule", "observed", "baseline", "unit", "contribution", "explanation")
UNAVAILABLE = "Briefing unavailable; see the rule breakdown on the card."

# The wording rules (BUILD_PLAN §7.3) bind model output too. The stems are stored
# reversed so the repo-wide wording grep (tests/test_wording.py) stays a plain grep;
# tests/test_vultr.py checks this list equals the one in that test.
_BLOCKED_REVERSED = ("kips", "tacixotni", "suoicsnocnu", "songaid", "tceted", "dracydarb", "dracyhcat",
                     "repmat", "ecnedive")
BLOCKED_STEMS = tuple(s[::-1] for s in _BLOCKED_REVERSED)
_BLOCKED = re.compile("|".join(BLOCKED_STEMS), re.IGNORECASE)
# A briefing restates rule findings. It never names a condition and never predicts a time frame.
# The second half mirrors the senior-mode card rules (tests/test_care_wording.py CARE_BLOCKLIST):
# no forward-looking claim and no skin-temperature reading, since the briefing is shown on the card.
# Plurals and other forms count too ("infections", "UTIs", "falling"), and so does a time frame
# ("within 2 days", "in the next few days", "tomorrow").
_NO_CONDITIONS = re.compile(
    r"\b(?:infection|uti|delirium|dementia|parkinson\w*|stroke|sepsis|pneumonia|dehydrat\w*|disease|"
    r"disorder|syndrome|illness)(?:e?s)?\b|\b48\s*(?:–|—|-|to)\s*72\b"
    r"|hypotension|orthostatic|\bpots\b|urinary|septic|syncope|frail|sedat|hydration"
    r"|predict|forecast|prevent|will fall|\bfall(?:s|ing|en)?\b|\bfell\b|\brisk|early warning|\b72\s*h"
    r"|\bwithin\s+\d+(?:\.\d+)?\s*(?:h|hrs?|hours?|days?|weeks?)\b"
    r"|\b(?:in|over)\s+the\s+(?:next|coming)\s+(?:few\s+)?(?:\d+\s+)?(?:hours?|days?|weeks?)\b|\btomorrow\b"
    r"|skin|temperature|fever",
    re.IGNORECASE)
_THINK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

SYSTEM_PROMPT = (
    "You write a short briefing for a nursing-home care team. The JSON you receive is the complete "
    "output of a fixed rule engine run on SYNTHETIC demo data. Restate only what it contains: the level, "
    "the change score out of 100 (change from the resident's own usual; never call it a risk score), "
    "the rules that contributed most (observed value against the resident's own "
    "baseline, with units) and the suggested check. Write two or three plain sentences, at most 70 words, "
    "with no lists, headings or markdown. Refer to the person only as 'the resident'. Do not name or "
    "suggest any illness or medical condition, do not guess causes, do not predict what will happen or "
    "when, and do not add numbers that are not in the JSON. End with the suggested check."
)


class BriefingRejected(ValueError):
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


# --- deterministic template ---------------------------------------------------
def _num(v: Any) -> Optional[str]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float) and math.isfinite(v):
        return f"{v:.1f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _contribution(rule: Mapping) -> float:
    v = rule.get("contribution")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0.0


def _label(rule: Mapping) -> str:
    return str(rule.get("rule") or "rule").replace("_", " ").strip()


def _rule_phrase(rule: Mapping) -> str:
    expl = rule.get("explanation")
    if isinstance(expl, str) and expl.strip() and not _BLOCKED.search(expl):
        return expl.strip().rstrip(".")
    unit = f" {str(rule.get('unit')).strip()}" if rule.get("unit") else ""
    obs, base = _num(rule.get("observed")), _num(rule.get("baseline"))
    if obs is not None and base is not None:
        return f"{_label(rule)} {obs}{unit} against a usual {base}{unit}"
    if obs is not None:
        return f"{_label(rule)} {obs}{unit}"
    return _label(rule)


def _prefix(card: Mapping) -> str:
    name = str(card.get("display_name") or "").strip() or "Resident"
    return f"{name} (SYNTHETIC)" if card.get("synthetic", True) else name


def _top_rules(card: Mapping, n: int = 3) -> list:
    rules = [r for r in (card.get("rules") or []) if isinstance(r, Mapping)]
    rules.sort(key=_contribution, reverse=True)
    contributing = [r for r in rules if _contribution(r) > 0]
    return (contributing or rules)[:n]


def template_briefing(card: Mapping) -> str:
    """Deterministic briefing from the card alone. Never raises, never calls out."""
    try:
        level = str(card.get("level") or "").strip().lower()
        phrase = LEVEL_PHRASES.get(level, "review the rule breakdown")
        score = _num(card.get("score"))
        text = f"{_prefix(card)}: {phrase}" + (f" (change score {score}/100)." if score is not None else ".")
        parts = [_rule_phrase(r) for r in _top_rules(card)]
        text += f" Main changes: {'; '.join(parts)}." if parts else " No rule added to the score."
        check = card.get("suggested_check")
        if isinstance(check, str) and check.strip():
            text += f" Suggested check: {check.strip().rstrip('.')}."
        text += (" This compares the resident with their own baseline over the 14 days before the last 48 h; "
                 "it is not a medical assessment.")
        return text
    except Exception:  # a malformed card must never break the care page
        log.warning("template_briefing could not read the card")
        return UNAVAILABLE


# --- request / response ---------------------------------------------------------
def facts_for_model(card: Mapping) -> dict:
    """What leaves the server: rule findings only, no identifiers."""
    name = str(card.get("display_name") or "").strip()

    def clean(v: Any) -> Any:
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v if math.isfinite(v) else None
        s = str(v)
        if name:
            s = s.replace(name, "the resident")
        return s[:300]

    rules = [{k: clean(r.get(k)) for k in RULE_FIELDS if k in r}
             for r in (card.get("rules") or []) if isinstance(r, Mapping)][:10]
    return {"synthetic": bool(card.get("synthetic", True)), "score": clean(card.get("score")),
            "level": clean(card.get("level")), "rules": rules, "suggested_check": clean(card.get("suggested_check"))}


def request_body(card: Mapping, cfg: VultrConfig) -> dict:
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(facts_for_model(card), ensure_ascii=False, sort_keys=True)},
        ],
        "max_tokens": cfg.max_tokens,
        "temperature": 0.2,
    }
    if cfg.reasoning_effort:
        body["reasoning_effort"] = cfg.reasoning_effort
    return body


def _numbers(text: str) -> set:
    return {f"{float(n):g}" for n in _NUMBER.findall(text)}


def accept_text(data: Any, facts: Optional[Mapping] = None) -> str:
    """The model's sentence(s), or BriefingRejected.

    With `facts`, every number in the text must appear in them (or be the 100 of
    "out of 100"): the model may reword the findings, never add to them.
    """
    try:
        choice = data["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise BriefingRejected("unexpected response body") from None
    if not isinstance(content, str):
        raise BriefingRejected("no text content")
    if choice.get("finish_reason") == "length":
        raise BriefingRejected("cut off at max_tokens")
    text = _THINK.sub(" ", content)
    if "<think>" in text.lower():
        raise BriefingRejected("unfinished reasoning")
    text = re.sub(r"[*_`#>]+", "", text)
    text = " ".join(text.split()).strip().strip('"').strip()
    if not text:
        raise BriefingRejected("empty text")
    if len(text) > MAX_CHARS:
        raise BriefingRejected("text too long")
    if _BLOCKED.search(text) or _NO_CONDITIONS.search(text):
        raise BriefingRejected("wording rules")
    if facts is not None:
        allowed = _numbers(json.dumps(facts, ensure_ascii=False)) | {"100"}
        if not _numbers(text) <= allowed:
            raise BriefingRejected("numbers not on the card")
    return text


# --- cache and failure cooldown ------------------------------------------------------
_lock = threading.Lock()
_cache: "OrderedDict[str, str]" = OrderedDict()
_down_until = 0.0


def reset_state() -> None:
    """Forget cached texts and any failure cooldown (tests, or after fixing the key)."""
    global _down_until
    with _lock:
        _cache.clear()
        _down_until = 0.0


def _key(card: Mapping, cfg: VultrConfig) -> str:
    raw = json.dumps([cfg.base_url, cfg.model, facts_for_model(card)], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cached(key: str) -> Optional[str]:
    with _lock:
        text = _cache.get(key)
        if text is not None:
            _cache.move_to_end(key)
        return text


def _remember(key: str, text: str) -> None:
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
    log.warning("vultr briefing fell back to the template (%s); retrying Vultr after %.0f s",
                reason, FAILURE_COOLDOWN_S)


def _result(card: Mapping, text: Optional[str], cfg: VultrConfig) -> dict:
    if not text:  # None: not called or failed; "": this card's model text was rejected
        return {"text": template_briefing(card), "source": "template", "model": None}
    return {"text": f"{_prefix(card)}: {text}", "source": "vultr", "model": cfg.model}


def _start(card: Mapping, env: Optional[Mapping[str, str]]):
    """(config, cache key, text or None, go) — go is False when Vultr must not be called."""
    cfg = VultrConfig.from_env(env)
    if not cfg.enabled:
        return cfg, None, None, False
    key = _key(card, cfg)
    hit = _cached(key)
    if hit is not None:
        return cfg, key, hit, False
    return cfg, key, None, not _cooling_down()


def _headers(cfg: VultrConfig) -> dict:
    return {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}


def _timeout(cfg: VultrConfig) -> httpx.Timeout:
    return httpx.Timeout(cfg.timeout_s, connect=min(cfg.timeout_s, 3.0))


def briefing(card: Mapping, *, env: Optional[Mapping[str, str]] = None,
             transport: Optional[httpx.BaseTransport] = None) -> dict:
    """{text, source: 'vultr' | 'template', model}. Blocking; never raises.

    From async code use `await abriefing(card)` (or asyncio.to_thread): the KAZE
    timer worker shares the event loop and must never wait on this call.
    """
    try:
        cfg, key, text, go = _start(card, env)
        if not go:
            return _result(card, text, cfg)
        body = request_body(card, cfg)
        with httpx.Client(base_url=cfg.base_url, timeout=_timeout(cfg), transport=transport) as client:
            for attempt in (1, 2):
                r = client.post("/chat/completions", json=body, headers=_headers(cfg))
                if r.status_code in RETRY_STATUS and attempt == 1:
                    time.sleep(random.uniform(0.3, 0.8))
                    continue
                break
        if r.status_code != 200:
            _failed(f"HTTP {r.status_code}")
            return _result(card, None, cfg)
        text = accept_text(r.json(), facts_for_model(card))
        _remember(key, text)
        return _result(card, text, cfg)
    except BriefingRejected as exc:  # the endpoint works; this card gets the template
        log.warning("vultr briefing rejected (%s); using the template for this card", exc)
        _remember(key, "")
    except (httpx.HTTPError, ValueError) as exc:  # transport errors, bad JSON
        _failed(type(exc).__name__)
    except Exception as exc:  # pragma: no cover - never let a briefing break the page
        _failed(type(exc).__name__)
    return {"text": template_briefing(card), "source": "template", "model": None}


async def abriefing(card: Mapping, *, env: Optional[Mapping[str, str]] = None,
                    transport: Optional[httpx.AsyncBaseTransport] = None) -> dict:
    """Async twin of briefing(): same result, never blocks the event loop, never raises."""
    try:
        cfg, key, text, go = _start(card, env)
        if not go:
            return _result(card, text, cfg)
        body = request_body(card, cfg)
        async with httpx.AsyncClient(base_url=cfg.base_url, timeout=_timeout(cfg), transport=transport) as client:
            for attempt in (1, 2):
                r = await client.post("/chat/completions", json=body, headers=_headers(cfg))
                if r.status_code in RETRY_STATUS and attempt == 1:
                    await asyncio.sleep(random.uniform(0.3, 0.8))
                    continue
                break
        if r.status_code != 200:
            _failed(f"HTTP {r.status_code}")
            return _result(card, None, cfg)
        text = accept_text(r.json(), facts_for_model(card))
        _remember(key, text)
        return _result(card, text, cfg)
    except BriefingRejected as exc:
        log.warning("vultr briefing rejected (%s); using the template for this card", exc)
        _remember(key, "")
    except (httpx.HTTPError, ValueError) as exc:
        _failed(type(exc).__name__)
    except Exception as exc:  # pragma: no cover
        _failed(type(exc).__name__)
    return {"text": template_briefing(card), "source": "template", "model": None}


def briefing_text(card: dict, *, env: Optional[Mapping[str, str]] = None,
                  transport: Optional[httpx.BaseTransport] = None) -> str:
    """Shared PCC contract: Vultr wording when VULTR_API_KEY is set, else (or on any error
    or timeout) template_briefing(card). Blocking; async callers use abriefing_text."""
    return briefing(card, env=env, transport=transport)["text"]


async def abriefing_text(card: dict, *, env: Optional[Mapping[str, str]] = None,
                         transport: Optional[httpx.AsyncBaseTransport] = None) -> str:
    return (await abriefing(card, env=env, transport=transport))["text"]


def sample_card() -> dict:
    """For the CLI check below: the top card the rule engine gives the default synthetic cohort,
    so 'source: vultr' means a real card's numbers and wording got through, not a made-up one."""
    from . import care
    end = care.default_end_day()
    cards = care.score_cohort(care.generate_cohort(care.DEFAULT_SEED, end), end)
    return max(cards, key=lambda c: (c["score"], c["resident_id"]))


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:  # pragma: no cover
        pass
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(status(), indent=1))
    card = sample_card()
    print(f"card: {card['display_name']}, {card['level']}, change score {card['score']}/100 (rule engine, synthetic)")
    result = briefing(card)
    print(f"source: {result['source']}" + (f" ({result['model']})" if result["model"] else ""))
    print(result["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
