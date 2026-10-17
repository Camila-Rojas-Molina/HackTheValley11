"""C1: contracts/openapi.json is a snapshot of the API generated from api/app/schemas.py (ROUTES + route_kwargs).
S4's api/tests check that the real app reproduces the same snapshot."""
from __future__ import annotations

import json
from pathlib import Path

import build_contracts as bc
from app import schemas as S

REPO = Path(__file__).resolve().parents[3]
SNAPSHOT = json.loads((REPO / "contracts" / "openapi.json").read_text(encoding="utf-8"))


def test_snapshot_equals_the_generated_openapi():
    assert json.loads(json.dumps(bc.contract_app().openapi())) == SNAPSHOT


def test_every_route_is_in_the_snapshot_and_nothing_else():
    ops = {(m.upper(), p): d for p, item in SNAPSHOT["paths"].items() for m, d in item.items()}
    want = {(r.method, S.API_PREFIX + r.path) for r in S.ROUTES}
    assert set(ops) == want
    assert len(S.ROUTES) == 18
    ids = [d["operationId"] for d in ops.values()]
    assert len(ids) == len(set(ids)) and set(ids) == set(S.ROUTES_BY_ID)


def test_admin_routes_are_marked_and_document_401():
    for r in S.ROUTES:
        op = SNAPSHOT["paths"][S.API_PREFIX + r.path][r.method.lower()]
        assert op.get("x-admin", False) is r.admin, r.operation_id
        if r.admin:
            assert "401" in op["responses"], r.operation_id


def test_later_milestone_routes_document_501():
    for r in S.ROUTES:
        op = SNAPSHOT["paths"][S.API_PREFIX + r.path][r.method.lower()]
        assert ("501" in op["responses"]) is (r.m1_behaviour == "501"), r.operation_id


def test_snapshot_identity():
    assert SNAPSHOT["info"] == {"title": S.API_TITLE, "version": S.CONTRACT_VERSION}
