"""Admin auth, asset path traversal and the upload size cap (PLAN M2 "Security")."""
from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.schemas import API_PREFIX
from tests.conftest import ADMIN_KEY

ADMIN_WRITES = [
    ("POST", f"{API_PREFIX}/scans?patient_id=demo-b&day=0", None, b"\x1f\x8b"),
    ("POST", f"{API_PREFIX}/scans/myel018-L1-d273/runs", {"steps": ["physics"]}, None),
    ("POST", f"{API_PREFIX}/scans/myel018-L1-d273/summary/regenerate", {"chain": ["groq"]}, None),
]


@pytest.mark.parametrize("method,url,body,content", ADMIN_WRITES)
@pytest.mark.parametrize("key", [None, "wrong", ""])
async def test_an_admin_write_without_the_right_key_is_401(client, method, url, body, content, key):
    headers = {"content-type": "application/gzip"} if content else {}
    if key is not None:
        headers["x-admin-key"] = key
    r = await client.request(method, url, json=body, content=content, headers=headers)
    assert (r.status_code, r.json()) == (401, {"detail": "X-Admin-Key missing or wrong"})


@pytest.mark.parametrize("method,url,body,content", ADMIN_WRITES)
async def test_an_admin_write_is_503_when_the_key_is_unset(settings, method, url, body, content):
    app = create_app(Settings(**{**settings.__dict__, "admin_key": None, "auto_migrate": False,
                                 "demo_seed": False}))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.request(method, url, json=body, content=content,
                                headers={"x-admin-key": ADMIN_KEY, "content-type": "application/gzip"} if content
                                else {"x-admin-key": ADMIN_KEY})
    assert r.status_code == 503 and r.json()["ok"] is False


@pytest.mark.parametrize("name", ["..%2F..%2Fapi%2F.env", "%2E%2E", "..%2Fct.nii.gz", "%2Fetc%2Fpasswd",
                                  "ct.nii.gz%00", ".hidden.nii.gz", "x.vtu"])
async def test_traversal_in_an_asset_name_is_404(client, storage_dirs, name):
    # Encoded on purpose: httpx would normalise a literal "/assets/.." to the parent path before sending.
    (storage_dirs[0] / "secret.nii.gz").write_bytes(b"outside the scan folder")
    r = await client.get(f"{API_PREFIX}/scans/myel018-L1-d273/assets/{name}")
    assert r.status_code == 404, r.text


async def test_a_ready_asset_is_served_from_storage_with_its_media_type(client, storage_dirs):
    folder = storage_dirs[0] / "myel018-L1-d273"
    folder.mkdir(exist_ok=True)
    (folder / "mask_lesion.expert.nii.gz").write_bytes(b"\x1f\x8bfake")
    r = await client.get(f"{API_PREFIX}/scans/myel018-L1-d273/assets/mask_lesion.expert.nii.gz")
    assert r.status_code == 200 and r.content == b"\x1f\x8bfake"
    assert r.headers["content-type"] == "application/gzip"


async def test_a_symlink_out_of_storage_is_404(client, storage_dirs, tmp_path):
    outside = tmp_path / "outside.nii.gz"
    outside.write_bytes(b"x")
    folder = storage_dirs[0] / "myel018-L1-d273"
    folder.mkdir(exist_ok=True)
    link = folder / "ct.nii.gz"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(outside)
    try:
        r = await client.get(f"{API_PREFIX}/scans/myel018-L1-d273/assets/ct.nii.gz")
        assert r.status_code == 404
    finally:
        link.unlink()


async def test_an_over_size_upload_is_413(client):
    """The session app caps uploads at 1 MB (conftest settings: max_upload_mb=1)."""
    big = b"\x1f\x8b" + b"\0" * (1024 * 1024)
    r = await client.post(f"{API_PREFIX}/scans", params={"patient_id": "demo-b", "day": 0}, content=big,
                          headers={"content-type": "application/gzip", "x-admin-key": ADMIN_KEY})
    assert (r.status_code, r.json()) == (413, {"detail": "upload over RB_MAX_UPLOAD_MB"})


async def test_an_over_size_chunked_upload_is_413(client):
    async def chunks():
        for _ in range(3):
            yield b"\0" * (512 * 1024)
    r = await client.post(f"{API_PREFIX}/scans", params={"patient_id": "demo-b", "day": 0}, content=chunks(),
                          headers={"content-type": "application/gzip", "x-admin-key": ADMIN_KEY})
    assert r.status_code == 413


async def test_an_upload_under_the_cap_reaches_the_milestone_answer(client):
    r = await client.post(f"{API_PREFIX}/scans", params={"patient_id": "demo-b", "day": 0}, content=b"\x1f\x8bx",
                          headers={"content-type": "application/gzip", "x-admin-key": ADMIN_KEY})
    assert (r.status_code, r.json()["available_in"]) == (501, "M5")
