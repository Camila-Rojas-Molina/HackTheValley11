"""The committed demo bundle (demo/, S2, PLAN M3 item 2; contracts/formats.md "Demo bundle").

- Every file is listed once in demo/PROVENANCE.json, and its sha256 and size match.
- The manifest fixtures (contracts/fixtures/scans/*.json) and the files agree: names, kinds, hashes, grids.
- Every NIfTI is 1 mm; CTs are int16, masks uint8 {0,1}, strain volumes float32. All volumes of one scan
  share one grid.
- The Day-273 L1 lesion mask holds 3,217 voxels on the 1 mm crop, which is 3.22 cm³; Day 0 holds none.
- Mock data stays on the synthetic phantom: real anatomy never carries a mock strain field.
- The calibration block is empty (null) until M5, and the bundle stays small (it is committed).

Standard library only: the NIfTI-1 and .mz3 headers are read with struct, so the suite needs nothing but
pytest. Checks against the sources in RB_DATA_DIR skip, with the reason, when it is not set.
"""
from __future__ import annotations

import functools
import gzip
import hashlib
import json
import math
import re
import struct
from array import array
from pathlib import Path

import pytest

pytestmark = pytest.mark.pipeline

REPO = Path(__file__).resolve().parents[2]
DEMO = REPO / "demo"
FIXTURES = REPO / "contracts" / "fixtures" / "scans"
LABELS = json.loads((REPO / "contracts" / "labels.json").read_text(encoding="utf-8"))
PROV = json.loads((DEMO / "PROVENANCE.json").read_text(encoding="utf-8"))
SCANS = ("myel018-L1-d273", "myel018-L1-d0", "phantom")
REAL = ("myel018-L1-d273", "myel018-L1-d0")
DOCS = {"PROVENANCE.json", "ATTRIBUTION.md"}
PROVENANCE_ENUM = {"real_scan", "synthetic_insilico", "synthetic_metric_interp"}
NAME_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")   # formats.md: the asset route serves only these
MOCK_CAL_MAX_UE = 1500.0                                      # formats.md: the phantom's mock uses 0-1500 µε
BUNDLE_MAX_BYTES = 6 * 1024 * 1024
FILE_MAX_BYTES = 2 * 1024 * 1024

# NIfTI-1 datatype codes
DT_UINT8, DT_INT16, DT_FLOAT32 = 2, 4, 16
DTYPE_BY_KIND = {"ct": DT_INT16, "mask_vertebra": DT_UINT8, "mask_body": DT_UINT8, "mask_lesion": DT_UINT8,
                 "strain_volume": DT_FLOAT32}
ARRAY_CODE = {DT_UINT8: "B", DT_INT16: "h", DT_FLOAT32: "f"}


FILES = {f["path"]: f for f in PROV["files"]}
NIFTIS = sorted(p for p in FILES if p.endswith(".nii.gz"))


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fixture(scan_id: str) -> dict:
    return json.loads((FIXTURES / f"{scan_id}.json").read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=None)
def nifti(rel: str) -> dict:
    """NIfTI-1 single-file header fields and the voxel bytes (little-endian, as every file here is written)."""
    raw = gzip.decompress((DEMO / rel).read_bytes())
    assert struct.unpack_from("<i", raw, 0)[0] == 348, f"{rel}: not a little-endian NIfTI-1 header"
    assert raw[344:348] == b"n+1\x00", f"{rel}: magic {raw[344:348]!r}"
    dim = struct.unpack_from("<8h", raw, 40)
    datatype, bitpix = struct.unpack_from("<hh", raw, 70)
    pixdim = struct.unpack_from("<8f", raw, 76)
    vox_offset = int(struct.unpack_from("<f", raw, 108)[0])
    scl_slope, scl_inter = struct.unpack_from("<ff", raw, 112)
    qform_code, sform_code = struct.unpack_from("<hh", raw, 252)
    srow = [struct.unpack_from("<4f", raw, off) for off in (280, 296, 312)]
    shape = tuple(dim[1:1 + dim[0]])
    return {"dim": dim, "shape": shape, "datatype": datatype, "bitpix": bitpix, "pixdim": pixdim,
            "vox_offset": vox_offset, "scl_slope": scl_slope, "scl_inter": scl_inter,
            "xyz_unit": raw[123] & 0x07, "qform_code": qform_code, "sform_code": sform_code, "srow": srow,
            "descrip": raw[148:228].split(b"\x00")[0].decode("ascii", "replace"),
            "data": raw[vox_offset:]}


