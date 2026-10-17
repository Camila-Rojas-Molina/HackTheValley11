"""Shared fixtures for the AI-layer tests (S5). No database, no server, no network.

Every test runs with the AI env vars removed, so a developer's real keys or chain can never leak into a
test; tests that need a config pass an explicit env mapping.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.ai import contract
from app.schemas import Facts

AI_ENV = (
    "AI_CHAIN", "AI_OFFLINE", "GEMINI_API_KEY", "GEMINI_MODEL", "GOOGLE_API_KEY", "GROQ_API_KEY",
    "GROQ_MODEL", "VULTR_API_KEY", "VULTR_INFERENCE_API_KEY", "VULTR_BASE_URL", "VULTR_MODEL",
    "VULTR_TIMEOUT_S", "VULTR_MAX_TOKENS", "VULTR_REASONING_EFFORT", "LOCAL_LLM_BASE_URL", "LOCAL_LLM_MODEL",
)
FACTS_FIXTURE = "fixtures/ai/facts.myel018-L1-d273.json"
SUMMARY_FIXTURE = "fixtures/scans/myel018-L1-d273/summary.json"
EXPLANATION_FIXTURE = "fixtures/scans/myel018-L1-d273/explanation.json"


@pytest.fixture(autouse=True)
def _no_ai_env(monkeypatch):
    for name in AI_ENV:
        monkeypatch.delenv(name, raising=False)


def load_fixture(rel: str) -> dict:
    with open(contract.contracts_dir() / rel, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def facts() -> Facts:
    return Facts.model_validate(load_fixture(FACTS_FIXTURE))


@pytest.fixture
def fixed_clock():
    """Clock pinned to the golden fixtures' generated_at."""
    at = datetime(2026, 10, 17, 20, 45, 0, tzinfo=timezone.utc)
    return lambda: at
