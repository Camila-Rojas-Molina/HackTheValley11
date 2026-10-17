"""Re-scoped filter (M2). These replace StillHere's test_vultr.

The moved Vultr client's filter (providers/vultr.accept_text) and the chain's checker (contract.check) apply
one rule set, read from contracts/labels.json: it still blocks "risk" and "predict" in AI text, it no longer
blocks "fall"/"fell", it accepts cm³ and cm3, and every number must be in the facts.

The client keeps StillHere's transport behaviour (one retry on 429/5xx, a 60 s cooldown, a cache, the
template on any failure). It is tested against httpx.MockTransport only: no test touches the network, and
AI_CHAIN stays pinned to the template (Galit's gate).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.ai import chain as chain_mod
from app.ai import contract
from app.ai.chain import Chain
from app.ai.facts import build_facts, facts_json
from app.ai.providers import vultr
from app.ai.providers.base import STUB_REASON
from app.ai.template import template
from app.schemas import GeneratedText

CHECKER, VULTR = "checker", "vultr"
VIA = (CHECKER, VULTR)
DETAIL = "Values are from the scan pair."
FAKE_KEY = "VULTRFAKEFAKEFAKEFAKE4242"
FIXTURE_REPLY = ("L1 lesion volume went from 0.00 cm³ to 3.24 cm³.", "That is 4.73% of the vertebra on Day 273.")


@pytest.fixture(autouse=True)
def _fresh_client_state():
    vultr.reset_state()
    yield
    vultr.reset_state()


@pytest.fixture
def facts42():
    """Facts that hold 42 (lesion cm³) and 12 (lesion share, points), the numbers PLAN M2's sentences use."""
    return build_facts(scan_id="case-L1-d273", level="L1", day_a=0, day_b=273, lesion_volume_a_cm3=0.0,
                       lesion_volume_b_cm3=42.0, lesion_grid="original", vertebra_volume_b_cm3=840.0,
                       mask_source="expert", provenance="real_scan", lesion_share_pts=12.0)


def reply(headline: str, detail: str = DETAIL, *, finish: str = "stop", content=None) -> dict:
    """An OpenAI-compatible chat completion body carrying the C4 JSON output."""
    if content is None:
        content = json.dumps({"headline": headline, "detail": detail, "numbers_used": []}, ensure_ascii=False)
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}]}


def verdict(via: str, facts, headline: str, detail: str = DETAIL) -> tuple[bool, str | None]:
    if via == CHECKER:
        v = contract.check(GeneratedText(headline=headline, detail=detail, numbers_used=[]), facts)
        return v.ok, v.reason
    try:
        vultr.accept_text(reply(headline, detail), facts)
        return True, None
    except vultr.TextRejected as exc:
        return False, str(exc)


# ----------------------------------------------------------------------------------------- PLAN M2 sentences
@pytest.mark.parametrize("via", VIA)
@pytest.mark.parametrize("headline,ok,reason", [
    ("L1 rigidity fell 12%.", True, None),                                       # 12 is in the facts
    ("Fracture Risk Index rose.", False, "blocked wording"),
    ("Lesion volume rose, indicating a positive response to therapy.", False, "blocked wording"),
    ("The lesion measures 42 cm³.", True, None),                                 # 42 is in the facts
    ("The lesion measures 42 cm3.", True, None),
    ("The lesion measures 43 cm³.", False, "number not in facts: 43"),
    ("L1 rigidity fell 13%.", False, "number not in facts: 13"),
])
def test_plan_m2_filter_sentences(via, facts42, headline, ok, reason):
    got_ok, got_reason = verdict(via, facts42, headline)
    assert got_ok is ok, got_reason
    if reason:
        assert reason in got_reason