def voxels(rel: str) -> array:
    h = nifti(rel)
    a = array(ARRAY_CODE[h["datatype"]])
    a.frombytes(h["data"])
    return a


def nonzero(rel: str) -> int:
    data = nifti(rel)["data"]
    assert nifti(rel)["datatype"] == DT_UINT8
    return len(data) - data.count(0)


# ------------------------------------------------------------------ PROVENANCE.json

def test_provenance_lists_every_bundle_file_exactly_once():
    listed = [f["path"] for f in PROV["files"]]
    assert len(listed) == len(set(listed)), "a file is listed twice"
    on_disk = sorted(str(p.relative_to(DEMO)) for p in DEMO.rglob("*") if p.is_file()
                     and str(p.relative_to(DEMO)) not in DOCS)
    assert sorted(listed) == on_disk
    assert {p.split("/")[0] for p in on_disk} == set(SCANS)


@pytest.mark.parametrize("rel", sorted(FILES))
def test_sha256_and_size_match_provenance(rel):
    entry, p = FILES[rel], DEMO / rel
    assert p.stat().st_size == entry["bytes"], rel
    assert sha256(p) == entry["sha256"], rel


@pytest.mark.parametrize("rel", sorted(FILES))
def test_every_file_names_its_script_basis_and_provenance(rel):
    e = FILES[rel]
    assert set(e) >= {"path", "sha256", "bytes", "source_script", "basis", "provenance"}, rel
    assert e["source_script"] in PROV["scripts"], rel
    assert e["basis"] in {"measured", "mock"}, rel
    assert e["provenance"] in PROVENANCE_ENUM, rel
    assert e["scan_id"] == rel.split("/")[0]
    assert e["provenance"] == PROV["scans"][e["scan_id"]]["provenance"]
    assert NAME_RX.match(rel.split("/")[1]), rel


def test_every_source_script_carries_a_hash_and_a_command():
    assert PROV["scripts"]
    for path, s in PROV["scripts"].items():
        assert path.startswith("RB_DATA_DIR/"), path
        assert re.fullmatch(r"[0-9a-f]{64}", s["sha256"]) and s["bytes"] > 0, path
        assert s["command"] and s["what"], path


def test_calibration_block_is_empty_until_m5():
    assert set(PROV["scans"]) == set(SCANS)
    for scan_id, s in PROV["scans"].items():
        assert "calibration" in s and s["calibration"] is None, scan_id
        assert fixture(scan_id)["calibration"] is None, scan_id


# ------------------------------------------------------------------ the manifest fixtures (C1/C2)

@pytest.mark.parametrize("scan_id", SCANS)
def test_files_are_assets_the_manifest_names(scan_id):
    assets = {a["name"]: a for a in fixture(scan_id)["assets"]}
    for rel, e in FILES.items():
        if e["scan_id"] != scan_id:
            continue
        name = rel.split("/")[1]
        assert name in assets, f"{rel} is not an asset of contracts/fixtures/scans/{scan_id}.json"
        a = assets[name]
        assert a["kind"] == e["kind"], rel
        assert a["provenance"] == e["provenance"], rel
        # a shipped file is a ready asset of its scan: the contract cannot say it is still pending
        assert a["status"] == "ready" and a["available_in"] is None, (rel, a["status"], a["available_in"])
        assert a["basis"] == e["basis"], rel
        assert (a["sha256"], a["bytes"]) == (e["sha256"], e["bytes"]), rel


@pytest.mark.parametrize("scan_id", SCANS)
def test_nothing_in_the_demo_fixtures_waits_on_m3(scan_id):
    """M3 ships the bundle, so no asset or metric of a demo scan is still pending M3."""
    m = fixture(scan_id)
    assert [a["name"] for a in m["assets"] if a.get("available_in") == "M3"] == [], scan_id
    assert [x["key"] for x in m["metrics"] if x.get("available_in") == "M3"] == [], scan_id


