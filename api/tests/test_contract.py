"""C1 contract tests for every route the app mounts, on the seeded database (PLAN M2 "Routes").

- contracts/openapi.json equals the OpenAPI the real app generates (snapshot);
- every mounted route has at least one request below, and every request hits a mounted route;
- the database-backed routes return their golden payload: the scan manifests, the patient list (with the
  deterministic flag), the patient and the scan list exactly; the ladder and series except `source` and
  `query_ms`; the summary except `generated_at`;
- routes that land later return {"detail", "available_in"} (501) with the milestone from app.schemas.ROUTES,
  admin routes only with the right X-Admin-Key;
- health answers 200 with the database up and 503, same body shape, with it down.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import httpx
import pytest

from app.config import Settings
from app.contract_fixtures import contracts_dir
from app.main import create_app
from app.schemas import (API_PREFIX, CONTRACT_VERSION, ROUTES, ROUTES_BY_ID, AlertFeedbackAck, AuditTrail,
                         ErrorDetail, Health, NotAvailable, TigerInfo)
from tests.conftest import ADMIN_KEY
from tests.contract_routes import mounted_routes

FASTAPI_DOC_ROUTES = {("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/docs/oauth2-redirect"),
                      ("GET", "/redoc")}
ADMIN = {"x-admin-key": ADMIN_KEY}
# Database-backed routes and the fields that are live measurements, not contract values.
LIVE_FIELDS = {"get_patient_levels": ("source", "query_ms"), "get_patient_series": ("source", "query_ms"),
               "get_scan_summary": ("generated_at",)}
CHECKED_BY_HAND = {"get_tiger_info", "get_audit_trail", "post_alert_feedback"}


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
    expect: Any = None                  # exact JSON body (after dropping `drop` fields), when known
    json_body: Any = None
    content: Optional[bytes] = None
    headers: Optional[dict] = None
    drop: tuple[str, ...] = ()
    check: Optional[Callable[[Any], None]] = None


def _na(op: str) -> dict:
    m = ROUTES_BY_ID[op].available_in
    return {"detail": f"lands in {m}", "available_in": m}


def _cases() -> list[Case]:
    idx = _fixtures()
    requests = {(e["operation_id"], e["path"]): e["file"] for e in idx if e["kind"] == "request"}
    cases: list[Case] = []
    for e in idx:
        op = e["operation_id"]
        if e["kind"] != "response" or not e["path"] or op == "get_health" or op in CHECKED_BY_HAND:
            continue
        spec = ROUTES_BY_ID[op]
        body = requests.get((op, e["path"]))
        kw: dict[str, Any] = {}
        if body:
            kw["json_body"] = _fixture(body)
        headers = dict(ADMIN) if spec.admin else {}
        if spec.raw_body_media_type:
            kw["content"] = b"\x1f\x8b not a real scan"
            headers["content-type"] = spec.raw_body_media_type
        if headers:
            kw["headers"] = headers
        if spec.m1_behaviour == "fixture":
            cases.append(Case(e["file"], op, spec.method, e["path"], 200, _fixture(e["file"]),
                              drop=LIVE_FIELDS.get(op, ()), **kw))
        else:
            cases.append(Case(e["file"], op, spec.method, e["path"], 501, _na(op), **kw))
    cases += _hand_cases()
    unknown = [
        ("get_patient", f"{API_PREFIX}/patients/nobody", "unknown patient_id"),
        ("list_patient_scans", f"{API_PREFIX}/patients/nobody/scans", "unknown patient_id"),
        ("get_patient_levels", f"{API_PREFIX}/patients/nobody/levels", "unknown patient_id"),
        ("get_patient_series", f"{API_PREFIX}/patients/demo-a/series?site=L2&metric=strain_ratio",
         "unknown patient_id, site or metric"),
        ("get_patient_series", f"{API_PREFIX}/patients/nobody/series?site=L1&metric=lesion_volume_cm3",
         "unknown patient_id, site or metric"),
        ("get_scan", f"{API_PREFIX}/scans/nope", "unknown scan_id"),
        ("get_scan_summary", f"{API_PREFIX}/scans/phantom/summary", "unknown scan_id"),
        ("get_scan_summary", f"{API_PREFIX}/scans/myel018-L1-d0/summary", "unknown scan_id"),
        ("get_audit_trail", f"{API_PREFIX}/audit/nope", "unknown scan_id"),
        ("get_scan_asset", f"{API_PREFIX}/scans/nope/assets/ct.nii.gz", "unknown asset"),
        ("get_scan_asset", f"{API_PREFIX}/scans/myel018-L1-d273/assets/nothing.nii.gz", "unknown asset"),
        ("get_scan_asset", f"{API_PREFIX}/scans/myel018-L1-d273/assets/mask_vertebra.expert.nii.gz",
         "asset file not found"),
    ]
    for op, url, detail in unknown:
        cases.append(Case(f"404-{op}-{url.rsplit('/', 1)[-1]}", op, "GET", url, 404, {"detail": detail}))
    return cases


def _hand_cases() -> list[Case]:
    tiger_fx = _fixture("tiger/info.json")
    audit_fx = _fixture("audit/myel018-L1-d273.json")
    fb_req = _fixture("_post/alerts/al-myel018-L1-d273/feedback.request.json")

    def tiger_check(body):
        info = TigerInfo.model_validate(body)
        assert info.source == "live" and info.budget_bytes == tiger_fx["budget_bytes"]
        assert [h.name for h in info.hypertables] == [h["name"] for h in tiger_fx["hypertables"]]
        assert [c.model_dump() | {"materialized_only": None} for c in info.continuous_aggregates] == \
            tiger_fx["continuous_aggregates"]
        assert info.db_size_bytes and info.db_size_bytes > 0
        assert info.query_ms.raw_ms is not None and info.query_ms.aggregate_ms is not None

    def audit_check(body):
        trail = AuditTrail.model_validate(body)
        assert trail.chain_ok and trail.chain_key == audit_fx["chain_key"] and trail.head == trail.entries[-1].hash
        first, fx = trail.entries[0], audit_fx["entries"][0]
        assert (first.seq, first.actor, first.action, first.patient_id, first.prev_hash) == \
            (fx["seq"], fx["actor"], fx["action"], fx["patient_id"], fx["prev_hash"])
        assert {k: first.detail[k] for k in fx["detail"]} == fx["detail"]

    def feedback_check(body):
        ack = AlertFeedbackAck.model_validate(body)
        assert (ack.alert_id, ack.feedback) == ("al-myel018-L1-d273", "snooze")
        assert ack.snooze_until - ack.recorded_at == timedelta(hours=24)

    return [
        Case("tiger/info", "get_tiger_info", "GET", f"{API_PREFIX}/tiger/info", 200, check=tiger_check),
        Case("audit/myel018-L1-d273", "get_audit_trail", "GET", f"{API_PREFIX}/audit/myel018-L1-d273", 200,
             check=audit_check),
        Case("feedback", "post_alert_feedback", "POST", f"{API_PREFIX}/alerts/al-myel018-L1-d273/feedback", 201,
             json_body=fb_req, check=feedback_check),
        Case("asset-pending", "get_scan_asset", "GET",
             f"{API_PREFIX}/scans/myel018-L1-d273/assets/strain_min_principal_ue.expert.nii.gz", 501,
             {"detail": "lands in M5", "available_in": "M5"}),
        Case("asset-pending-d0", "get_scan_asset", "GET", f"{API_PREFIX}/scans/myel018-L1-d0/assets/ct.nii.gz",
             501, {"detail": "lands in M3", "available_in": "M3"}),
    ]


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
    if case.check is not None:
        case.check(body)
    if case.expect is not None:
        expect = dict(case.expect) if isinstance(case.expect, dict) else case.expect
        for k in case.drop:
            body.pop(k)
            expect.pop(k)
        assert body == expect


async def test_live_fields_are_measured(client):
    r = await client.get(f"{API_PREFIX}/patients/demo-a/levels")
    body = r.json()
    assert body["source"] == "raw" and body["query_ms"]["raw_ms"] >= 0 and body["query_ms"]["aggregate_ms"] >= 0
    r = await client.get(f"{API_PREFIX}/patients/demo-a/series", params={"site": "L1", "metric": "lesion_volume_cm3"})
    assert r.json()["source"] == "raw" and r.json()["query_ms"] >= 0
    r = await client.get(f"{API_PREFIX}/scans/myel018-L1-d273/summary")
    assert abs(datetime.fromisoformat(r.json()["generated_at"].replace("Z", "+00:00")).timestamp()
               - datetime.now().timestamp()) < 60


async def test_series_of_a_level_without_a_viewable_scan_comes_from_the_ladder_rows(client):
    r = await client.get(f"{API_PREFIX}/patients/demo-a/series", params={"site": "L4", "metric": "lesion_volume_cm3"})
    assert r.status_code == 200, r.text
    pts = r.json()["points"]
    assert [(p["day"], p["value"], p["scan_id"], p["provenance"]) for p in pts] == \
        [(0, 0.1, None, "real_scan"), (273, 3.32, None, "real_scan")]


async def test_routes_reject_unknown_enum_values(client):
    r = await client.get(f"{API_PREFIX}/patients/demo-a/series", params={"site": "C7", "metric": "lesion_volume_cm3"})
    assert r.status_code == 422


async def test_501_routes_still_validate_their_body(client):
    r = await client.post(f"{API_PREFIX}/copilot", json={"question": "", "patient_id": "demo-a"})
    assert r.status_code == 422
    r = await client.post(f"{API_PREFIX}/scans", params={"patient_id": "Bad Id", "day": 0}, content=b"x",
                          headers={"content-type": "application/gzip", **ADMIN})
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


async def test_health_reports_the_queue_to_an_admin_only(client):
    r = await client.get(f"{API_PREFIX}/health", headers=ADMIN)
    assert set(Health.model_validate(r.json()).queue) == {"queued", "running", "succeeded", "failed", "cancelled"}
    r = await client.get(f"{API_PREFIX}/health", headers={"x-admin-key": "wrong"})
    assert r.json()["queue"] is None


async def test_health_503_with_the_same_shape_when_the_database_is_down():
    settings = Settings(database_url="postgresql://rb@127.0.0.1:1/rb_down?connect_timeout=1", auto_migrate=False,
                        demo_seed=False)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get(f"{API_PREFIX}/health")
            db_route = await c.get(f"{API_PREFIX}/patients")
    assert r.status_code == 503, r.text
    h = Health.model_validate(r.json())
    assert (h.status, h.db, h.server_time, h.timescaledb) == ("down", "down", None, None)
    assert r.json() == _fixture("health.down.json")
    assert (db_route.status_code, db_route.json()) == (503, _fixture("_errors/db_unavailable.json"))


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
