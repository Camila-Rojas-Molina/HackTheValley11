"""Providers (C4 "Provider"): every stub reports unavailable, keys are shown by shape only, and the moved
Vultr client imports cleanly and reports unavailable without a key."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.ai.chain import build_providers
from app.ai.providers import gemini, openai_compat, vultr
from app.ai.providers.base import Provider, ProviderUnavailable, key_shape

API_DIR = Path(__file__).resolve().parents[2]
AI_DIR = API_DIR / "app" / "ai"
SECRETISH = {"GEMINI_API_KEY": "AIza.fake.not.a.key.0000", "GROQ_API_KEY": "gsk_FAKEFAKEFAKEFAKEFAKE9876",
             "VULTR_API_KEY": "VULTRFAKEFAKEFAKEFAKE4242", "LOCAL_LLM_BASE_URL": "http://127.0.0.1:9/v1"}


@pytest.mark.parametrize("env", [{}, SECRETISH])
def test_every_model_provider_reports_unavailable(env):
    providers = build_providers(env)
    for name in ("gemini", "groq", "vultr", "local"):
        p = providers[name]
        assert isinstance(p, Provider)
        s = p.status()
        assert s["name"] == name and s["available"] is False
        assert s["configured"] is bool(env)
        with pytest.raises(ProviderUnavailable):
            asyncio.run(p.generate([{"role": "user", "content": "{}"}]))
    assert providers["template"].status()["available"] is True


def test_status_never_contains_a_key():
    dumped = json.dumps([p.status() for p in build_providers(SECRETISH).values()])
    for name, value in SECRETISH.items():
        if name.endswith("_KEY"):
            assert value not in dumped
            assert value[4:12] not in dumped


def test_key_shape():
    assert key_shape(None) is None and key_shape("") is None
    assert key_shape("gsk_abcdefghijklmnop") == "gsk_****(20 chars)"
    assert key_shape("abc") == "****(3 chars)"  # a short key shows nothing of itself


def test_defaults_match_the_contract():
    assert gemini.GeminiProvider({}).model == "gemini-3.8-flash"
    assert openai_compat.groq({}).model == "openai/gpt-oss-120b"
    assert openai_compat.groq({}).base_url == "https://api.groq.com/openai/v1"
    assert openai_compat.vultr({}).model == "qwen3.8-flash-next"
    assert openai_compat.vultr({}).timeout == 6.0
    assert openai_compat.local({}).model is None and openai_compat.local({}).base_url is None


def test_google_api_key_is_flagged():
    s = gemini.GeminiProvider({"GEMINI_API_KEY": "x" * 39, "GOOGLE_API_KEY": "y" * 39}).status()
    assert "GOOGLE_API_KEY" in s["warning"]


# ----------------------------------------------------------------------------------------- moved Vultr client
def test_moved_vultr_imports_cleanly_in_a_fresh_interpreter():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("VULTR_", "AI_"))}
    out = subprocess.run([sys.executable, "-c", "import app.ai.providers.vultr as v; print(v.status()['enabled'])"],
                         cwd=API_DIR, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_moved_vultr_has_no_reference_to_deleted_modules():
    assert not hasattr(vultr, "sample_card")
    tree = ast.parse((AI_DIR / "providers" / "vultr.py").read_text(encoding="utf-8"))
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not imported & {"care", "engine", "states", "timers", "rules", "telegram", "sensors", "messages"}
    assert all(n.level == 0 for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))


def test_moved_vultr_reports_unavailable_without_a_key():
    assert vultr.VultrConfig.from_env({}).enabled is False
    assert vultr.status({})["enabled"] is False
    s = openai_compat.vultr({}).status()
    assert s["configured"] is False and s["available"] is False and s["key"] is None


def test_moved_vultr_without_a_key_never_touches_the_network(facts):
    def refuse(request):
        raise AssertionError(f"network call to {request.url}")

    vultr.reset_state()
    result = asyncio.run(vultr.agenerate("care_team_summary", facts, env={},
                                         transport=httpx.MockTransport(refuse)))
    assert result["source"] == "template"
