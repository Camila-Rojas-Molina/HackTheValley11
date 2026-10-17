"""Senior-mode cards worded by Vultr: GET /v1/care/alerts sends each "watch" / "check today" card to
app.vultr and shows the model's text only as the briefing. Score, level, rules and suggested check
come from the rule engine and are never changed; without a key, on an error or on text that breaks
the care wording rules, the card keeps the rule template. Vultr is mocked here (no network)."""
from __future__ import annotations

import functools
import json
import re

import httpx
import pytest

from app import care, vultr
from tests.test_care_db import END, seed
from tests.test_care_wording import CARE_BLOCKLIST, hits

ENV = {"VULTR_API_KEY": "test-vultr-key-not-real", "VULTR_MODEL": "test-model",
       "VULTR_BASE_URL": "https://vultr.example.test/v1"}
TEXT = "The resident should be seen in person today. Please follow the suggested check on the card."


def completion(text: str) -> dict:
    return {"id": "x", "object": "chat.completion", "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}]}


class FakeVultr:
    """Answers every chat completion with one fixed reply and records the request bodies."""

    def __init__(self, reply):
        self.reply = reply
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        if isinstance(self.reply, httpx.Response):
            return self.reply
        return httpx.Response(200, json=completion(self.reply))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for name in ("VULTR_API_KEY", "VULTR_INFERENCE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(vultr.random, "uniform", lambda a, b: 0.0)
    vultr.reset_state()
    yield
    vultr.reset_state()


def use_fake(monkeypatch, fake: FakeVultr) -> None:
    """Route calls to vultr.abriefing go to the fake endpoint with a test key."""
    real = vultr.abriefing
    monkeypatch.setattr(vultr, "abriefing", functools.partial(real, env=ENV, transport=httpx.MockTransport(fake)))


async def stored_cards(db) -> dict:
    async with db.connection() as conn:
        rows = await (await conn.execute(
            "SELECT card FROM care_alerts WHERE as_of = (SELECT max(as_of) FROM care_alerts)")).fetchall()
    return {r["card"]["resident_id"]: r["card"] for r in rows}


async def test_without_a_key_cards_keep_the_rule_template(client, db):
    await seed(client)
    stored = await stored_cards(db)
    cards = (await client.get("/v1/care/alerts?include_low=true")).json()["alerts"]
    assert len(cards) == 20
    for c in cards:
        assert c["briefing_source"] == "template"
        assert c["briefing"] == stored[c["resident_id"]]["briefing"]


async def test_vultr_words_only_the_briefing_of_flagged_cards(client, db, monkeypatch):
    await seed(client)
    stored = await stored_cards(db)
    fake = FakeVultr(TEXT)
    use_fake(monkeypatch, fake)

    cards = (await client.get("/v1/care/alerts")).json()["alerts"]
    assert len(cards) == 5 and len(fake.bodies) == 5
    for c in cards:
        assert c["briefing_source"] == "vultr"
        assert c["briefing"] == f"{c['display_name']} (SYNTHETIC): {TEXT}"
        before = stored[c["resident_id"]]
        # The rule engine's decision is untouched: everything but the briefing is the stored card.
        assert {k: v for k, v in c.items() if k not in ("briefing", "briefing_source")} == \
               {k: v for k, v in before.items() if k != "briefing"}
    # Only rule findings left the server: no names, no resident ids.
    sent = json.dumps(fake.bodies)
    assert "Synthetic resident" not in sent and not re.search(r"R-\d\d", sent)

    # "low" cards are not sent, and flagged ones come from the cache on a refresh.
    everyone = (await client.get("/v1/care/alerts?include_low=true")).json()["alerts"]
    assert len(fake.bodies) == 5
    assert sorted(c["briefing_source"] for c in everyone) == ["template"] * 15 + ["vultr"] * 5
    for c in everyone:
        assert (c["briefing_source"] == "vultr") == (c["level"] != care.LEVEL_LOW)
    # The stored cards still hold the rule template.
    assert await stored_cards(db) == stored


@pytest.mark.parametrize("reply", [httpx.Response(500), httpx.Response(401),
                                   "Watch for falls: the risk is high this week."])
async def test_errors_and_rejected_text_keep_the_template(client, db, monkeypatch, reply):
    await seed(client)
    stored = await stored_cards(db)
    use_fake(monkeypatch, FakeVultr(reply))
    r = await client.get("/v1/care/alerts")
    assert r.status_code == 200
    for c in r.json()["alerts"]:
        assert c["briefing_source"] == "template"
        assert c["briefing"] == stored[c["resident_id"]]["briefing"]
    assert not hits(r.text), hits(r.text)


# One phrase per senior-mode wording rule. The model's text must be refused for every one.
CARE_SAMPLES = [
    "Possible hypotension after standing.", "An orthostatic pattern is likely.", "This may be POTS.",
    "Check for dehydration.", "Could be a UTI.", "Ask about urinary symptoms.", "Rule out sepsis.",
    "A septic picture.", "An infection is possible.", "Early delirium.", "Dementia is progressing.",
    "A Parkinson tremor.", "A stroke is possible.", "Syncope after standing.", "The resident is frail.",
    "A disease is progressing.", "A sleep disorder.", "Sedation may explain it.", "Offer hydration.",
    "We predict a decline.", "The forecast is poor.", "Act now to prevent harm.", "The resident will fall soon.",
    "Watch for falls.", "A fall today.", "High risk tonight.", "This is an early warning.",
    "Expect change in 48-72 hours.", "Expect change in 48–72 hours.", "Expect change in 48—72 hours.",
    "Within 72 h.", "Skin looks pale.", "Temperature rose.", "A fever is likely.",
    # Plural and inflected forms, and forward-looking phrasing without the words above.
    "These changes are often seen with infections or UTIs.", "This could suggest strokes or other diseases.",
    "Possible disorders or syndromes or illnesses.", "Pneumonias are common.", "The resident is likely falling.",
    "The resident has fallen.", "She fell twice.", "Likely to change within 2 days.",
    "Expect a change in the next few days.", "Check again tomorrow.",
]


def test_samples_cover_every_care_wording_rule():
    for pattern in CARE_BLOCKLIST:
        assert any(re.search(pattern, s, re.IGNORECASE) for s in CARE_SAMPLES), pattern
    assert all(hits(s) for s in CARE_SAMPLES)


@pytest.mark.parametrize("text", CARE_SAMPLES)
def test_model_text_breaking_the_care_wording_is_refused(text):
    with pytest.raises(vultr.BriefingRejected):
        vultr.accept_text(completion(f"The resident is worth watching. {text} Watch one sit-to-stand."))


def test_every_text_the_care_wording_check_flags_is_refused_by_the_briefing_filter():
    """The two lists must not drift apart: anything CARE_BLOCKLIST flags, accept_text refuses."""
    corpus = CARE_SAMPLES + [s.lower() for s in CARE_SAMPLES] + [s.upper() for s in CARE_SAMPLES]
    for text in corpus:
        if hits(text):
            with pytest.raises(vultr.BriefingRejected):
                vultr.accept_text(completion(text))
