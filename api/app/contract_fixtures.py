"""Contract helpers shared by the routes: the contracts folder, the labels version and the 501 body.

A route that lands in a later milestone answers `{"detail": "lands in <M>", "available_in": "<M>"}` with the
milestone from app.schemas.ROUTES (`available_in`). The golden payloads in contracts/fixtures (C1) are what
the database-backed routes are tested against (api/tests/test_contract.py).

The contracts folder is <repo>/contracts, or RB_CONTRACTS_DIR when that is set.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from fastapi.responses import JSONResponse

from .schemas import ROUTES_BY_ID

REPO_ROOT = Path(__file__).resolve().parents[2]


def contracts_dir() -> Path:
    return Path(os.environ.get("RB_CONTRACTS_DIR") or REPO_ROOT / "contracts")


@lru_cache(maxsize=4)
def _labels_version(root: str) -> str:
    return json.loads((Path(root) / "labels.json").read_text(encoding="utf-8"))["version"]


def labels_version() -> str:
    return _labels_version(str(contracts_dir()))


def not_available(operation_id: str) -> JSONResponse:
    milestone = ROUTES_BY_ID[operation_id].available_in
    return JSONResponse({"detail": f"lands in {milestone}", "available_in": milestone}, status_code=501)
