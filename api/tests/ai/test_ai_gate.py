"""Galit's gate and the M8 stubs: no live AI code before M6, and the copilot contract is pinned."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import get_args

import pytest

from app.ai import copilot, tools
from app.ai.chain import Chain
from app.schemas import CopilotRequest, CopilotTool

AI_DIR = Path(__file__).resolve().parents[2] / "app" / "ai"
NETWORK_MODULES = {"httpx", "requests", "aiohttp", "urllib", "socket", "google", "groq", "openai", "mlx_lm"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    return {name.split(".")[0] for name in names}


def test_no_network_client_outside_the_moved_vultr_client():
    """Until M6 the only network code in app/ai is the moved StillHere Vultr client, which no tier calls."""
    offenders = {str(p.relative_to(AI_DIR)): _imports(p) & NETWORK_MODULES
                 for p in AI_DIR.rglob("*.py") if p.name != "vultr.py"}
    assert {k: v for k, v in offenders.items() if v} == {}


def test_ai_layer_imports_no_database_or_web_framework():
    """The AI layer words numbers; it reads them from the API, never from the DB or the request directly."""
    for p in AI_DIR.rglob("*.py"):
        assert not _imports(p) & {"psycopg", "psycopg_pool", "fastapi", "starlette"}, p


def test_copilot_tools_match_the_contract():
    assert set(tools.TOOL_NAMES) == set(get_args(CopilotTool))
    with pytest.raises(NotImplementedError):
        asyncio.run(tools.call_tool("list_scans", {}, patient_id="demo-a"))


def test_copilot_is_not_live_before_m8():
    req = CopilotRequest(question="How has L1 changed since the first scan?", patient_id="demo-a")
    with pytest.raises(NotImplementedError, match="M8"):
        asyncio.run(copilot.answer(req, chain=Chain.from_env({})))
