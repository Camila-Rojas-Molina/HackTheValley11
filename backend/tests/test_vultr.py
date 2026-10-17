"""Vultr layer: the briefing client, its isolation from KAZE, and the deploy/API docs.

No test here touches the network: every Vultr call goes through httpx.MockTransport,
and the no-key tests pass an env without a key.
"""
from __future__ import annotations

import ast
import asyncio
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app import vultr
from tests.test_wording import BANNED, PATTERN

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
APP = BACKEND / "app"

KEY = "test-vultr-key-not-real"
ENV = {"VULTR_API_KEY": KEY, "VULTR_MODEL": "test-model", "VULTR_BASE_URL": "https://vultr.example.test/v1"}
NO_KEY: dict = {}

# Every module on the KAZE check-in / escalation path. None may import app.vultr.
KAZE_MODULES = (
    "app.states", "app.engine", "app.timers", "app.outbox", "app.telegram", "app.rules", "app.sensors",
    "app.messages", "app.audit", "app.security", "app.deps", "app.db", "app.config",
    "app.routes.device", "app.routes.hr", "app.routes.sessions", "app.routes.contacts", "app.routes.demo",
    "app.routes.health",
)

# A hand-made card in the shared PCC card shape, to exercise the template and the response
# checks with round numbers. Not an engine card; vultr.sample_card() is (see the CLI tests).
FIXTURE_CARD = {
    "resident_id": "SYN-007",
    "display_name": "Resident 7",
    "synthetic": True,
    "score": 64,
    "level": "watch",
    "rules": [
        {"rule": "sway_drift", "observed": 41.0, "baseline": 30.0, "unit": "mg", "contribution": 30,
         "explanation": "Standing sway 37% above the 48 h baseline"},
        {"rule": "rise_to_settle", "observed": 19.0, "baseline": 11.0, "unit": "s", "contribution": 24,
         "explanation": "Heart rate took 19 s to settle after standing, usual 11 s"},
        {"rule": "night_restlessness", "observed": 9, "baseline": 4, "unit": "periods", "contribution": 10,
         "explanation": "9 restless periods last night, usual 4"},
    ],
    "suggested_check": "CNA: watch one sit-to-stand and ask how the resident slept",
    "briefing": "",
}
CARD = copy.deepcopy(FIXTURE_CARD)
CARD["display_name"] = "Rosa Example"
CARD["rules"][0]["explanation"] = "Rosa Example swayed 37% more than her 48 h baseline"

def variant(i: int) -> dict:
    """Same numbers as CARD (so GOOD stays valid), different cache key."""
    return dict(CARD, suggested_check=f"CNA: watch one sit-to-stand (variant {chr(97 + i % 26)})")


GOOD = ("The resident is worth watching with a score of 64 out of 100: standing sway was 41 mg against a usual "
        "30 mg and settling after standing took 19 s against 11 s. Please watch one sit-to-stand today.")


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    vultr.reset_state()
    monkeypatch.setattr(vultr.random, "uniform", lambda a, b: 0.0)
    yield
    vultr.reset_state()


