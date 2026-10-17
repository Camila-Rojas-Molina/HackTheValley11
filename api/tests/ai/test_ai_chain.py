"""Chain (C4): order from AI_CHAIN, fall-through to the template, the golden fixtures, the outage drill."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.ai import chain as chain_mod
from app.ai.chain import AIConfigError, Chain, order_from_env, parse_order
from app.ai.providers.base import PROVIDER_NAMES, ProviderError, ProviderUnavailable, Result
from app.schemas import Explanation, Generation
from tests.ai.conftest import EXPLANATION_FIXTURE, SUMMARY_FIXTURE, load_fixture

ALL = "gemini,groq,vultr,local,template"
# Shapes only: these strings are not keys and never leave the process.
FAKE_KEYS = {"GEMINI_API_KEY": "fake-gemini-key-000000000000", "GROQ_API_KEY": "fake-groq-key-0000000000",
             "VULTR_API_KEY": "fake-vultr-key-000000000", "LOCAL_LLM_BASE_URL": "http://127.0.0.1:9/v1"}


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------------------- order
def test_default_chain_is_pinned_to_template():
    assert order_from_env({}) == ("template",)
    assert Chain.from_env({}).order == ("template",)


def test_template_is_appended_when_missing():
    assert order_from_env({"AI_CHAIN": "gemini,groq"}) == ("gemini", "groq", "template")
    assert order_from_env({"AI_CHAIN": " Gemini , vultr "}) == ("gemini", "vultr", "template")


@pytest.mark.parametrize("value", ["gemini,bard", "openai", "gemini,,grok", "template,gemini", "groq,groq"])
def test_bad_ai_chain_fails_at_startup(value):
    with pytest.raises(AIConfigError):
        Chain.from_env({"AI_CHAIN": value})


def _real_app():
    from app.config import Settings
    from app.main import create_app
    # Port 9 (discard): nothing answers, and the chain is built before the database is touched.
    return create_app(Settings.from_env({"DATABASE_URL": "postgresql://127.0.0.1:9/rb_none", "AUTO_MIGRATE": "0"}))


def test_unknown_ai_chain_entry_stops_the_app_starting(monkeypatch):
    """The real API's lifespan builds the chain with Chain.from_env(); a typo must stop startup, not limp on."""
    monkeypatch.setenv("AI_CHAIN", "gemini,gemni")
    with pytest.raises(AIConfigError, match="gemni"):
        with TestClient(_real_app()):
            pass


def test_the_app_keeps_the_chain_it_built(monkeypatch):
    monkeypatch.setenv("AI_CHAIN", "groq")
    with TestClient(_real_app()) as client:
        assert client.app.state.ai_chain.order == ("groq", "template")


def test_offline_switch():
    assert order_from_env({"AI_CHAIN": ALL, "AI_OFFLINE": "1"}) == ("local", "template")
    assert order_from_env({"AI_CHAIN": ALL, "AI_OFFLINE": "0"}) == tuple(ALL.split(","))
    with pytest.raises(AIConfigError):
        order_from_env({"AI_OFFLINE": "yes"})
    with pytest.raises(AIConfigError):  # a typo in AI_CHAIN fails even when offline
        order_from_env({"AI_CHAIN": "gemini,bard", "AI_OFFLINE": "1"})


# ----------------------------------------------------------------------------------------- stubs fall through
@pytest.mark.parametrize("env", [{"AI_CHAIN": ALL}, {"AI_CHAIN": ALL, **FAKE_KEYS}])
def test_every_stub_unavailable_and_chain_returns_template(env, facts):
    c = Chain.from_env(env)
    gen = run(c.run("care_team_summary", facts))
    assert gen.source == "template"
    assert gen.verified is True
    assert [(a.provider, a.outcome) for a in gen.attempts] == [
        ("gemini", "unavailable"), ("groq", "unavailable"), ("vultr", "unavailable"),
        ("local", "unavailable"), ("template", "accepted")]
    status = {s["name"]: s for s in c.status()}
    assert list(status) == list(PROVIDER_NAMES)
    for name in ("gemini", "groq", "vultr", "local"):
        assert status[name]["available"] is False
    assert status["template"]["available"] is True


def test_configured_stub_says_why_it_is_unavailable(facts):
    gen = run(Chain.from_env({"AI_CHAIN": ALL, **FAKE_KEYS}).run("care_team_summary", facts))
    assert all("M6" in a.reason for a in gen.attempts if a.outcome == "unavailable")