@pytest.mark.parametrize("via", VIA)
@pytest.mark.parametrize("headline", [
    "L1 is at risk.", "Risks rose at L1.", "L1 is an at-risk level.", "The change at L1 is risky.",
    "This predicts a change at L1.", "L1 was predicted to change.", "The prediction for L1 is unclear.",
    "L1 is unpredictable.",
])
def test_risk_and_predict_are_still_blocked(via, facts42, headline):
    ok, reason = verdict(via, facts42, headline)
    assert not ok and "blocked wording" in reason


@pytest.mark.parametrize("via", VIA)
@pytest.mark.parametrize("headline", [
    "L1 stiffness fell.", "L1 stiffness can fall.", "L1 stiffness falls.", "L1 stiffness is falling.",
    "L1 stiffness has fallen.", "A brisk look at L1.",
])
def test_fall_and_fell_are_allowed(via, facts42, headline):
    """StillHere's filter blocked every form of fall/fell (a care-home falls rule); RainbowBone allows them."""
    ok, reason = verdict(via, facts42, headline)
    assert ok, reason


@pytest.mark.parametrize("via", VIA)
def test_cm3_is_a_unit_not_a_number(via, facts42):
    """reuse.md's spike: StillHere rejected this line because it counted the 3 of cm3 as a number. ("the two
    scans" is shortened to "the scans": "two" is a spelled-out quantity, which the checker rejects.)"""
    ok, reason = verdict(via, facts42, "Lesion volume changed by 42 cm3 between the scans.")
    assert ok, reason
    got = vultr.accept_text(reply("The lesion is 42 cm³ (42 cm3).", "It is 5% of L1."), facts42)
    assert got.numbers_used == [5.0, 42.0]


@pytest.mark.parametrize("via", VIA)
def test_sign_is_compared_as_written(via, facts42):
    """C4 compares values as written: a fact of -21.4 does not admit "fell 21.4%". Pinned as frozen; see the
    S0 note in this stream's status (magnitudes vs signed facts before M5 stiffness facts)."""
    f = facts42.model_copy(update={"stiffness_change_axial_pct": -21.4})
    ok, reason = verdict(via, f, "L1 stiffness fell 21.4%.")
    assert not ok and "number not in facts: 21.4" in reason
    assert verdict(via, f, "L1 stiffness changed by −21.4%.")[0]


# ----------------------------------------------------------------------------------------- one rule set
CORPUS = [
    "L1 rigidity fell 12%.", "Fracture Risk Index rose to 42 since the prior scan.",
    "Lesion volume fell by 12%, indicating a positive response to therapy.", "L1 changed over four months.",
    "The lesion volume doubled.", "Surgical fixation should be considered.", "The vertebra is 2x weaker.",
    "It grew 15mm.", "T11–L5 ladder; L1 lesion 42 cm³ on Day 273.", "This is not a diagnosis.",
    "The lesion is cancer.", "L1 lost about 60% of its strength.", "Nothing fell outside the computed values.",
]


@pytest.mark.parametrize("headline", CORPUS)
def test_vultr_filter_and_checker_agree(facts42, headline):
    """The moved client keeps no wording list of its own: it accepts and rejects exactly as contract.check."""
    assert verdict(VULTR, facts42, headline) == verdict(CHECKER, facts42, headline)


def test_stillhere_wording_lists_are_gone():
    for name in ("BLOCKED_STEMS", "_BLOCKED", "_BLOCKED_REVERSED", "_NO_CONDITIONS", "_NUMBER", "LEVEL_PHRASES",
                 "SYSTEM_PROMPT", "template_briefing", "facts_for_model", "briefing", "abriefing"):
        assert not hasattr(vultr, name), name


# ----------------------------------------------------------------------------------------- reply parsing
def test_think_block_is_stripped(facts42):
    inner = json.dumps({"headline": "L1 fell 12%.", "detail": DETAIL, "numbers_used": [12]})
    body = reply("", content=f"<think>12? 42? 99?</think>{inner}")
    assert vultr.accept_text(body, facts42).headline == "L1 fell 12%."