def completion(text) -> dict:
    return {"id": "x", "object": "chat.completion", "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}


class Recorder:
    """MockTransport handler: answers from a script and records every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=completion(answer))

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


# --- shared contract ---------------------------------------------------------------
def test_no_key_returns_the_template_without_any_request():
    rec = Recorder(GOOD)
    out = vultr.briefing(CARD, env=NO_KEY, transport=rec.transport)
    assert out == {"text": vultr.template_briefing(CARD), "source": "template", "model": None}
    assert vultr.briefing_text(CARD, env=NO_KEY, transport=rec.transport) == vultr.template_briefing(CARD)
    assert rec.requests == []
    assert vultr.status(NO_KEY)["enabled"] is False


def test_contract_signature_and_default_env(monkeypatch):
    monkeypatch.delenv("VULTR_API_KEY", raising=False)
    monkeypatch.delenv("VULTR_INFERENCE_API_KEY", raising=False)
    assert vultr.briefing_text(CARD) == vultr.template_briefing(CARD)  # contract: briefing_text(card) -> str
    assert isinstance(vultr.briefing_text(CARD), str)


def test_template_is_deterministic_labelled_and_complete():
    a, b = vultr.template_briefing(CARD), vultr.template_briefing(copy.deepcopy(CARD))
    assert a == b
    assert a.startswith("Rosa Example (SYNTHETIC): worth watching (score 64/100).")
    assert "Suggested check: CNA: watch one sit-to-stand" in a
    # Highest contribution first, at most three rules.
    assert a.index("swayed 37%") < a.index("19 s to settle") < a.index("9 restless periods")
    assert not PATTERN.search(a)


@pytest.mark.parametrize("card", [
    {}, {"rules": None}, {"rules": ["x", 3, None]}, {"score": float("nan"), "level": "??"},
    {"rules": [{"rule": "sway_drift", "observed": 41.25, "baseline": 30, "unit": "mg", "contribution": 5}]},
    {"rules": [{"rule": "r", "contribution": "high"}], "suggested_check": 7}, None, "not a card",
])
def test_template_never_raises(card):
    text = vultr.template_briefing(card)
    assert isinstance(text, str) and text
    assert not PATTERN.search(text)


def test_template_numbers_without_explanation():
    text = vultr.template_briefing({"display_name": "R", "synthetic": True, "score": 40, "level": "watch", "rules": [
        {"rule": "sway_drift", "observed": 41.25, "baseline": 30.0, "unit": "mg", "contribution": 5}]})
    assert "sway drift 41.2 mg against a usual 30 mg" in text


def test_template_skips_an_explanation_that_breaks_the_wording_rules():
    stem = BANNED[4]
    card = {"display_name": "R", "level": "watch", "score": 50, "rules": [
        {"rule": "sway_drift", "observed": 41, "baseline": 30, "unit": "mg", "contribution": 9,
         "explanation": f"sway {stem}ed"}]}
    text = vultr.template_briefing(card)
    assert not PATTERN.search(text)
    assert "sway drift 41 mg against a usual 30 mg" in text


# --- the Vultr call ----------------------------------------------------------------
def test_with_key_calls_openai_compatible_chat_completions():
    rec = Recorder(GOOD)
    out = vultr.briefing(CARD, env=ENV, transport=rec.transport)
    assert out == {"text": f"Rosa Example (SYNTHETIC): {GOOD}", "source": "vultr", "model": "test-model"}
    (req,) = rec.requests
    assert req.method == "POST"
    assert str(req.url) == "https://vultr.example.test/v1/chat/completions"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(req.content)
    assert body["model"] == "test-model"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["max_tokens"] == vultr.DEFAULT_MAX_TOKENS
    assert body["reasoning_effort"] == "minimal"


def test_only_rule_findings_leave_the_server():
    rec = Recorder(GOOD)
    vultr.briefing(CARD, env=ENV, transport=rec.transport)
    sent = rec.requests[0].content.decode()
    for private in ("Rosa", "Example", "SYN-007"):
        assert private not in sent, private
    user = json.loads(sent)["messages"][1]["content"]
    for key in ("resident_id", "display_name", "briefing"):
        assert key not in user, key
    facts = json.loads(user)
    assert set(facts) == {"synthetic", "score", "level", "rules", "suggested_check"}
    assert facts["rules"][0]["explanation"].startswith("the resident swayed")


def test_default_endpoint_and_model_match_the_vultr_docs():
    cfg = vultr.VultrConfig.from_env({"VULTR_API_KEY": KEY})
    assert cfg.base_url == "https://api.vultrinference.com/v1"
    assert cfg.model == "qwen3.8-flash-next"
    alias = vultr.VultrConfig.from_env({"VULTR_INFERENCE_API_KEY": KEY})
    assert alias.enabled and alias.api_key == KEY
    assert "reasoning_effort" not in vultr.request_body(CARD, vultr.VultrConfig.from_env(
        {"VULTR_API_KEY": KEY, "VULTR_REASONING_EFFORT": ""}))


def test_status_never_contains_the_key():
    st = vultr.status(ENV)
    assert st["enabled"] is True and KEY not in json.dumps(st)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retries_once_on_429_and_5xx_then_uses_vultr(status):
    rec = Recorder(httpx.Response(status, json={"error": "busy"}), GOOD)
    out = vultr.briefing(CARD, env=ENV, transport=rec.transport)
    assert out["source"] == "vultr" and len(rec.requests) == 2


def test_two_failures_fall_back_and_cool_down():
    rec = Recorder(httpx.Response(503), httpx.Response(503), GOOD)
    out = vultr.briefing(CARD, env=ENV, transport=rec.transport)
    assert out["source"] == "template" and out["text"] == vultr.template_briefing(CARD)
    assert len(rec.requests) == 2
    # Cooling down: a refresh gets the template at once, without a request.
    other = variant(12)
    assert vultr.briefing(other, env=ENV, transport=rec.transport)["source"] == "template"
    assert len(rec.requests) == 2
    vultr.reset_state()
    assert vultr.briefing(other, env=ENV, transport=rec.transport)["source"] == "vultr"


@pytest.mark.parametrize("answer", [
    httpx.ReadTimeout("slow"), httpx.ConnectError("down"),
    httpx.Response(401, json={"error": {"code": 401, "message": "No API key provided", "type": "auth_error"}}),
    httpx.Response(400, json={"error": "bad model"}),
    httpx.Response(200, content=b"<html>not json</html>"),
])
def test_errors_and_timeouts_fall_back_to_the_template(answer):
    rec = Recorder(answer)
    out = vultr.briefing(CARD, env=ENV, transport=rec.transport)
    assert out == {"text": vultr.template_briefing(CARD), "source": "template", "model": None}
    assert len(rec.requests) == 1


@pytest.mark.parametrize("text", [
    "", "   ", None, "x" * (vultr.MAX_CHARS + 1),
    f"The resident may be {BANNED[2]}.",
    f"Sway changes were {BANNED[4]}ed today.",
    "This pattern may point to an infection; check today.",
    "Expect a decline within 48-72 hours.",
    "Possible early dementia; check today.",
])
def test_text_that_breaks_the_rules_is_never_shown(text):
    rec = Recorder(text)
    out = vultr.briefing(CARD, env=ENV, transport=rec.transport)
    assert out["source"] == "template" and out["text"] == vultr.template_briefing(CARD)
    # Rejected per card, not a cooldown: the next card still asks Vultr.
    rec.answers = [GOOD]
    assert vultr.briefing(variant(1), env=ENV, transport=rec.transport)["source"] == "vultr"


def test_numbers_must_come_from_the_card():
    rec = Recorder("The resident is worth watching (score 64/100); sway rose 55% in 3 days. Check today.")
    assert vultr.briefing(CARD, env=ENV, transport=rec.transport)["source"] == "template"
    # The card's own numbers, including those inside explanations, are fine.
    ok = "Score 64 out of 100: sway 41 mg against 30 mg, 37% above the 48 h baseline. Watch one sit-to-stand."
    assert vultr.accept_text(completion(ok), vultr.facts_for_model(CARD)) == ok


def test_cut_off_or_unfinished_reasoning_falls_back():
    cut = completion(GOOD)
    cut["choices"][0]["finish_reason"] = "length"
    rec = Recorder(httpx.Response(200, json=cut))
    assert vultr.briefing(CARD, env=ENV, transport=rec.transport)["source"] == "template"
    rec2 = Recorder(f"<think>still thinking about {GOOD}")
    assert vultr.briefing(variant(2), env=ENV, transport=rec2.transport)["source"] == "template"


def test_unexpected_body_falls_back():
    rec = Recorder(httpx.Response(200, json={"choices": []}))
    assert vultr.briefing(CARD, env=ENV, transport=rec.transport)["source"] == "template"


def test_reasoning_and_markdown_are_stripped():
    rec = Recorder(f"<think>scratch work</think>**{GOOD}**")
    out = vultr.briefing(CARD, env=ENV, transport=rec.transport)
    assert out["text"] == f"Rosa Example (SYNTHETIC): {GOOD}"


def test_cached_per_card():
    rec = Recorder(GOOD)
    first = vultr.briefing(CARD, env=ENV, transport=rec.transport)
    again = vultr.briefing(copy.deepcopy(CARD), env=ENV, transport=rec.transport)
    assert first == again and len(rec.requests) == 1
    vultr.briefing(variant(65), env=ENV, transport=rec.transport)
    assert len(rec.requests) == 2


def test_async_twin_matches():
    rec = Recorder(httpx.Response(500), GOOD)
    out = asyncio.run(vultr.abriefing(CARD, env=ENV, transport=rec.transport))
    assert out["source"] == "vultr" and len(rec.requests) == 2
    vultr.reset_state()
    rec2 = Recorder(httpx.ReadTimeout("slow"))
    text = asyncio.run(vultr.abriefing_text(CARD, env=ENV, transport=rec2.transport))
    assert text == vultr.template_briefing(CARD)
    assert asyncio.run(vultr.abriefing_text(CARD, env=NO_KEY)) == vultr.template_briefing(CARD)


def test_blocked_stems_match_the_wording_test():
    assert vultr.BLOCKED_STEMS == BANNED


def test_prompt_and_sample_follow_the_wording_rules():
    assert not PATTERN.search(vultr.SYSTEM_PROMPT)
    sample = vultr.sample_card()
    assert not PATTERN.search(json.dumps(sample, ensure_ascii=False))
    assert {"resident_id", "display_name", "synthetic", "score", "level", "rules", "suggested_check",
            "briefing"} <= set(sample)
    assert all(set(vultr.RULE_FIELDS) <= set(r) for r in sample["rules"])


# --- isolation from KAZE -----------------------------------------------------------
def _module_path(name: str) -> Path:
    return BACKEND.joinpath(*name.split(".")).with_suffix(".py")


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            found.add(mod)
            found.update(f"{mod}.{a.name}" for a in node.names)
    return found


@pytest.mark.parametrize("module", KAZE_MODULES)
def test_kaze_modules_never_import_vultr(module):
    path = _module_path(module)
    assert path.exists(), f"{module} moved; update KAZE_MODULES"
    assert not [i for i in _imports(path) if "vultr" in i.lower()], module
    assert "vultr" not in path.read_text(encoding="utf-8").lower(), module


def test_importing_every_kaze_module_does_not_load_vultr():
    code = ("import importlib, sys\n"
            f"for m in {list(KAZE_MODULES)!r}: importlib.import_module(m)\n"
            "print(sorted(m for m in sys.modules if 'vultr' in m))\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


def test_vultr_depends_on_nothing_in_kaze():
    # Its only in-app import is the care rule engine (for the CLI check's sample card), which
    # itself imports nothing from KAZE (test_care_isolation.py).
    own = {i for i in _imports(APP / "vultr.py") if i.startswith(("app", ".")) or i == ""}
    assert own <= {"", ".care"}, own
    assert not any(i.split(".")[0] in {"states", "engine", "timers", "outbox", "telegram"} for i in
                   _imports(APP / "vultr.py"))


# --- deploy files and API docs -----------------------------------------------------------
def _route_lines() -> list[str]:
    """Every route the app serves: the OpenAPI paths plus FastAPI's own docs pages."""
    from app.config import Settings
    from app.main import create_app
    application = create_app(Settings(database_url="postgresql:///unused"), start_workers=False)
    lines = {f"{m.upper()} {path}" for path, ops in application.openapi()["paths"].items() for m in ops}
    for r in application.routes:  # plain Starlette routes (/openapi.json, /docs, /redoc, ...)
        for m in getattr(r, "methods", None) or ():
            if m != "HEAD" and getattr(r, "path", None):
                lines.add(f"{m} {r.path}")
    return sorted(lines)


def test_api_doc_lists_every_route():
    doc = (REPO / "docs" / "API.md").read_text(encoding="utf-8")
    lines = _route_lines()
    assert "POST /v1/device/heartbeat" in lines and "GET /v1/health" in lines and "GET /docs" in lines
    assert len(lines) >= 40
    missing = [line for line in lines if f"`{line}`" not in doc]
    assert not missing, "docs/API.md does not document: " + ", ".join(missing)


def test_api_doc_has_the_line_protocol():
    doc = (REPO / "docs" / "API.md").read_text(encoding="utf-8")
    for line in ("H,<boot_id>,<seq>,<enmo60_mg>,<tilt_deg>,<fsr_base_mv>,<offbody>",
                 "E,<boot_id>:<seq>,<type>,<json_payload>", "C,<led>,<blink_n>,<challenge_id>"):
        assert line in doc


def test_docker_files_keep_secrets_and_dev_data_out():
    ignore = {ln.strip() for ln in (REPO / ".dockerignore").read_text().splitlines()}
    for pattern in ("**/.env", "**/.env.*", "**/.venv", "**/.pgdata*", ".git"):
        assert pattern in ignore, pattern
    dockerfile = (REPO / "Dockerfile").read_text()
    assert not re.search(r"\.env\b", dockerfile.replace(".env.example", "")), "Dockerfile mentions an env file"
    assert "USER " in dockerfile
    compose = (REPO / "docker-compose.yml").read_text()
    assert "timescale/timescaledb:latest-pg16" in compose
    assert re.search(r"^\s{2}migrate:", compose, re.M) and "service_completed_successfully" in compose
    assert not re.search(r"VULTR_API_KEY\s*[:=]\s*\S", compose)
    assert not re.search(r"TELEGRAM_BOT_TOKEN\s*[:=]\s*\S", compose)


def test_env_example_has_only_placeholders_for_vultr():
    text = (BACKEND / ".env.example").read_text()
    assert re.search(r"^VULTR_API_KEY=$", text, re.M)
    assert re.search(r"^VULTR_MODEL=qwen3\.8-flash-next$", text, re.M)


def test_cli_check_uses_a_card_the_rule_engine_produces():
    """`python -m app.vultr` must exercise a real card: a green check then means real cards get through."""
    from app import care
    card = vultr.sample_card()
    assert {r["rule"] for r in card["rules"]} == set(care.RULES)
    assert card["level"] == care.level_for(card["score"], any(r["top_band"] for r in card["rules"]))
    assert card["level"] in care.FLAGGED and card["synthetic"] is True
    assert {"s", "min"} == {r["unit"] for r in card["rules"]}  # sway is experimental, not a scored rule


def test_template_briefing_does_not_call_the_baseline_recent():
    text = vultr.template_briefing(vultr.sample_card())
    assert "recent baseline" not in text and "baseline from the start of the record" in text