# ----------------------------------------------------------------------------------------- golden fixtures
def test_template_summary_equals_golden_fixture(facts, fixed_clock):
    gen = run(Chain.from_env({}, clock=fixed_clock).run("care_team_summary", facts))
    assert type(gen) is Generation
    assert gen.model_dump(mode="json") == Generation.model_validate(load_fixture(SUMMARY_FIXTURE)).model_dump(
        mode="json")


def test_template_explanation_equals_golden_fixture(facts, fixed_clock):
    gen = run(Chain.from_env({}, clock=fixed_clock).run("patient_explanation", facts))
    assert isinstance(gen, Explanation)
    golden = Explanation.model_validate(load_fixture(EXPLANATION_FIXTURE))
    assert gen.model_dump(mode="json") == golden.model_dump(mode="json")
    assert gen.reading_grade <= gen.target_grade_max


def test_unknown_task_is_refused(facts):
    with pytest.raises(ValueError):
        run(Chain.from_env({}).run("copilot", facts))


# ----------------------------------------------------------------------------------------- outage drill
def test_override_replaces_the_order_and_is_flagged(facts):
    c = Chain.from_env({"AI_CHAIN": ALL})
    gen = run(c.run("care_team_summary", facts, override=["vultr", "groq"], bypass_cache=True))
    assert gen.chain_override is True
    assert [a.provider for a in gen.attempts] == ["vultr", "groq", "template"]
    with pytest.raises(AIConfigError):
        run(c.run("care_team_summary", facts, override=["gpt"]))


# ----------------------------------------------------------------------------------------- tier mechanics
class FakeProvider:
    """A provider double: returns or raises what the test scripts, and counts calls. No network."""

    def __init__(self, name, outputs, model="fake-model"):
        self.name, self.model, self.timeout = name, model, 1.0
        self.outputs = list(outputs)
        self.calls = 0

    def status(self):
        return {"name": self.name, "model": self.model, "configured": True, "key": None, "available": True}

    async def generate(self, messages, schema=None, tools=None, image=None, timeout=10.0):
        self.calls += 1
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def ok_result(headline, detail, finish="stop"):
    return Result(text=None, json={"headline": headline, "detail": detail, "numbers_used": []}, tool_calls=[],
                  finish_reason=finish, ms=5.0, tokens_in=100, tokens_out=40)


GOOD = ("L1 lesion volume was 0.00 cm³ on Day 0 and 3.24 cm³ on Day 273.",
        "That is 4.73% of the vertebra.")


def chain_with(*providers, now=None):
    tiers = {p.name: p for p in providers}
    tiers["template"] = chain_mod.TemplateProvider()
    clock = {"t": 0.0}
    c = Chain([p.name for p in providers], tiers, monotonic=lambda: clock["t"])
    return c, clock


def test_accepted_model_text_gets_badge_cost_and_true_numbers(facts):
    gem = FakeProvider("gemini", [ok_result(*GOOD)], model="gemini-3.8-flash")
    c, _ = chain_with(gem)
    gen = run(c.run("care_team_summary", facts))
    assert gen.source == "gemini" and gen.model == "gemini-3.8-flash"
    assert gen.badge == "Gemini · gemini-3.8-flash · 4/4 numbers match computed values"
    assert gen.text.numbers_used == [0.0, 3.24, 4.73, 273.0]  # recomputed from the text, not the model's list
    assert gen.cost_usd == pytest.approx((100 * 0.75 + 40 * 3.75) / 1e6)
    assert gen.prompt_version != "template/1"


@pytest.mark.parametrize("headline,detail,finish,reason", [
    ("L1 lost about 60% of its strength.", "Seen on Day 273.", "stop", "number not in facts"),
    ("L1 changed over four months.", "Lesion 3.24 cm³.", "stop", "spelled-out quantity"),
    ("Surgical fixation should be considered.", "Lesion 3.24 cm³.", "stop", "blocked wording"),
    ("This indicates a positive response to therapy.", "Lesion 3.24 cm³.", "stop", "blocked wording"),
    (*GOOD, "length", "cut off"),
])
def test_rejected_text_falls_through_with_reason(facts, headline, detail, finish, reason):
    gem = FakeProvider("gemini", [ok_result(headline, detail, finish)])
    groq = FakeProvider("groq", [ok_result(*GOOD)])
    c, _ = chain_with(gem, groq)
    gen = run(c.run("care_team_summary", facts))
    assert gen.source == "groq"
    assert gen.attempts[0].outcome == "rejected" and reason in gen.attempts[0].reason


