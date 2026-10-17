"""C1: the golden payloads. Every fixture parses with its pydantic model, regenerating them from
api/app/schemas.py gives the same bytes, and every route has a response fixture."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import build_contracts as bc
from app import schemas as S

REPO = Path(__file__).resolve().parents[3]
FIX = REPO / "contracts" / "fixtures"
INDEX = json.loads((FIX / "index.json").read_text(encoding="utf-8"))
DAY0 = datetime(2026, 1, 17, tzinfo=timezone.utc)


def test_generated_contract_files_are_up_to_date():
    stale = bc.stale_files()
    assert not stale, f"run .venv/bin/python scripts/build_contracts.py and commit: {stale}"


def test_builder_rules_pass():
    assert bc.check(INDEX["fixtures"]) == []


def test_index_lists_every_fixture_file_once():
    listed = [e["file"] for e in INDEX["fixtures"]]
    on_disk = sorted(str(p.relative_to(FIX)) for p in FIX.rglob("*.json") if p.name != "index.json")
    assert sorted(listed) == on_disk
    assert len(listed) == len(set(listed))
    assert INDEX["contract_version"] == S.CONTRACT_VERSION


@pytest.mark.parametrize("entry", INDEX["fixtures"], ids=lambda e: e["file"])
def test_fixture_parses_with_its_model(entry):
    model = getattr(S, entry["model"])
    data = json.loads((FIX / entry["file"]).read_text(encoding="utf-8"))
    obj = model.model_validate(data)
    assert obj.model_dump(mode="json", by_alias=True) == data


def test_every_route_has_a_response_fixture():
    ops = {e["operation_id"] for e in INDEX["fixtures"] if e["kind"] == "response"}
    missing = [r.operation_id for r in S.ROUTES if r.response_model != "bytes" and r.operation_id not in ops]
    assert not missing, missing


def test_fixture_paths_are_under_the_api_prefix():
    for e in INDEX["fixtures"]:
        if e["path"]:
            assert e["path"].startswith(S.API_PREFIX + "/"), e


@pytest.mark.parametrize("rel", sorted(str(p.relative_to(FIX)) for p in (FIX / "scans").glob("*.json")))
def test_scan_time_is_the_demo_calendar(rel):
    m = S.ScanManifest.model_validate(json.loads((FIX / rel).read_text(encoding="utf-8")))
    if m.day is not None:
        assert m.time == DAY0 + timedelta(days=m.day)
    if m.provenance == "real_scan":
        assert not any(a.basis == "mock" for a in m.assets)


def test_pending_never_carries_a_number():
    def walk(v):
        if isinstance(v, dict):
            if v.get("basis") == "pending":
                assert v.get("value") is None and v.get("available_in"), v
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    for p in FIX.rglob("*.json"):
        walk(json.loads(p.read_text(encoding="utf-8")))


def test_no_field_or_value_is_called_risk():
    for p in FIX.rglob("*.json"):
        assert "risk" not in p.read_text(encoding="utf-8").lower(), p


def test_manifest_refuses_a_mock_strain_field_on_real_anatomy():
    data = json.loads((FIX / "scans" / "myel018-L1-d273.json").read_text(encoding="utf-8"))
    data["assets"][-1] = {**data["assets"][-1], "kind": "strain_volume", "basis": "mock"}
    with pytest.raises(ValueError):
        S.ScanManifest.model_validate(data)