@pytest.mark.parametrize("scan_id", SCANS)
def test_ready_fixture_assets_are_these_bytes(scan_id):
    """A fixture asset marked ready (or carrying a hash) is exactly the committed file."""
    for a in fixture(scan_id)["assets"]:
        rel = f"{scan_id}/{a['name']}"
        if a["status"] == "ready" or a["sha256"]:
            assert rel in FILES, f"{rel} is ready in the fixture but missing from demo/"
            assert a["sha256"] == FILES[rel]["sha256"], rel
            assert a["bytes"] == FILES[rel]["bytes"], rel


def test_day273_crops_are_the_spike_files_byte_for_byte():
    """formats.md: the Day-273 crops keep the fixture's hashes (4ca90d8b, b22e99cd, 163103d9)."""
    want = {"ct.nii.gz": "4ca90d8b", "mask_vertebra.expert.nii.gz": "b22e99cd",
            "mask_lesion.expert.nii.gz": "163103d9"}
    for name, prefix in want.items():
        assert FILES[f"myel018-L1-d273/{name}"]["sha256"].startswith(prefix), name


# ------------------------------------------------------------------ NIfTI headers and grids

@pytest.mark.parametrize("rel", NIFTIS)
def test_nifti_header_is_1mm_with_the_contract_dtype(rel):
    h, kind = nifti(rel), FILES[rel]["kind"]
    assert h["datatype"] == DTYPE_BY_KIND[kind], (rel, kind, h["datatype"])
    assert h["bitpix"] == {DT_UINT8: 8, DT_INT16: 16, DT_FLOAT32: 32}[h["datatype"]]
    assert len(h["shape"]) == 3, rel
    assert h["pixdim"][1:4] == (1.0, 1.0, 1.0), (rel, h["pixdim"][1:4])
    assert h["xyz_unit"] == 2, f"{rel}: spatial unit is not mm"
    assert h["sform_code"] > 0, f"{rel}: no sform"
    for c in range(3):   # the affine itself is 1 mm per voxel
        assert math.isclose(math.hypot(*(h["srow"][r][c] for r in range(3))), 1.0, abs_tol=1e-5), rel
    assert h["scl_slope"] in (0.0, 1.0) and h["scl_inter"] == 0.0, f"{rel}: values are rescaled"
    assert len(h["data"]) == math.prod(h["shape"]) * h["bitpix"] // 8, f"{rel}: voxel data size"


@pytest.mark.parametrize("scan_id", SCANS)
def test_volumes_of_one_scan_share_the_ct_grid(scan_id):
    ct = nifti(f"{scan_id}/ct.nii.gz")
    for rel in NIFTIS:
        if rel.startswith(scan_id + "/"):
            h = nifti(rel)
            assert h["shape"] == ct["shape"], rel
            assert h["srow"] == ct["srow"], rel
            assert (h["qform_code"], h["sform_code"]) == (ct["qform_code"], ct["sform_code"]), rel
    grid = fixture(scan_id)["grid"]
    assert grid is not None, f"{scan_id}: the fixture has no grid for a shipped CT"
    assert tuple(grid["shape"]) == ct["shape"]
    for r in range(3):
        for c in range(4):
            assert math.isclose(grid["affine"][r][c], ct["srow"][r][c], abs_tol=1e-3), (scan_id, r, c)


@pytest.mark.parametrize("rel", [r for r in NIFTIS if FILES[r]["kind"].startswith("mask_")])
def test_masks_are_binary(rel):
    data = nifti(rel)["data"]
    assert set(data) <= {0, 1}, rel


# ------------------------------------------------------------------ lesion and vertebra volumes

def crop_metric(scan_id: str, key: str, grid: str):
    vals = [m["value"] for m in fixture(scan_id)["metrics"]
            if m["key"] == key and m["grid"] == grid and m["mask_source"] == "expert"]
    assert len(vals) == 1, (scan_id, key, grid, vals)
    return vals[0]


def measured(scan_id: str, key: str, grid: str) -> float:
    vals = [v["value"] for v in PROV["scans"][scan_id]["measured"][key] if v["grid"] == grid]
    assert len(vals) == 1, (scan_id, key, grid)
    return vals[0]


