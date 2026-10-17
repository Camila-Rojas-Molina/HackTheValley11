"""Shared set-up for tests/repo (S0): repo-level tests that need no database and no server."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
for p in (REPO / "scripts", REPO / "pipeline", REPO / "api"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def labels() -> dict:
    return json.loads((REPO / "contracts" / "labels.json").read_text(encoding="utf-8"))