@pytest.mark.parametrize("bad", [
    Result(text="not json", json=None, tool_calls=[], finish_reason="stop", ms=1, tokens_in=1, tokens_out=1),
    Result(text=None, json={"headline": "", "detail": "x", "numbers_used": []}, tool_calls=[],
           finish_reason="stop", ms=1, tokens_in=1, tokens_out=1),
    Result(text=None, json={"headline": "a", "detail": "b", "numbers_used": [], "extra": 1}, tool_calls=[],
           finish_reason="stop", ms=1, tokens_in=1, tokens_out=1),
])
def test_malformed_output_is_rejected(facts, bad):
    c, _ = chain_with(FakeProvider("gemini", [bad]))
    gen = run(c.run("care_team_summary", facts))
    assert gen.source == "template"
    assert gen.attempts[0].outcome == "rejected" and gen.attempts[0].reason.startswith("invalid output")


def test_error_starts_a_60s_cooldown(facts):
    gem = FakeProvider("gemini", [ProviderError(503, "upstream"), ok_result(*GOOD)])
    c, clock = chain_with(gem)
    first = run(c.run("care_team_summary", facts))
    assert [a.outcome for a in first.attempts] == ["error", "accepted"]
    second = run(c.run("care_team_summary", facts))
    assert [a.outcome for a in second.attempts] == ["cooldown", "accepted"] and gem.calls == 1
    assert {s["name"]: s["available"] for s in c.status()}["gemini"] is False
    clock["t"] = 61.0
    third = run(c.run("care_team_summary", facts))
    assert third.source == "gemini" and gem.calls == 2


def test_adapter_bug_or_timeout_never_breaks_the_chain(facts):
    class Slow(FakeProvider):
        async def generate(self, *a, **k):
            await asyncio.sleep(10)

    slow = Slow("groq", [])
    slow.timeout = 0.01
    c, _ = chain_with(FakeProvider("gemini", [RuntimeError("bug")]), slow)
    gen = run(c.run("care_team_summary", facts))
    assert [(a.provider, a.outcome) for a in gen.attempts] == [
        ("gemini", "error"), ("groq", "error"), ("template", "accepted")]


def test_unavailable_does_not_cool_down(facts):
    gem = FakeProvider("gemini", [ProviderUnavailable("no key"), ok_result(*GOOD)])
    c, _ = chain_with(gem)
    assert run(c.run("care_team_summary", facts)).source == "template"
    assert run(c.run("care_team_summary", facts)).source == "gemini"


def test_cache_and_bypass(facts):
    gem = FakeProvider("gemini", [ok_result(*GOOD), ok_result(*GOOD)])
    c, _ = chain_with(gem)
    first = run(c.run("care_team_summary", facts))
    hit = run(c.run("care_team_summary", facts))
    assert (first.cache_hit, hit.cache_hit, gem.calls) == (False, True, 1)
    fresh = run(c.run("care_team_summary", facts, bypass_cache=True))
    assert fresh.cache_hit is False and gem.calls == 2


def test_template_result_is_never_cached(facts):
    c = Chain.from_env({})
    run(c.run("care_team_summary", facts))
    assert run(c.run("care_team_summary", facts)).cache_hit is False


def test_one_provenance_row_per_run(facts):
    c = Chain.from_env({"AI_CHAIN": ALL})
    run(c.run("care_team_summary", facts))
    run(c.run("patient_explanation", facts, override=["groq"]))
    rows = list(c.recorder.rows)
    assert len(rows) == 2
    expected = {"time", "generation_id", "scan_id", "patient_id", "task", "provider", "model", "prompt_version",
                "facts_sha256", "output_sha256", "verdict", "attempts", "ms", "tokens_in", "tokens_out",
                "cost_usd", "chain_override", "cache_hit", "audit_hash"}
    assert all(set(r) == expected for r in rows)
    assert [r["chain_override"] for r in rows] == [False, True]
    assert rows[0]["provider"] == "template" and rows[0]["verdict"] == "accepted"