def test_day273_lesion_is_3217_voxels_which_is_3_22_cm3():
    rel = "myel018-L1-d273/mask_lesion.expert.nii.gz"
    n = nonzero(rel)
    voxel_mm3 = math.prod(nifti(rel)["pixdim"][1:4])
    assert n == 3217
    assert n == PROV["scans"]["myel018-L1-d273"]["measured"]["lesion_voxels_crop_1mm"]
    cm3 = round(n * voxel_mm3 / 1000.0, 2)
    assert cm3 == 3.22
    assert cm3 == crop_metric("myel018-L1-d273", "lesion_volume_cm3", "crop_1mm")
    assert cm3 == measured("myel018-L1-d273", "lesion_volume_cm3", "crop_1mm")
    # the headline names its grid: 3.24 cm³ on the original scan grid
    assert crop_metric("myel018-L1-d273", "lesion_volume_cm3", "original") == 3.24
    assert measured("myel018-L1-d273", "lesion_volume_cm3", "original") == 3.24


@pytest.mark.parametrize("scan_id", REAL)
def test_lesion_lies_inside_the_vertebra_mask(scan_id):
    les = nifti(f"{scan_id}/mask_lesion.expert.nii.gz")["data"]
    vert = nifti(f"{scan_id}/mask_vertebra.expert.nii.gz")["data"]
    assert not any(l and not v for l, v in zip(les, vert)), scan_id


def test_day0_has_no_lesion():
    assert nonzero("myel018-L1-d0/mask_lesion.expert.nii.gz") == 0
    assert PROV["scans"]["myel018-L1-d0"]["measured"]["lesion_voxels_crop_1mm"] == 0
    assert crop_metric("myel018-L1-d0", "lesion_volume_cm3", "original") == 0.0


@pytest.mark.parametrize("scan_id,voxel_count", [("myel018-L1-d273", 67790), ("myel018-L1-d0", 69376)])
def test_vertebra_volume_incl_lesion_on_the_crop(scan_id, voxel_count):
    n = nonzero(f"{scan_id}/mask_vertebra.expert.nii.gz")
    assert n == voxel_count == PROV["scans"][scan_id]["measured"]["vertebra_voxels_crop_1mm"]
    cm3 = round(n / 1000.0, 2)
    assert cm3 == measured(scan_id, "vertebra_volume_incl_lesion_cm3", "crop_1mm")
    assert crop_metric(scan_id, "vertebra_volume_cm3", "crop_1mm") == cm3
    assert crop_metric(scan_id, "vertebra_volume_cm3", "original") == \
        measured(scan_id, "vertebra_volume_incl_lesion_cm3", "original")


# ------------------------------------------------------------------ mock data stays on the phantom

def test_real_anatomy_never_carries_mock_data():
    for rel, e in FILES.items():
        if e["scan_id"] in REAL:
            assert e["basis"] == "measured" and e["provenance"] == "real_scan", rel
            assert ".mock." not in rel and not e["kind"].startswith("strain_"), rel
        else:
            assert e["provenance"] == "synthetic_insilico", rel
        if e["kind"].startswith("strain_") or ".mock." in rel:
            assert e["scan_id"] == "phantom" and e["basis"] == "mock" and ".mock." in rel, rel
    assert PROV["scans"]["phantom"]["simulated"] is True
    assert PROV["scans"]["phantom"]["label"] == LABELS["viewer"]["phantom_label"]
    for scan_id in REAL:
        assert PROV["scans"][scan_id]["stiffness"] == "pending FE (M5)"


def test_mock_strain_volume_is_labelled_and_in_range():
    rel = "phantom/strain_min_principal_ue.mock.nii.gz"
    assert "MOCK" in nifti(rel)["descrip"]
    v = voxels(rel)
    assert all(math.isfinite(x) for x in v)
    assert min(v) >= 0.0 and 0.0 < max(v) <= MOCK_CAL_MAX_UE
    scale = [s for s in fixture("phantom")["color_scales"] if s["layer"] == "strain_min_principal_ue"]
    assert scale and scale[0]["cal_min"] == 0.0 and scale[0]["cal_max"] == MOCK_CAL_MAX_UE