def test_fenced_json_is_accepted(facts42):
    inner = json.dumps({"headline": "L1 fell 12%.", "detail": DETAIL, "numbers_used": [12]})
    assert vultr.accept_text(reply("", content=f"```json\n{inner}\n```"), facts42).headline == "L1 fell 12%."


def test_numbers_used_is_recomputed_from_the_text(facts42):
    body = reply("", content=json.dumps({"headline": "L1 fell 12%.", "detail": DETAIL, "numbers_used": [999]}))
    assert vultr.accept_text(body, facts42).numbers_used == [12.0]


@pytest.mark.parametrize("body,reason", [
    ({}, "unexpected response body"),
    ({"choices": []}, "unexpected response body"),
    ({"choices": [{"message": {"content": None}}]}, "no text content"),
    (reply("L1 fell 12%.", finish="length"), "cut off at the token limit"),
    (reply("", content="<think>still thinking"), "unfinished reasoning"),
    (reply("", content="   "), "empty text"),
    (reply("", content="L1 fell 12%."), "not JSON"),
    (reply("", content="[1, 2]"), "not a JSON object"),
    (reply("", content='{"headline": "L1 fell 12%."}'), "does not match the output schema"),
    (reply("  ", "x"), "empty headline or detail"),
    (reply("L1 lesion. " * 60), "over 600 characters"),
])
def test_reply_rejections(facts42, body, reason):
    with pytest.raises(vultr.TextRejected) as exc:
        vultr.accept_text(body, facts42)
    assert reason in str(exc.value)


# ----------------------------------------------------------------------------------------- config
def test_config_from_env():
    cfg = vultr.VultrConfig.from_env({"VULTR_INFERENCE_API_KEY": FAKE_KEY, "VULTR_BASE_URL": "http://x/v1/",
                                      "VULTR_TIMEOUT_S": "99", "VULTR_MAX_TOKENS": "1",
                                      "VULTR_REASONING_EFFORT": ""})
    assert cfg.enabled and cfg.base_url == "http://x/v1"
    assert cfg.timeout_s == 30.0 and cfg.max_tokens == 32 and cfg.reasoning_effort is None
    bad = vultr.VultrConfig.from_env({"VULTR_TIMEOUT_S": "nan", "VULTR_MAX_TOKENS": "lots"})
    assert (bad.timeout_s, bad.max_tokens, bad.reasoning_effort) == (6.0, 400, "minimal")


def test_status_never_contains_the_key():
    s = vultr.status({"VULTR_API_KEY": FAKE_KEY})
    assert s["enabled"] is True and FAKE_KEY not in json.dumps(s) and FAKE_KEY[4:12] not in json.dumps(s)


