"""What the M1 skeleton routes serve: the golden payloads in contracts/fixtures (C1), or a 501.

Each route's M1 behaviour comes from app.schemas.ROUTES (`m1_behaviour`):
- "fixture": the payload that contracts/fixtures/index.json lists for (operation_id, request path), returned
  through the route's response model; any other id is a 404 `{"detail": "unknown <param>"}`;
- "501": `{"detail": "lands in <M>", "available_in": "<M>"}` until the milestone that makes the route live.
Push 2 replaces the fixture reads with database queries, route by route.

The contracts folder is <repo>/contracts, or RB_CONTRACTS_DIR when that is set.
"""
from __future__ import annotations

import copy
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .schemas import API_PREFIX, ROUTES_BY_ID

REPO_ROOT = Path(__file__).resolve().parents[2]


def contracts_dir() -> Path:
    return Path(os.environ.get("RB_CONTRACTS_DIR") or REPO_ROOT / "contracts")


@lru_cache(maxsize=4)
def _index(root: str) -> dict[tuple[str, str], str]:
    """{(operation_id, full request path): fixture file} for every 2xx response fixture."""
    data = json.loads((Path(root) / "fixtures" / "index.json").read_text(encoding="utf-8"))
    return {(e["operation_id"], e["path"]): e["file"] for e in data["fixtures"]
            if e["kind"] == "response" and e["path"] and 200 <= e["status"] < 300}


@lru_cache(maxsize=64)
def _load(root: str, rel: str) -> Any:
    return json.loads((Path(root) / "fixtures" / rel).read_text(encoding="utf-8"))


@lru_cache(maxsize=4)
def _labels_version(root: str) -> str:
    return json.loads((Path(root) / "labels.json").read_text(encoding="utf-8"))["version"]


def labels_version() -> str:
    return _labels_version(str(contracts_dir()))


def fixture(operation_id: str, path: str) -> Optional[Any]:
    """The golden response for `operation_id` at `path` (relative to /api/v1, query included), or None."""
    root = str(contracts_dir())
    rel = _index(root).get((operation_id, API_PREFIX + path))
    return None if rel is None else copy.deepcopy(_load(root, rel))


def not_available(operation_id: str) -> JSONResponse:
    milestone = ROUTES_BY_ID[operation_id].available_in
    return JSONResponse({"detail": f"lands in {milestone}", "available_in": milestone}, status_code=501)


def serve(operation_id: str, path: str, unknown: str) -> Any:
    """The M1 answer for a skeleton route; `unknown` names the id a 404 reports (e.g. "scan_id")."""
    behaviour = ROUTES_BY_ID[operation_id].m1_behaviour
    if behaviour == "501":
        return not_available(operation_id)
    if behaviour != "fixture":
        raise RuntimeError(f"{operation_id} is {behaviour!r} in M1; it needs its own handler")
    payload = fixture(operation_id, path)
    if payload is None:
        raise HTTPException(404, f"unknown {unknown}")
    return payload