def test_mock_strain_surface_is_a_valid_mz3_in_world_mm():
    raw = gzip.decompress((DEMO / "phantom" / "strain_min_principal_ue.mock.mz3").read_bytes())
    magic, attr, n_faces, n_verts, n_skip = struct.unpack_from("<HHIII", raw, 0)
    assert (magic, attr, n_skip) == (23117, 1 | 2 | 8, 0)   # faces, vertices, scalars (formats.md)
    assert len(raw) == 16 + n_faces * 12 + n_verts * 12 + n_verts * 4
    faces = array("i"); faces.frombytes(raw[16:16 + n_faces * 12])
    verts = array("f"); verts.frombytes(raw[16 + n_faces * 12:16 + n_faces * 12 + n_verts * 12])
    scal = array("f"); scal.frombytes(raw[16 + n_faces * 12 + n_verts * 12:])
    assert n_faces > 0 and n_verts > 0 and 0 <= min(faces) and max(faces) < n_verts
    assert min(scal) >= 0.0 and 0.0 < max(scal) <= MOCK_CAL_MAX_UE
    ct = nifti("phantom/ct.nii.gz")   # vertices are in NIfTI world mm, inside the phantom CT's box
    for axis in range(3):
        lo = ct["srow"][axis][3]
        hi = lo + ct["srow"][axis][axis] * (ct["shape"][axis] - 1)
        coords = verts[axis::3]
        assert min(lo, hi) - 1.0 <= min(coords) and max(coords) <= max(lo, hi) + 1.0, axis


# ------------------------------------------------------------------ size and wording

def test_bundle_is_small():
    sizes = {str(p.relative_to(DEMO)): p.stat().st_size for p in DEMO.rglob("*") if p.is_file()}
    assert sum(sizes.values()) <= BUNDLE_MAX_BYTES, sum(sizes.values())
    big = {k: v for k, v in sizes.items() if v > FILE_MAX_BYTES}
    assert not big, big


def doc_texts() -> dict[str, str]:
    strings: list[str] = []

    def walk(v):
        if isinstance(v, str):
            strings.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                strings.append(str(k))
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(PROV)
    return {"ATTRIBUTION.md": (DEMO / "ATTRIBUTION.md").read_text(encoding="utf-8"),
            "PROVENANCE.json": "\n".join(strings)}


def test_attribution_carries_the_dataset_citation_and_the_mock_label():
    text = (DEMO / "ATTRIBUTION.md").read_text(encoding="utf-8")
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)   # Markdown links -> their text
    assert LABELS["attribution"]["full"] in plain
    assert LABELS["status_line"] in plain
    assert LABELS["viewer"]["phantom_label"] in plain
    assert LABELS["viewer"]["strain_legend"] + LABELS["viewer"]["mock_suffix"] in plain
    assert "https://doi.org/10.7937/k4qv-hh78" in text
    assert "https://creativecommons.org/licenses/by/4.0/" in text


@pytest.mark.parametrize("doc", ["ATTRIBUTION.md", "PROVENANCE.json"])
def test_bundle_docs_pass_the_wording_lint(doc):
    text = doc_texts()[doc]
    hits = [m.group(0) for p in LABELS["wording"]["banned_claims"] for m in re.finditer(p, text, re.I)]
    assert not hits, hits
    assert not re.search(r"\b(19|20)\d{2}-?(0[1-9]|1[0-2])-?(0[1-9]|[12]\d|3[01])\b", text), \
        "scan dates are shown as Day 0 / Day 273, never as calendar dates"


# ------------------------------------------------------------------ sources in RB_DATA_DIR (skips without it)

@pytest.mark.parametrize("rel", sorted(FILES))
def test_each_file_is_a_byte_copy_of_its_source(rel, rb_data_dir):
    pattern = FILES[rel]["source"].removeprefix("RB_DATA_DIR/")
    matches = sorted(rb_data_dir.glob(pattern))
    assert len(matches) == 1, (rel, pattern, matches)
    assert sha256(matches[0]) == FILES[rel]["sha256"], rel


@pytest.mark.parametrize("script", sorted(PROV["scripts"]))
def test_each_source_script_matches_its_hash(script, rb_data_dir):
    p = rb_data_dir / script.removeprefix("RB_DATA_DIR/")
    assert p.is_file(), script
    assert sha256(p) == PROV["scripts"][script]["sha256"], script