# ----------------------------------------------------------------------------------------- client (mock transport)
class Server:
    """httpx.MockTransport handler: answers from a script of responses and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, int):
            return httpx.Response(nxt, json={"error": "x"})
        return httpx.Response(200, json=nxt)


def gen(task, facts, server, env=None):
    env = {"VULTR_API_KEY": FAKE_KEY} if env is None else env
    return asyncio.run(vultr.agenerate(task, facts, env=env, transport=httpx.MockTransport(server)))


@pytest.fixture
def no_jitter(monkeypatch):
    monkeypatch.setattr(vultr.random, "uniform", lambda a, b: 0.0)


def test_accepted_reply_is_vultr_and_sends_only_the_facts(facts):
    server = Server(reply(*FIXTURE_REPLY))
    out = gen("care_team_summary", facts, server)
    assert out["source"] == "vultr" and out["model"] == "qwen3.8-flash-next"
    assert (out["text"].headline, out["text"].detail) == FIXTURE_REPLY
    assert contract.check(out["text"], facts).ok
    (req,) = server.requests
    assert req.method == "POST" and req.url.path.endswith("/chat/completions")
    assert req.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(req.content)
    assert body["messages"] == contract.messages_for("care_team_summary", facts_json(facts))
    assert body["messages"][1]["content"] == facts_json(facts)
    assert (body["model"], body["max_tokens"], body["reasoning_effort"]) == ("qwen3.8-flash-next", 400, "minimal")


def test_patient_explanation_uses_its_own_prompt(facts):
    server = Server(reply(*FIXTURE_REPLY))
    assert gen("patient_explanation", facts, server)["source"] == "vultr"
    body = json.loads(server.requests[0].content)
    assert body["messages"] == contract.messages_for("patient_explanation", facts_json(facts))


def test_accepted_reply_is_cached(facts):
    server = Server(reply(*FIXTURE_REPLY))
    first, second = gen("care_team_summary", facts, server), gen("care_team_summary", facts, server)
    assert first == second and len(server.requests) == 1


def test_rejected_reply_gives_the_template_and_is_not_asked_again(facts):
    server = Server(reply("Fracture Risk Index rose.", "L1 is at risk."))
    for _ in range(2):
        out = gen("care_team_summary", facts, server)
        assert out == {"text": template("care_team_summary", facts), "source": "template", "model": None}
    assert len(server.requests) == 1
    other = facts.model_copy(update={"scan_id": "other"})  # a rejection is per facts, not a cooldown
    gen("care_team_summary", other, server)
    assert len(server.requests) == 2


def test_one_retry_on_5xx_then_success(facts, no_jitter):
    server = Server(503, reply(*FIXTURE_REPLY))
    assert gen("care_team_summary", facts, server)["source"] == "vultr"
    assert len(server.requests) == 2


@pytest.mark.parametrize("failure", [500, 429, httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_failure_gives_the_template_and_a_60s_cooldown(facts, no_jitter, failure):
    server = Server(failure)
    out = gen("care_team_summary", facts, server)
    assert out["source"] == "template" and out["text"] == template("care_team_summary", facts)
    calls = len(server.requests)
    assert calls == (2 if isinstance(failure, int) else 1)  # status codes retry once; transport errors do not
    other = facts.model_copy(update={"scan_id": "other"})
    assert gen("care_team_summary", other, server)["source"] == "template"
    assert len(server.requests) == calls  # cooling down: Vultr is skipped
    vultr.reset_state()
    gen("care_team_summary", other, server)
    assert len(server.requests) > calls


def test_non_json_body_gives_the_template(facts):
    def handler(request):
        return httpx.Response(200, text="<html>gateway</html>")

    out = asyncio.run(vultr.agenerate("care_team_summary", facts, env={"VULTR_API_KEY": FAKE_KEY},
                                      transport=httpx.MockTransport(handler)))
    assert out["source"] == "template"


def test_unknown_task_is_refused(facts):
    with pytest.raises(ValueError):
        gen("diagnosis", facts, Server(reply(*FIXTURE_REPLY)))


# ----------------------------------------------------------------------------------------- AI_CHAIN pinned
def test_ai_chain_is_pinned_to_the_template():
    assert chain_mod.DEFAULT_CHAIN == ("template",)
    assert Chain.from_env({}).order == ("template",)


def test_chain_never_reaches_the_vultr_client_before_m6(facts, monkeypatch):
    """Even with every key set and every tier named, the chain answers from the template and the moved
    Vultr client is never called (its tier is a stub until M6)."""
    async def refuse(*args, **kwargs):
        raise AssertionError("the chain reached the Vultr client")

    monkeypatch.setattr(vultr, "_call", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    env = {"AI_CHAIN": "gemini,groq,vultr,local,template", "GEMINI_API_KEY": "x" * 39, "GROQ_API_KEY": "y" * 40,
           "VULTR_API_KEY": FAKE_KEY, "LOCAL_LLM_BASE_URL": "http://127.0.0.1:9/v1"}
    gen_ = asyncio.run(Chain.from_env(env).run("care_team_summary", facts))
    assert gen_.source == "template"
    vultr_attempt = next(a for a in gen_.attempts if a.provider == "vultr")
    assert (vultr_attempt.outcome, vultr_attempt.reason) == ("unavailable", STUB_REASON)
