"""Shared pytest set-up for pipeline/tests (S0). Stage tests live in pipeline/tests/<stage>/ (S2, S3).

- rb_pipeline is imported from this checkout (pipeline/ on sys.path), never from an installed copy.
- `rb_data_dir` is RB_DATA_DIR (large data outside the repo); a test that needs it skips, with the reason,
  when it is unset or missing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PIPELINE = Path(__file__).resolve().parents[1]
REPO = PIPELINE.parent
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO


@pytest.fixture(scope="session")
def demo_dir() -> Path:
    return REPO / "demo"


@pytest.fixture(scope="session")
def rb_data_dir() -> Path:
    value = os.environ.get("RB_DATA_DIR")
    if not value:
        pytest.skip("RB_DATA_DIR is not set (large data lives outside the repo)")
    path = Path(value)
    if not path.is_dir():
        pytest.skip(f"RB_DATA_DIR does not exist: {path}")
    return path
