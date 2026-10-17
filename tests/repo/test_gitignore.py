"""The .gitignore traps found while planning (PLAN M1): shadcn's web/lib must be tracked, the demo bundle must be
tracked, and scans, meshes, build output, local databases, status notes and every .env file must not be."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def ignored(path: str) -> bool:
    r = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", path])
    assert r.returncode in (0, 1), r
    return r.returncode == 0


@pytest.mark.parametrize("path", [
    "web/lib/utils.ts", "web/lib/api.ts", "web/app/page.tsx", "web/components/ui/button.tsx",
    "demo/ATTRIBUTION.md", "demo/PROVENANCE.json", "demo/myel018-L1-d273/ct.nii.gz",
    "demo/myel018-L1-d273/mask_lesion.expert.nii.gz", "demo/phantom/strain_min_principal_ue.mock.mz3",
    "demo/myel018-L1-d273/vertebra.expert.stl", "api/.env.example", "contracts/fixtures/index.json",
    "pipeline/rb_pipeline/io/__init__.py", "worker/rb_worker/__init__.py",
])
def test_tracked(path):
    assert not ignored(path), path


@pytest.mark.parametrize("path", [
    "web/node_modules/next/package.json", "node_modules/x/index.js", "web/.next/cache/x", "scan.nii",
    "rb-data/ct.nii.gz", "pipeline/out/strain.nii.gz", "x/slice.dcm", "out/mesh.vtu", "out/surface.vtp",
    "out/fields.vti", "out/mesh.mz3", "out/vertebra.stl", "data/ct.nii.gz", "pipeline/data/big.bin",
    ".pgdata/PG_VERSION", "api/.pgdata/x", ".rb-status.md", ".env", "api/.env", "web/.env.local", ".env.bak",
    ".env.bak2", "demo/.env", "pipeline/.venv/bin/python", "lib/x.py",
])
def test_ignored(path):
    assert ignored(path), path


def test_no_tracked_file_is_ignored():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-ci", "--exclude-standard"], capture_output=True,
                         text=True, check=True).stdout.split()
    assert not out, out
