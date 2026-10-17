"""The provider chain (C4, contracts/ai-interface.md "Chain").

Order comes from AI_CHAIN (a comma list of gemini, groq, vultr, local, template). An unknown name fails
at startup: the API's lifespan calls Chain.from_env(), which raises AIConfigError. `template` is appended
when missing and must be last. AI_OFFLINE=1 makes the chain local,template.

Until M6, AI_CHAIN defaults to `template` (Galit's gate) and every model provider is a stub that raises
ProviderUnavailable, so whatever AI_CHAIN says, the chain falls through to the template.

Per attempt: the provider's own timeout (a hard cap here as well), one ProviderError puts that provider in
a 60 s cooldown (outcome `cooldown` while it lasts), a text that fails check() is `rejected` and the next
tier runs. Accepted model texts are cached on (task, prompt_version, facts_sha256); bypass_cache skips the
read, never the write. The template is deterministic and free, so it is never cached.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Callable, Mapping, Optional, Sequence

from app.schemas import Attempt, Explanation, Facts, GeneratedText, Generation

from . import contract, provenance
from .facts import facts_json, facts_sha256
from .providers import gemini, openai_compat
from .providers.base import PROVIDER_NAMES, Provider, ProviderError, ProviderUnavailable
from .template import PROMPT_VERSION as TEMPLATE_PROMPT_VERSION
from .template import TemplateProvider, template

DEFAULT_CHAIN: tuple[str, ...] = ("template",)   # pinned until M6 (Galit's gate)
OFFLINE_CHAIN: tuple[str, ...] = ("local", "template")
COOLDOWN_S = 60.0


class AIConfigError(ValueError):
    """AI_CHAIN, AI_OFFLINE or an override names something the chain does not know."""


def parse_order(names: Sequence[str], *, source: str = "AI_CHAIN") -> tuple[str, ...]:
    """Validated provider order: known names only, no repeats, `template` last (appended if missing)."""
    order: list[str] = []
    for raw in names:
        name = str(raw).strip().lower()
        if not name:
            continue
        if name not in PROVIDER_NAMES:
            raise AIConfigError(f"{source}: unknown provider {raw!r}; known: {', '.join(PROVIDER_NAMES)}")
        if name in order:
            raise AIConfigError(f"{source}: {name!r} is listed twice")
        if "template" in order:
            raise AIConfigError(f"{source}: {name!r} comes after 'template', which always answers")
        order.append(name)
    if "template" not in order:
        order.append("template")
    return tuple(order)


def order_from_env(env: Optional[Mapping[str, str]] = None) -> tuple[str, ...]:
    e = os.environ if env is None else env
    raw = (e.get("AI_CHAIN") or "").strip()
    order = parse_order(raw.split(",")) if raw else DEFAULT_CHAIN
    offline = (e.get("AI_OFFLINE") or "").strip()
    if offline not in ("", "0", "1"):
        raise AIConfigError(f"AI_OFFLINE must be 0 or 1, got {offline!r}")
    return OFFLINE_CHAIN if offline == "1" else order


def build_providers(env: Optional[Mapping[str, str]] = None) -> dict[str, Provider]:
    return {
        "gemini": gemini.GeminiProvider(env),
        "groq": openai_compat.groq(env),
        "vultr": openai_compat.vultr(env),
        "local": openai_compat.local(env),
        "template": TemplateProvider(),
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Chain:
    def __init__(self, order: Sequence[str], providers: Optional[Mapping[str, Provider]] = None, *,
                 recorder: Optional[provenance.Recorder] = None,
                 clock: Callable[[], datetime] = _utcnow,
                 monotonic: Callable[[], float] = time.monotonic):
        self.order = parse_order(order)
        self.providers: dict[str, Provider] = dict(providers or build_providers({}))
        missing = [n for n in self.order if n not in self.providers]
        if missing:
            raise AIConfigError(f"no provider object for {missing}")
        self.recorder = recorder if recorder is not None else provenance.MemoryRecorder()
        self._clock = clock
        self._monotonic = monotonic
        self._cooldown_until: dict[str, float] = {}
        self._cache: dict[tuple[str, str, str], Generation] = {}

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, **kwargs) -> "Chain":
        """The chain the API runs. Raises AIConfigError on an unknown AI_CHAIN entry (fail at startup)."""
        e = os.environ if env is None else env
        return cls(order_from_env(e), build_providers(e), **kwargs)

    def status(self) -> list[dict]:
        """One entry per tier in order: configured, available and the key by shape only."""
        out = []
        for name in self.order:
            s = dict(self.providers[name].status())
            if self._cooling(name):
                s["available"], s["reason"] = False, "cooldown"
            out.append(s)
        return out

    def _cooling(self, name: str) -> bool:
        return self._monotonic() < self._cooldown_until.get(name, 0.0)

    async def run(self, task: str, facts: Facts, override: Optional[Sequence[str]] = None,
                  bypass_cache: bool = False, patient_id: Optional[str] = None) -> Generation:
        """Word `facts` for `task` with the first tier whose text passes check(); the template always does.

        `override` (the outage drill) replaces the order for this call and sets chain_override.
        """
        if task not in contract.TASKS:
            raise ValueError(f"unknown task {task!r}")
        order = self.order if override is None else parse_order(override, source="override")
        missing = [n for n in order if n not in self.providers]
        if missing:
            raise AIConfigError(f"no provider object for {missing}")
        started = time.perf_counter()
        fsha = facts_sha256(facts)
        key = (task, contract.PROMPT_VERSION, fsha)
        gen = None if bypass_cache else self._cache_hit(key, order, override is not None)
        if gen is None:
            gen = await self._run_tiers(task, facts, order, fsha, override is not None, key)
        ms = round((time.perf_counter() - started) * 1000.0, 1)
        self.recorder.record(provenance.generation_row(gen, patient_id=patient_id, ms=ms))
        return gen

    def _cache_hit(self, key, order, chain_override: bool) -> Optional[Generation]:
        cached = self._cache.get(key)
        if cached is None or cached.source not in order:
            return None
        return cached.model_copy(update={"cache_hit": True, "chain_override": chain_override,
                                         "generated_at": self._clock()})

    async def _run_tiers(self, task, facts, order, fsha, chain_override, key) -> Generation:
        attempts: list[Attempt] = []
        messages = contract.messages_for(task, facts_json(facts))
        for name in order:
            if name == "template":
                text = template(task, facts)
                verdict = contract.check(text, facts)
                if not verdict.ok:  # correct by construction; a failure here is a labels.json bug
                    raise RuntimeError(f"template failed the checker: {verdict.reason}")
                attempts.append(Attempt(provider="template", model=None, ms=0.0, outcome="accepted"))
                return self._generation(task, facts, text, verdict, "template", None, attempts, fsha,
                                        TEMPLATE_PROMPT_VERSION, chain_override, None, None)
            provider = self.providers[name]
            if self._cooling(name):
                attempts.append(Attempt(provider=name, model=provider.model, ms=0.0, outcome="cooldown",
                                        reason="failed in the last 60 s"))
                continue
            t0 = time.perf_counter()

            def elapsed() -> float:
                return round((time.perf_counter() - t0) * 1000.0, 1)

            try:
                # The adapter enforces its own timeout and one retry; this cap is the backstop.
                result = await asyncio.wait_for(
                    provider.generate(messages, schema=contract.OUTPUT_SCHEMA, timeout=provider.timeout),
                    timeout=provider.timeout * 2 + 1.0)
            except ProviderUnavailable as exc:
                attempts.append(Attempt(provider=name, model=provider.model, ms=elapsed(),
                                        outcome="unavailable", reason=exc.reason))
                continue
            except Exception as exc:  # ProviderError, timeout, or an adapter bug: never break the chain
                reason = exc.reason if isinstance(exc, ProviderError) else type(exc).__name__
                self._cooldown_until[name] = self._monotonic() + COOLDOWN_S
                attempts.append(Attempt(provider=name, model=provider.model, ms=elapsed(), outcome="error",
                                        reason=reason))
                continue
            try:
                text = _parse_output(result)
            except ValueError as exc:
                attempts.append(Attempt(provider=name, model=provider.model, ms=elapsed(),
                                        outcome="rejected", reason=f"invalid output: {exc}"))
                continue
            verdict = contract.check(text, facts, result.finish_reason)
            if not verdict.ok:
                attempts.append(Attempt(provider=name, model=provider.model, ms=elapsed(),
                                        outcome="rejected", reason=verdict.reason))
                continue
            attempts.append(Attempt(provider=name, model=provider.model, ms=elapsed(), outcome="accepted"))
            gen = self._generation(task, facts, text, verdict, name, provider.model, attempts, fsha,
                                   contract.PROMPT_VERSION, chain_override, result.tokens_in,
                                   result.tokens_out)
            self._cache[key] = gen
            return gen
        raise RuntimeError("chain ended without the template")  # parse_order always appends it

    def _generation(self, task, facts, text: GeneratedText, verdict, source, model, attempts, fsha,
                    prompt_version, chain_override, tokens_in, tokens_out) -> Generation:
        ai = contract.ai_labels()
        if source == "template":
            badge = ai["badge_template"].format(n=verdict.numbers_matched, m=verdict.numbers_total)
        else:
            badge = ai["badge"].format(provider=ai["provider_names"][source], model=model or "",
                                       n=verdict.numbers_matched, m=verdict.numbers_total)
        fields = dict(
            scan_id=facts.scan_id, task=task, text=text, footer=ai["footer"], source=source, model=model,
            verified=verdict.ok, numbers_matched=verdict.numbers_matched, numbers_total=verdict.numbers_total,
            badge=badge, attempts=attempts, facts_sha256=fsha, prompt_version=prompt_version,
            generated_at=self._clock(), cache_hit=False, chain_override=chain_override,
            tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=provenance.cost_usd(model, tokens_in, tokens_out),
        )
        if task == "patient_explanation":
            grade = ai["reading_grade"]
            return Explanation(
                **fields,
                reading_grade=contract.reading_grade(contract.joined([text.headline, text.detail])),
                reading_grade_formula=grade["formula"],
                target_grade_max=float(grade["target_max"]),
                review_note=ai["patient_review_note"],
            )
        return Generation(**fields)


def _parse_output(result) -> GeneratedText:
    """The model's JSON as GeneratedText. numbers_used is recomputed from the text, so it is always true."""
    data = result.json
    if data is None:
        try:
            data = json.loads(result.text or "")
        except json.JSONDecodeError as exc:
            raise ValueError("not JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    try:
        text = GeneratedText.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError is a ValueError subclass; keep the reason short
        raise ValueError("does not match the output schema") from exc
    if not text.headline.strip() or not text.detail.strip():
        raise ValueError("empty headline or detail")
    used = sorted(contract.numbers_in(f"{text.headline}\n{text.detail}"))
    return text.model_copy(update={"numbers_used": used})
