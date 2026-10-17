"""C1 contract tests for every route the app mounts (PLAN M1 "Tests: Contracts").

- contracts/openapi.json equals the OpenAPI the real app generates (snapshot);
- every mounted route has at least one request below, and every request hits a mounted route;
- "fixture" routes return their golden payload exactly, and a 404 for an unknown id;
- "501" routes return {"detail", "available_in"} with the milestone from app.schemas.ROUTES;
- health answers 200 with the database up and 503, same body shape, with it down.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import pytest

from app.config import Settings
from app.contract_fixtures import contracts_dir
from app.main import create_app
from app.schemas import (API_PREFIX, CONTRACT_VERSION, ROUTES, ROUTES_BY_ID, ErrorDetail, Health, NotAvailable)
from tests.contract_routes import mounted_routes

FASTAPI_DOC_ROUTES = {("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/docs/oauth2-redirect"),
                      ("GET", "/redoc")}


def _fixtures() -> list[dict]:
    return json.loads((contracts_dir() / "fixtures" / "index.json").read_text(encoding="utf-8"))["fixtures"]


def _fixture(rel: str) -> Any:
    return json.loads((contracts_dir() / "fixtures" / rel).read_text(encoding="utf-8"))


def _template(method: str, url: str) -> Optional[str]:
    """The mounted path template ("/api/v1/scans/{scan_id}") a concrete request URL matches."""
    path = url.split("?", 1)[0]
    for r in ROUTES:
        if r.method != method:
            continue
        rx = "^" + re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(API_PREFIX + r.path)) + "$"
        if re.match(rx, path):
            return API_PREFIX + r.path
    return None


@dataclass
class Case:
    id: str
    operation_id: str
    method: str
    url: str
    status: int
    expect: Any = None                  # exact JSON body, when known
    json_body: Any = None
    content: Optional[bytes] = None
    headers: Optional[dict] = None


def _cases() -> list[Case]:
    idx = _fixtures()
    requests = {(e["operation_id"], e["path"]): e["file"] for e in idx if e["kind"] == "request"}
    cases: list[Case] = []
    for e in idx:
        if e["kind"] != "response" or not e["path"] or e["operation_id"] == "get_health":
            continue
        spec = ROUTES_BY_ID[e["operation_id"]]
        body = requests.get((e["operation_id"], e["path"]))
        kw: dict[str, Any] = {}
        if body:
            kw["json_body"] = _fixture(body)
        if spec.raw_body_media_type:
            kw["content"] = b"\x1f\x8b not a real scan"
            kw["headers"] = {"content-type": spec.raw_body_media_type}
        if spec.m1_behaviour == "fixture":
            cases.append(Case(e["file"], spec.operation_id, spec.method, e["path"], 200, _fixture(e["file"]), **kw))
        else:
            na = {"detail": f"lands in {spec.available_in}", "available_in": spec.available_in}
            cases.append(Case(e["file"], spec.operation_id, spec.method, e["path"], 501, na, **kw))
    # get_scan_asset serves bytes, so it has no JSON fixture: one request by hand.
    spec = ROUTES_BY_ID["get_scan_asset"]
    cases.append(Case("get_scan_asset", "get_scan_asset", "GET", f"{API_PREFIX}/scans/myel018-L1-d273/assets/ct.nii.gz",
                      501, {"detail": f"lands in {spec.available_in}", "available_in": spec.available_in}))
    # Unknown ids on the fixture routes: 404 with ErrorDetail.
    unknown = [
        ("get_patient", f"{API_PREFIX}/patients/nobody", "unknown patient_id"),
        ("list_patient_scans", f"{API_PREFIX}/patients/nobody/scans", "unknown patient_id"),
        ("get_patient_levels", f"{API_PREFIX}/patients/nobody/levels", "unknown patient_id"),
        ("get_patient_series", f"{API_PREFIX}/patients/demo-a/series?site=L2&metric=strain_ratio",
         "unknown patient_id, site or metric"),
        ("get_scan", f"{API_PREFIX}/scans/nope", "unknown scan_id"),
        ("get_scan_summary", f"{API_PREFIX}/scans/phantom/summary", "unknown scan_id"),
        ("get_audit_trail", f"{API_PREFIX}/audit/nope", "unknown scan_id"),
    ]
    for op, url, detail in unknown:
        cases.append(Case(f"404-{op}", op, "GET", url, 404, {"detail": detail}))
    return cases


CASES = _cases()


def test_every_mounted_route_has_a_contract_test():
    mounted = set(mounted_routes())
    covered = {(c.method, _template(c.method, c.url)) for c in CASES}
    covered.add(("GET", API_PREFIX + "/health"))   # test_health_* below
    covered |= FASTAPI_DOC_ROUTES                   # test_openapi_* and test_docs_pages below
    assert None not in {t for _, t in covered}, "a case URL matches no route in app.schemas.ROUTES"
    assert mounted - covered == set(), "mounted routes without a contract test"
    assert covered - mounted == set(), "contract tests for routes the app does not mount"


def test_routes_mount_exactly_the_contract_table():
    api = {(m, p) for (m, p) in mounted_routes() if p.startswith(API_PREFIX + "/")}
    assert api == {(r.method, API_PREFIX + r.path) for r in ROUTES}


def test_every_route_in_the_table_has_a_case():
    ops = {c.operation_id for c in CASES} | {"get_health"}
    assert ops == {r.operation_id for r in ROUTES}


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
async def test_route(client, case: Case):
    r = await client.request(case.method, case.url, json=case.json_body, content=case.content,
                             headers=case.headers)
    assert r.status_code == case.status, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    if case.status == 501:
        NotAvailable.model_validate(body)
    elif case.status == 404:
        ErrorDetail.model_validate(body)
    assert body == case.expect


async def test_fixture_routes_reject_unknown_enum_values(client):
    r = await client.get(f"{API_PREFIX}/patients/demo-a/series", params={"site": "C7", "metric": "lesion_volume_cm3"})
    assert r.status_code == 422


async def test_501_routes_still_validate_their_body(client):
    r = await client.post(f"{API_PREFIX}/copilot", json={"question": "", "patient_id": "demo-a"})
    assert r.status_code == 422
    r = await client.post(f"{API_PREFIX}/scans", params={"patient_id": "Bad Id", "day": 0}, content=b"x",
                          headers={"content-type": "application/gzip"})
    assert r.status_code == 422


async def test_health_ok_with_the_database_up(client):
    r = await client.get(f"{API_PREFIX}/health")
    assert r.status_code == 200, r.text
    h = Health.model_validate(r.json())
    assert (h.status, h.db, h.version) == ("ok", "ok", CONTRACT_VERSION)
    assert h.labels_version == json.loads((contracts_dir() / "labels.json").read_text())["version"]
    assert h.ai_chain == ["template"]
    assert h.server_time is not None and h.timescaledb is not None
    assert h.queue is None


async def test_health_503_with_the_same_shape_when_the_database_is_down():
    settings = Settings(database_url="postgresql://rb@127.0.0.1:1/rb_down?connect_timeout=1", auto_migrate=False,
                        run_workers=False, demo_seed=False)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"{API_PREFIX}/health")
    assert r.status_code == 503, r.text
    h = Health.model_validate(r.json())
    assert (h.status, h.db, h.server_time, h.timescaledb) == ("down", "down", None, None)
    assert r.json() == _fixture("health.down.json")


async def test_openapi_json_equals_the_contract_snapshot(client):
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    snapshot = json.loads((contracts_dir() / "openapi.json").read_text(encoding="utf-8"))
    assert r.json() == snapshot


def test_openapi_snapshot_without_a_server():
    app = create_app(Settings(database_url="postgresql:///unused"))
    snapshot = json.loads((contracts_dir() / "openapi.json").read_text(encoding="utf-8"))
    assert json.loads(json.dumps(app.openapi())) == snapshot


@pytest.mark.parametrize("path", ["/docs", "/docs/oauth2-redirect", "/redoc"])
async def test_docs_pages(client, path):
    r = await client.get(path)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
