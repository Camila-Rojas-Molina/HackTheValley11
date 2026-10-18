"""Build and check the C1 contract files from api/app/schemas.py.

    .venv/bin/python scripts/build_contracts.py          # rewrite contracts/fixtures/** and contracts/openapi.json
    .venv/bin/python scripts/build_contracts.py --check  # change nothing; exit 1 if a file is stale or a rule fails

contracts/openapi.json is generated from a stub app that mounts every route in schemas.ROUTES with
route_kwargs(), so it depends only on api/app/schemas.py (and the pinned fastapi/pydantic versions in
scripts/requirements-repo.txt). The fixtures are golden payloads: real measured values from the data spike,
everything not computed yet is `pending` with `available_in`. tests/repo/contracts/ runs the same checks.
Only S0 changes a contract, as its own commit.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Annotated, Any, Optional

from fastapi import APIRouter, FastAPI, Query

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "api"))
from app import schemas as S  # noqa: E402

CONTRACTS = REPO / "contracts"
FIX = CONTRACTS / "fixtures"
LABELS = json.loads((CONTRACTS / "labels.json").read_text(encoding="utf-8"))
LV = LABELS["version"]
T0 = "2026-01-17T00:00:00Z"   # demo calendar Day 0
T273 = "2026-10-17T00:00:00Z"
NOW = "2026-10-17T20:45:00Z"
API = S.API_PREFIX

# real values (rb-spikes/data/out/myel018/per_vertebra.json, summary.json, L1 crop headers)
D273_FILES = {
    "ct.nii.gz": (1111145, "4ca90d8bb7dc061418def973dea8d04933c70dfd39ac01207f708a13a98468f1"),
    "mask_vertebra.expert.nii.gz": (8367, "b22e99cd5dda3939f74779429f9206e73c241057a6b3ba23b3a8f9f327633f1d"),
    "mask_lesion.expert.nii.gz": (2375, "163103d9f49ec3af765b94adf563a83c258ecfd46aab34512534fe1b343dae38"),
}
L1_AFFINE_D273 = [[-1.0, 0.0, 0.0, 62.875], [0.0, -1.0, 0.0, -122.744], [0.0, 0.0, 1.0, -568.673],
                  [0.0, 0.0, 0.0, 1.0]]
# M3: the demo bundle (demo/PROVENANCE.json, S2). Day 0 = spike R0's L1 crop of scan A (same crop.py, 10 mm margin);
# the phantom CT and its mock strain field are synthetic.
D0_FILES = {
    "ct.nii.gz": (1120634, "544fe5a2ba2413c8439bf9d8665f9ce9de26314b3b9622bddf54fd5afa1851e4"),
    "mask_vertebra.expert.nii.gz": (8563, "748d247cce07218c847014d4062efae451459f52f3bc67b8a06d9d939d5cab4c"),
    "mask_lesion.expert.nii.gz": (943, "18bab9992d887d882e900fe197473f6402babd1e269771c21e27bd107207174f"),
}
L1_AFFINE_D0 = [[-1.0, 0.0, 0.0, 50.186], [0.0, -1.0, 0.0, -131.024], [0.0, 0.0, 1.0, -641.44],
                [0.0, 0.0, 0.0, 1.0]]
L1_D0_CROP_VOXELS = {"lesion": 0, "vertebra": 69376}   # 1 mm3 each
PHANTOM_FILES = {
    "ct.nii.gz": (1321186, "9c035f24c27be8c04dfb1acc5056c69ff688314ed7074506fd20e79c0652aea5"),
    "strain_min_principal_ue.mock.nii.gz": (48721, "52e928239f966ffd90d8112a68527436a1dbdcc3ded23198e8bd86d452bed177"),
    "strain_min_principal_ue.mock.mz3": (227072, "8d6d3c3daec21545915afcd0e3507ce6b5951d63a3916345817ea7d6b09e8d09"),
}
LADDER = {  # level: (lesion_d0, vert_d0, lesion_d273, vert_d273), cm3, original grid, expert masks
    "T11": (0.53, 62.18, 1.44, 61.12), "T12": (0.42, 64.00, 1.06, 64.52), "L1": (0.00, 69.24, 3.24, 68.52),
    "L2": (0.80, 71.76, 0.22, 71.60), "L3": (0.63, 78.69, 2.24, 78.66), "L4": (0.10, 77.88, 3.32, 77.31),
    "L5": (1.09, 79.58, 2.34, 79.21),
}

MEDIA = {".nii.gz": "application/gzip", ".mz3": "application/octet-stream", ".vtp": "application/xml",
         ".vti": "application/xml", ".stl": "model/stl", ".json": "application/json"}


def media_type(name: str) -> str:
    for ext, mt in MEDIA.items():
        if name.endswith(ext):
            return mt
    raise ValueError(name)


def metric(key, value, unit, basis, prov="real_scan", **kw) -> dict:
    d = {"key": key, "value": value, "unit": unit, "basis": basis, "provenance": prov}
    d.update(kw)
    return d


def pending(key, unit, milestone, **kw) -> dict:
    return metric(key, None, unit, "pending", available_in=milestone, **kw)


def asset(scan_id, name, kind, basis, prov, status, *, variant=None, field=None, available_in=None,
          size=None, sha=None) -> dict:
    return {"name": name, "kind": kind, "url": f"{API}/scans/{scan_id}/assets/{name}", "media_type": media_type(name),
            "variant": variant, "field": field, "basis": basis, "provenance": prov, "status": status,
            "available_in": available_in, "bytes": size, "sha256": sha}


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ------------------------------------------------------------------ number check and reading grade (C4 rules)
NUM_RE = re.compile(r"(?<![A-Za-z0-9.])[-−]?\d+(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    return sorted({float(m.group(0).replace("−", "-")) for m in NUM_RE.finditer(text)})


def facts_numbers(facts: dict) -> set[float]:
    return {float(v) for v in facts.values() if isinstance(v, (int, float)) and not isinstance(v, bool)}


WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[.'’][A-Za-z0-9]+)*")


def syllables(word: str) -> int:
    w = word.lower()
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups)
    if w.endswith("e") and not w.endswith("le") and n > 1:
        n -= 1
    return max(1, n)


def fk_grade(text: str) -> float:
    sentences = [s for s in re.split(r"[.!?]+(?=\s|$)", text) if s.strip()]
    words = [w for w in WORD_RE.findall(text) if not re.search(r"\d", w)]
    syl = sum(syllables(w) for w in words)
    return round(0.39 * (len(words) / len(sentences)) + 11.8 * (syl / len(words)) - 15.59, 1)


# ------------------------------------------------------------------ fixtures
def manifests() -> dict[str, dict]:
    pend_assets_real = lambda sid: [  # noqa: E731
        asset(sid, "mask_body.expert.nii.gz", "mask_body", "pending", "real_scan", "pending", variant="expert",
              available_in="M5"),
        asset(sid, "strain_min_principal_ue.expert.nii.gz", "strain_volume", "pending", "real_scan", "pending",
              variant="expert", field="strain_min_principal_ue", available_in="M5"),
        asset(sid, "strain_min_principal_ue.expert.mz3", "strain_surface", "pending", "real_scan", "pending",
              variant="expert", field="strain_min_principal_ue", available_in="M5"),
        asset(sid, "context_T11-L5.mz3", "context_mesh", "pending", "real_scan", "pending", available_in="M5"),
        asset(sid, "vertebra.expert.stl", "stl", "pending", "real_scan", "pending", variant="expert",
              available_in="M5"),
    ]
    common_scales = [
        {"layer": "ct", "colormap": "gray", "cal_min": -500, "cal_max": 1500, "unit": None, "display": "window",
         "opacity": 1.0, "scope": "patient_level", "basis": "measured"},
        {"layer": "mask_vertebra", "colormap": "green", "cal_min": 0, "cal_max": 1, "unit": None,
         "display": "label", "opacity": 0.15, "scope": "patient_level", "basis": "measured"},
        {"layer": "mask_lesion", "colormap": "red", "cal_min": 0, "cal_max": 1, "unit": None, "display": "label",
         "opacity": 0.6, "scope": "patient_level", "basis": "measured"},
    ]
    mask_sources = {"vertebra": "expert", "body": None, "lesion": "expert", "body_split_method": None,
                    "caption": LABELS["viewer"]["lesion_outline_caption"]}

    def change_metrics(sid_base):
        return [
            pending("stiffness_change_axial_pct", "pct", "M5", mask_source="expert", statistic="central80_mean",
                    compared_to_scan_id=sid_base),
            pending("lesion_share_pts", "pts", "M5", mask_source="expert", statistic="central80_mean",
                    compared_to_scan_id=sid_base),
            pending("background_change_pct", "pct", "M5", mask_source="expert", statistic="central80_mean",
                    compared_to_scan_id=sid_base),
            pending("stiffness_change_axial_pct", "pct", "M5", mask_source="ai", statistic="central80_mean",
                    compared_to_scan_id=sid_base),
            pending("strain_ratio", "ratio", "M5", mask_source="expert", lesion_model="weak_bone",
                    compared_to_scan_id=sid_base),
            pending("modulus_lesion_gpa", "gpa", "M5", mask_source="expert", lesion_model="weak_bone"),
            pending("modulus_lesion_gpa", "gpa", "M5", mask_source="expert", lesion_model="void"),
            pending("modulus_ref_gpa", "gpa", "M5", mask_source="expert"),
            pending("vonmises_p99_mpa", "mpa", "M5", mask_source="expert", lesion_model="weak_bone"),
            pending("dice_ai_vs_expert", "dice", "M5"),
        ]

    d273 = "myel018-L1-d273"
    d0 = "myel018-L1-d0"
    m273 = {
        "schema": "rb.manifest/1", "scan_id": d273, "patient_id": "demo-a", "label": "Day 273", "level": "L1",
        "day": 273, "time": T273, "provenance": "real_scan", "simulated": False, "generator_version": None,
        "anchor_scan_ids": [], "seed": None,
        "acquisition": {"acquisition_id": "myel018-b", "dataset_case": "Myel_018",
                        "series_instance_uid": "1.3.6.1.4.1.14519.5.2.1.214314532294190421148158394502115174580",
                        "series_description": "Myel_018_b_konv", "modality": "CT", "kvp": 120,
                        "source_shape": [512, 512, 1035], "source_spacing_mm": [0.977, 0.977, 0.6]},
        "grid": {"shape": [95, 109, 80], "spacing_mm": [1.0, 1.0, 1.0], "affine": L1_AFFINE_D273,
                 "margin_mm": 10, "dtype": "int16"},
        "registration": {"status": "not_registered", "fixed_scan_id": None, "method": None, "available_in": "M5"},
        "calibration": None, "calibration_available_in": "M5",
        "mask_sources": mask_sources,
        "assets": [asset(d273, n, k, "measured", "real_scan", "ready", variant=v, size=D273_FILES[n][0],
                         sha=D273_FILES[n][1])
                   for n, k, v in (("ct.nii.gz", "ct", None), ("mask_vertebra.expert.nii.gz", "mask_vertebra", "expert"),
                                   ("mask_lesion.expert.nii.gz", "mask_lesion", "expert"))] + pend_assets_real(d273),
        "color_scales": common_scales,
        "metrics": [
            metric("lesion_volume_cm3", 3.24, "cm3", "measured", grid="original", mask_source="expert"),
            metric("lesion_volume_cm3", 3.22, "cm3", "measured", grid="crop_1mm", mask_source="expert",
                   note="3,217 voxels at 1 mm"),
            metric("vertebra_volume_cm3", 68.52, "cm3", "measured", grid="original", mask_source="expert"),
            metric("vertebra_volume_cm3", 67.79, "cm3", "measured", grid="crop_1mm", mask_source="expert"),
            metric("lesion_pct_of_vertebra", 4.73, "pct", "measured", grid="original", mask_source="expert"),
            pending("body_volume_cm3", "cm3", "M5", grid="crop_1mm", mask_source="expert"),
            pending("lesion_pct_of_body", "pct", "M5", grid="crop_1mm", mask_source="expert"),
        ] + change_metrics(d0),
        "labels_version": LV,
    }
    m0 = {
        **m273, "scan_id": d0, "label": "Day 0", "day": 0, "time": T0,
        "acquisition": {"acquisition_id": "myel018-a", "dataset_case": "Myel_018",
                        "series_instance_uid": "1.3.6.1.4.1.14519.5.2.1.163416561557440954759359029287186276821",
                        "series_description": "Myel_018_a_konv", "modality": "CT", "kvp": 120,
                        "source_shape": [512, 512, 1021], "source_spacing_mm": [0.883, 0.883, 0.6]},
        "grid": {"shape": [95, 110, 80], "spacing_mm": [1.0, 1.0, 1.0], "affine": L1_AFFINE_D0,
                 "margin_mm": 10, "dtype": "int16"},
        "assets": [asset(d0, n, k, "measured", "real_scan", "ready", variant=v, size=D0_FILES[n][0],
                         sha=D0_FILES[n][1])
                   for n, k, v in (("ct.nii.gz", "ct", None), ("mask_vertebra.expert.nii.gz", "mask_vertebra", "expert"),
                                   ("mask_lesion.expert.nii.gz", "mask_lesion", "expert"))]
        + [a for a in pend_assets_real(d0) if a["kind"] in ("mask_body", "strain_volume", "strain_surface", "stl")],
        "metrics": [
            metric("lesion_volume_cm3", 0.0, "cm3", "measured", grid="original", mask_source="expert"),
            metric("lesion_volume_cm3", round(L1_D0_CROP_VOXELS["lesion"] / 1000.0, 2), "cm3", "measured",
                   grid="crop_1mm", mask_source="expert", note=f"{L1_D0_CROP_VOXELS['lesion']:,} voxels at 1 mm"),
            metric("vertebra_volume_cm3", 69.24, "cm3", "measured", grid="original", mask_source="expert"),
            metric("vertebra_volume_cm3", round(L1_D0_CROP_VOXELS["vertebra"] / 1000.0, 2), "cm3", "measured",
                   grid="crop_1mm", mask_source="expert"),
            metric("lesion_pct_of_vertebra", 0.0, "pct", "measured", grid="original", mask_source="expert"),
            pending("body_volume_cm3", "cm3", "M5", grid="crop_1mm", mask_source="expert"),
            pending("strain_p99_lesion_ue", "ue", "M5", mask_source="expert", lesion_model="weak_bone",
                    note="lesion region of Day 273, mapped onto Day 0 after registration"),
            pending("dice_ai_vs_expert", "dice", "M5"),
        ],
    }
    ph = {
        "schema": "rb.manifest/1", "scan_id": "phantom", "patient_id": None, "label": "Synthetic phantom",
        "level": None, "day": None, "time": None, "provenance": "synthetic_insilico", "simulated": True,
        "generator_version": "phantom-femur-shaft/1", "anchor_scan_ids": [], "seed": 0,
        "acquisition": {"acquisition_id": "phantom", "dataset_case": None, "series_instance_uid": None,
                        "series_description": "Synthetic femur-shaft phantom with a lytic defect", "modality": "CT",
                        "kvp": None, "source_shape": None, "source_spacing_mm": None},
        "grid": {"shape": [96, 96, 128], "spacing_mm": [1.0, 1.0, 1.0],
                 "affine": [[1.0, 0.0, 0.0, -48.0], [0.0, 1.0, 0.0, -48.0], [0.0, 0.0, 1.0, -64.0],
                            [0.0, 0.0, 0.0, 1.0]], "margin_mm": None, "dtype": "int16"},
        "registration": {"status": "not_registered", "fixed_scan_id": None, "method": None, "available_in": None},
        "calibration": None, "calibration_available_in": None,
        "mask_sources": {"vertebra": None, "body": None, "lesion": None, "body_split_method": None,
                         "caption": LABELS["viewer"]["phantom_label"]},
        "assets": [
            asset("phantom", n, k, "mock", "synthetic_insilico", "ready", variant=v, field=f,
                  size=PHANTOM_FILES[n][0], sha=PHANTOM_FILES[n][1])
            for n, k, v, f in (("ct.nii.gz", "ct", None, None),
                               ("strain_min_principal_ue.mock.nii.gz", "strain_volume", "mock", "strain_min_principal_ue"),
                               ("strain_min_principal_ue.mock.mz3", "strain_surface", "mock", "strain_min_principal_ue"))
        ],
        "color_scales": [
            {**common_scales[0], "scope": "scan", "basis": "mock"},
            {"layer": "strain_min_principal_ue", "colormap": "turbo", "cal_min": 0, "cal_max": 1500, "unit": "ue",
             "display": "magnitude", "opacity": 0.8, "scope": "scan", "basis": "mock"},
        ],
        "metrics": [],
        "labels_version": LV,
    }
    return {d273: m273, d0: m0, "phantom": ph}


def flag_pending() -> dict:
    tmpl = LABELS["flag"]["templates"]["pending"]
    line = tmpl.format(change="new lesion", level="L1", vol_a="0.00", vol_b="3.24", available_in="M5")
    return {
        "status": "pending", "rule_version": LABELS["flag"]["rule"]["version"], "level": "L1",
        "scan_id": "myel018-L1-d273", "alert_id": None,
        "conditions": [
            {"key": "lesion_new_or_grew", "met": True, "value": 3.24, "threshold": None, "unit": "cm3",
             "basis": "measured", "available_in": None, "detail": "new lesion at L1: 0.00 to 3.24 cm3 (original grid)"},
            {"key": "lesion_share_at_least", "met": None, "value": None,
             "threshold": LABELS["flag"]["rule"]["lesion_share_pts_min"], "unit": "pts", "basis": "pending",
             "available_in": "M5", "detail": None},
            {"key": "share_exceeds_calibration_range", "met": None, "value": None, "threshold": None, "unit": "pts",
             "basis": "pending", "available_in": "M5", "detail": None},
        ],
        "line": line, "protocol_line": LABELS["flag"]["protocol_line"],
        "data_quality": {"mask_source": "expert", "lesion_source": "expert", "calibration_range": None,
                         "provenance": "real_scan", "simulated": False},
    }


def scan_ref(sid, label, day, time) -> dict:
    return {"scan_id": sid, "patient_id": "demo-a", "level": "L1", "day": day, "time": time, "label": label,
            "provenance": "real_scan", "simulated": False, "status": "ready",
            "has_strain": False}


def facts_d273() -> dict:
    return {"facts_version": "1", "scan_id": "myel018-L1-d273", "level": "L1", "day_a": 0, "day_b": 273,
            "interval_days": 273, "lesion_volume_a_cm3": 0.0, "lesion_volume_b_cm3": 3.24,
            "lesion_volume_change_cm3": 3.24, "lesion_grid": "original", "vertebra_volume_b_cm3": 68.52,
            "lesion_pct_of_vertebra_b": 4.73, "stiffness_change_axial_pct": None, "lesion_share_pts": None,
            "background_change_pct": None, "calib_low_pct": None, "calib_high_pct": None, "strain_ratio": None,
            "mask_source": "expert", "lesion_source": "expert", "provenance": "real_scan", "simulated": False}


def generation(task, headline, detail, facts, attempts, *, override=False) -> dict:
    text = headline + " " + detail
    nums = numbers_in(text)
    fset = facts_numbers(facts)
    matched = sum(1 for n in nums if n in fset)
    badge = LABELS["ai"]["badge_template"].format(n=matched, m=len(nums))
    return {
        "scan_id": facts["scan_id"], "task": task,
        "text": {"headline": headline, "detail": detail, "numbers_used": nums},
        "footer": LABELS["ai"]["footer"], "source": "template", "model": None, "verified": matched == len(nums),
        "numbers_matched": matched, "numbers_total": len(nums), "badge": badge, "attempts": attempts,
        "facts_sha256": hashlib.sha256(canonical(facts).encode()).hexdigest(),
        "prompt_version": "template/1", "generated_at": NOW, "cache_hit": False, "chain_override": override,
        "tokens_in": None, "tokens_out": None, "cost_usd": None,
    }


def audit_trail() -> dict:
    key = "scan:myel018-L1-d273"
    entries, prev = [], ""
    rows = [
        ("2026-10-17T20:40:00.000000+00:00", "seed", "seed_demo",
         {"assets": {n: s for n, (_, s) in D273_FILES.items()}, "source": "demo/myel018-L1-d273",
          "provenance": "real_scan"}),
    ]
    for i, (at, actor, action, detail) in enumerate(rows, 1):
        payload = {"chain_key": key, "at": at, "actor": actor, "action": action, "patient_id": "demo-a",
                   "scan_id": "myel018-L1-d273", "run_id": None, "detail": detail}
        h = hashlib.sha256((prev + canonical(payload)).encode()).hexdigest()
        entries.append({"seq": i, "at": at, "actor": actor, "action": action, "patient_id": "demo-a",
                        "scan_id": "myel018-L1-d273", "run_id": None, "detail": detail, "prev_hash": prev,
                        "hash": h})
        prev = h
    return {"scan_id": "myel018-L1-d273", "chain_key": key, "chain_ok": True, "head": prev, "entries": entries}


def build(out: dict[str, str]) -> list[dict]:
    """Renders every fixture into `out` (path relative to contracts/fixtures -> text); returns the index."""
    idx: list[dict] = []

    def put(rel: str, op: str, payload: Any, model: str, *, status=200, path=None, method="GET",
            kind="response", note=None):
        obj = S.__dict__[model].model_validate(payload).model_dump(mode="json", by_alias=True)
        out[rel] = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
        idx.append({"file": rel, "operation_id": op, "method": method, "path": path, "status": status,
                    "model": model, "kind": kind, "note": note})

    ms = manifests()
    for sid, m in ms.items():
        put(f"scans/{sid}.json", "get_scan", m, "ScanManifest", path=f"{API}/scans/{sid}")

    put("health.json", "get_health", {"status": "ok", "db": "ok", "version": S.CONTRACT_VERSION,
                                      "labels_version": LV, "server_time": NOW, "timescaledb": True,
                                      "ai_chain": ["template"], "queue": None}, "Health", path=f"{API}/health")
    put("health.down.json", "get_health", {"status": "down", "db": "down", "version": S.CONTRACT_VERSION,
                                           "labels_version": LV, "server_time": None, "timescaledb": None,
                                           "ai_chain": ["template"], "queue": None}, "Health",
        status=503, path=f"{API}/health")

    flag = flag_pending()
    item = {"patient_id": "demo-a", "label": "Demo patient A", "dataset": "TCIA Spinal-Multiple-Myeloma-SEG",
            "dataset_case": "Myel_018", "latest_scan_id": "myel018-L1-d273", "latest_day": 273, "scan_count": 2,
            "flag": flag}
    put("patients.json", "list_patients", {"patients": [item]}, "PatientList", path=f"{API}/patients")
    refs = [scan_ref("myel018-L1-d0", "Day 0", 0, T0), scan_ref("myel018-L1-d273", "Day 273", 273, T273)]
    put("patients/demo-a.json", "get_patient",
        {**item, "scans": refs, "sites": ["T11", "T12", "L1", "L2", "L3", "L4", "L5"]}, "PatientDetail",
        path=f"{API}/patients/demo-a")
    put("patients/demo-a/scans.json", "list_patient_scans", {"patient_id": "demo-a", "scans": refs}, "ScanList",
        path=f"{API}/patients/demo-a/scans")

    rows = []
    for lvl, (l0, v0, l1, v1) in LADDER.items():
        g = dict(grid="original", mask_source="expert")
        rows.append({
            "level": lvl, "excluded": False, "exclusion_reason": None,
            "baseline": [metric("lesion_volume_cm3", l0, "cm3", "measured", **g),
                         metric("vertebra_volume_cm3", v0, "cm3", "measured", **g)],
            "followup": [metric("lesion_volume_cm3", l1, "cm3", "measured", **g),
                         metric("vertebra_volume_cm3", v1, "cm3", "measured", **g)],
            "change": [pending(k, u, "M5", mask_source="expert", statistic="central80_mean",
                               compared_to_scan_id="myel018-L1-d0" if lvl == "L1" else None)
                       for k, u in (("stiffness_change_axial_pct", "pct"), ("lesion_share_pts", "pts"),
                                    ("background_change_pct", "pct"))],
        })
    put("patients/demo-a/levels.json", "get_patient_levels",
        {"patient_id": "demo-a", "baseline_scan_day": 0, "followup_scan_day": 273, "statistic": "central80_mean",
         "levels": rows, "source": "fixture", "query_ms": {"raw_ms": None, "aggregate_ms": None}}, "LevelLadder",
        path=f"{API}/patients/demo-a/levels")

    def pt(day, time, value, basis, sid, **kw):
        return {"time": time, "day": day, "value": value, "basis": basis, "provenance": "real_scan",
                "filled": False, "scan_id": sid, "mask_source": "expert", **kw}
    put("patients/demo-a/series.site-L1.metric-lesion_volume_cm3.json", "get_patient_series",
        {"patient_id": "demo-a", "site": "L1", "metric": "lesion_volume_cm3", "unit": "cm3",
         "points": [pt(0, T0, 0.0, "measured", "myel018-L1-d0"), pt(273, T273, 3.24, "measured", "myel018-L1-d273")],
         "contains_synthetic": False, "source": "fixture", "query_ms": None}, "Series",
        path=f"{API}/patients/demo-a/series?site=L1&metric=lesion_volume_cm3")
    put("patients/demo-a/series.site-L1.metric-stiffness_change_axial_pct.json", "get_patient_series",
        {"patient_id": "demo-a", "site": "L1", "metric": "stiffness_change_axial_pct", "unit": "pct",
         "points": [pt(0, T0, None, "pending", "myel018-L1-d0", available_in="M5"),
                    pt(273, T273, None, "pending", "myel018-L1-d273", available_in="M5")],
         "contains_synthetic": False, "source": "fixture", "query_ms": None}, "Series",
        path=f"{API}/patients/demo-a/series?site=L1&metric=stiffness_change_axial_pct")

    facts = facts_d273()
    put("ai/facts.myel018-L1-d273.json", "get_scan_summary", facts, "Facts", path=None, kind="facts",
        note="input to the AI chain; never served by a route")
    tmpl_attempt = [{"provider": "template", "model": None, "ms": 0.0, "outcome": "accepted", "reason": None}]
    s = LABELS["ai"]["templates"]["care_team_summary_lesion_only"]
    summ = generation("care_team_summary", s["headline"].format(**facts_fmt(facts)),
                      s["detail"].format(**facts_fmt(facts)), facts, tmpl_attempt)
    put("scans/myel018-L1-d273/summary.json", "get_scan_summary", summ, "Generation",
        path=f"{API}/scans/myel018-L1-d273/summary")

    drill_attempts = [
        {"provider": "groq", "model": "openai/gpt-oss-120b", "ms": 0.0, "outcome": "unavailable",
         "reason": "GROQ_API_KEY not set"},
        {"provider": "vultr", "model": "qwen3.8-flash-next", "ms": 0.0, "outcome": "unavailable",
         "reason": "VULTR_API_KEY not set"},
        {"provider": "template", "model": None, "ms": 0.0, "outcome": "accepted", "reason": None}]
    put("_post/scans/myel018-L1-d273/summary/regenerate.request.json", "regenerate_scan_summary",
        {"chain": ["groq", "vultr", "template"], "bypass_cache": True, "task": "care_team_summary"},
        "RegenerateRequest", path=f"{API}/scans/myel018-L1-d273/summary/regenerate", method="POST", kind="request")
    put("_post/scans/myel018-L1-d273/summary/regenerate.response.json", "regenerate_scan_summary",
        {**summ, "attempts": drill_attempts, "chain_override": True}, "Generation",
        path=f"{API}/scans/myel018-L1-d273/summary/regenerate", method="POST")

    e = LABELS["ai"]["templates"]["patient_explanation_lesion_only"]
    expl = generation("patient_explanation", e["headline"].format(**facts_fmt(facts)),
                      e["detail"].format(**facts_fmt(facts)), facts, tmpl_attempt)
    expl.update(reading_grade=fk_grade(expl["text"]["headline"] + " " + expl["text"]["detail"]),
                reading_grade_formula="flesch_kincaid_grade/1",
                target_grade_max=LABELS["ai"]["reading_grade"]["target_max"],
                review_note=LABELS["ai"]["patient_review_note"])
    put("scans/myel018-L1-d273/explanation.json", "get_scan_explanation", expl, "Explanation",
        path=f"{API}/scans/myel018-L1-d273/explanation")

    job_id = "6f1c2a9e-3b7d-4c55-9a0e-2d8f4b1e7c30"
    run_id = "0b9e5d6c-8a41-4f2e-b3c7-5e1d9a2f6c48"
    put(f"jobs/{job_id}.json", "get_job",
        {"job_id": job_id, "kind": "physics", "status": "queued", "scan_id": "myel018-L1-d273",
         "patient_id": "demo-a", "run_id": run_id,
         "params": {"scan_id": "myel018-L1-d273", "level": "L1", "baseline_scan_id": "myel018-L1-d0",
                    "mask_source": "expert", "lesion_models": ["weak_bone", "void"], "load_n": 800.0,
                    "voxel_mm": 1.0},
         "result": None, "error": None, "attempts": 0, "runner": None, "device": None, "created_at": NOW,
         "started_at": None, "finished_at": None, "seconds": None, "cost_usd": None, "cost": None}, "Job",
        path=f"{API}/jobs/{job_id}")
    put("_post/scans/myel018-L1-d273/runs.request.json", "start_scan_run",
        {"steps": ["physics"], "levels": ["L1"], "mask_source": "expert", "lesion_models": ["weak_bone", "void"],
         "baseline_scan_id": "myel018-L1-d0", "load_n": 800.0, "voxel_mm": 1.0}, "RunRequest",
        path=f"{API}/scans/myel018-L1-d273/runs", method="POST", kind="request")
    put("_post/scans/myel018-L1-d273/runs.response.json", "start_scan_run",
        {"run_id": run_id, "scan_id": "myel018-L1-d273",
         "jobs": [{"job_id": job_id, "kind": "physics", "status": "queued"}]}, "RunAccepted", status=202,
        path=f"{API}/scans/myel018-L1-d273/runs", method="POST")
    put("_post/scans.response.json", "upload_scan",
        {"scan_id": "demo-b-u7c41e2a9", "patient_id": "demo-b", "status": "pending",
         "jobs": [{"job_id": "9d3e7f10-2c6b-4a8e-8f51-c0b4a7d2e916", "kind": "segment", "status": "queued"}]},
        "ScanUploadAccepted", status=202, path=f"{API}/scans?patient_id=demo-b&day=0", method="POST",
        note="request body: the CT as application/gzip (.nii.gz), see formats.md")

    put("_post/alerts/al-myel018-L1-d273/feedback.request.json", "post_alert_feedback",
        {"feedback": "snooze", "snooze_hours": 24, "note": None}, "AlertFeedbackRequest",
        path=f"{API}/alerts/al-myel018-L1-d273/feedback", method="POST", kind="request")
    put("_post/alerts/al-myel018-L1-d273/feedback.response.json", "post_alert_feedback",
        {"feedback_id": 1, "alert_id": "al-myel018-L1-d273", "feedback": "snooze",
         "snooze_until": "2026-10-18T20:45:00Z", "recorded_at": NOW}, "AlertFeedbackAck", status=201,
        path=f"{API}/alerts/al-myel018-L1-d273/feedback", method="POST")

    c = LABELS["copilot"]
    q1 = c["suggested_questions"][0]
    put("_post/copilot.trend.request.json", "post_copilot", {"question": q1, "patient_id": "demo-a"},
        "CopilotRequest", path=f"{API}/copilot", method="POST", kind="request")
    ans = c["template_answers"]["l1_change_lesion_only"].format(vol_a="0.00", vol_b="3.24", day_a=0, day_b=273)
    nums = numbers_in(ans)
    put("_post/copilot.trend.response.json", "post_copilot",
        {"answer": ans, "refused": False,
         "tool_calls": [{"tool": "get_metric_series", "args": {"site": "L1", "metric": "lesion_volume_cm3"},
                         "rows": 2}],
         "mentions_simulated": False, "source": "template", "model": None, "verified": True,
         "numbers_matched": len(nums), "numbers_total": len(nums), "footer": LABELS["ai"]["footer"],
         "attempts": tmpl_attempt}, "CopilotAnswer", path=f"{API}/copilot", method="POST")
    put("_post/copilot.refusal.request.json", "post_copilot",
        {"question": "Should she have surgery?", "patient_id": "demo-a"}, "CopilotRequest",
        path=f"{API}/copilot", method="POST", kind="request")
    put("_post/copilot.refusal.response.json", "post_copilot",
        {"answer": c["refusal"], "refused": True, "tool_calls": [], "mentions_simulated": False,
         "source": "template", "model": None, "verified": True, "numbers_matched": 0, "numbers_total": 0,
         "footer": LABELS["ai"]["footer"], "attempts": []}, "CopilotAnswer", path=f"{API}/copilot", method="POST")

    put("tiger/info.json", "get_tiger_info",
        {"backend": "timescaledb", "timescaledb_version": None,
         "hypertables": [{"name": n, "chunk_interval": ci, "chunks": None, "compressed_chunks": None,
                          "rows_estimate": None, "before_bytes": None, "after_bytes": None}
                         for n, ci in (("scan_metrics", "90 days"), ("fe_element_results", "30 days"),
                                       ("ai_generation", "7 days"))],
         "continuous_aggregates": [
             {"name": "scan_metrics_monthly", "hypertable": "scan_metrics", "bucket": "1 month",
              "group_by": ["bucket", "patient_id", "site", "provenance", "mask_source", "lesion_model",
                           "lesion_grid", "statistic"],
              "materialized_only": None},
             {"name": "fe_level_daily", "hypertable": "fe_element_results", "bucket": "1 day",
              "group_by": ["bucket", "patient_id", "site", "provenance", "mask_source", "lesion_model", "run_id"],
              "materialized_only": None}],
         "db_size_bytes": None, "db_size_pretty": None, "budget_bytes": 300 * 1024 * 1024,
         "query_ms": {"raw_ms": None, "aggregate_ms": None}, "source": "fixture", "measured_at": NOW},
        "TigerInfo", path=f"{API}/tiger/info")
    put("audit/myel018-L1-d273.json", "get_audit_trail", audit_trail(), "AuditTrail",
        path=f"{API}/audit/myel018-L1-d273")

    # error bodies
    put("_errors/not_available.json", "*", {"detail": "lands in M5", "available_in": "M5"}, "NotAvailable",
        status=501, kind="error")
    put("_errors/unauthorized.json", "*", {"detail": "X-Admin-Key missing or wrong"}, "ErrorDetail", status=401,
        kind="error")
    put("_errors/not_found.json", "*", {"detail": "unknown scan_id"}, "ErrorDetail", status=404, kind="error")
    put("_errors/db_unavailable.json", "*", {"ok": False, "error": "database unavailable"}, "Unavailable",
        status=503, kind="error")
    put("_errors/too_large.json", "upload_scan", {"detail": "upload over RB_MAX_UPLOAD_MB"}, "ErrorDetail",
        status=413, kind="error")
    return idx


def facts_fmt(f: dict) -> dict:
    return {"level": f["level"], "day_a": f["day_a"], "day_b": f["day_b"], "interval_days": f["interval_days"],
            "vol_a": f"{f['lesion_volume_a_cm3']:.2f}", "vol_b": f"{f['lesion_volume_b_cm3']:.2f}",
            "pct_vert": f"{f['lesion_pct_of_vertebra_b']:.2f}"}


# ------------------------------------------------------------------ openapi
def contract_app():
    r = APIRouter(prefix=S.API_PREFIX)
    k = S.route_kwargs
    nie = None

    @r.get("/health", **k("get_health"))
    async def get_health(): return nie
    @r.get("/patients", **k("list_patients"))
    async def list_patients(): return nie
    @r.get("/patients/{patient_id}", **k("get_patient"))
    async def get_patient(patient_id: str): return nie
    @r.get("/patients/{patient_id}/scans", **k("list_patient_scans"))
    async def list_patient_scans(patient_id: str): return nie
    @r.get("/patients/{patient_id}/levels", **k("get_patient_levels"))
    async def get_patient_levels(patient_id: str): return nie
    @r.get("/patients/{patient_id}/series", **k("get_patient_series"))
    async def get_patient_series(patient_id: str, site: S.Site, metric: S.SeriesMetric): return nie
    @r.post("/scans", **k("upload_scan"))
    async def upload_scan(patient_id: Annotated[str, Query(pattern=S.ID_PATTERN)],
                          day: Annotated[int, Query(ge=0)], label: Optional[str] = None): return nie
    @r.get("/scans/{scan_id}", **k("get_scan"))
    async def get_scan(scan_id: str): return nie
    @r.get("/scans/{scan_id}/assets/{name}", **k("get_scan_asset"))
    async def get_scan_asset(scan_id: str, name: str): return nie
    @r.post("/scans/{scan_id}/runs", **k("start_scan_run"))
    async def start_scan_run(scan_id: str, body: S.RunRequest): return nie
    @r.get("/jobs/{job_id}", **k("get_job"))
    async def get_job(job_id: str): return nie
    @r.get("/scans/{scan_id}/summary", **k("get_scan_summary"))
    async def get_scan_summary(scan_id: str): return nie
    @r.post("/scans/{scan_id}/summary/regenerate", **k("regenerate_scan_summary"))
    async def regenerate_scan_summary(scan_id: str, body: S.RegenerateRequest): return nie
    @r.get("/scans/{scan_id}/explanation", **k("get_scan_explanation"))
    async def get_scan_explanation(scan_id: str): return nie
    @r.post("/alerts/{alert_id}/feedback", **k("post_alert_feedback"))
    async def post_alert_feedback(alert_id: str, body: S.AlertFeedbackRequest): return nie
    @r.post("/copilot", **k("post_copilot"))
    async def post_copilot(body: S.CopilotRequest): return nie
    @r.get("/tiger/info", **k("get_tiger_info"))
    async def get_tiger_info(): return nie
    @r.get("/audit/{scan_id}", **k("get_audit_trail"))
    async def get_audit_trail(scan_id: str): return nie

    app = FastAPI(title=S.API_TITLE, version=S.CONTRACT_VERSION)
    app.include_router(r)
    return app


# ------------------------------------------------------------------ checks
def check(idx: list[dict]) -> list[str]:
    errs: list[str] = []
    for e in idx:
        data = json.loads((FIX / e["file"]).read_text())
        try:
            S.__dict__[e["model"]].model_validate(data)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{e['file']}: {exc}")
    ops_with_fixture = {e["operation_id"] for e in idx if e["kind"] == "response"}
    for r in S.ROUTES:
        if r.response_model != "bytes" and r.operation_id not in ops_with_fixture:
            errs.append(f"no response fixture for {r.operation_id}")
    app = contract_app()
    spec = app.openapi()
    paths = {(m.upper(), p) for p, d in spec["paths"].items() for m in d}
    for r in S.ROUTES:
        if (r.method, S.API_PREFIX + r.path) not in paths:
            errs.append(f"route missing from openapi: {r.method} {r.path}")
    if len(paths) != len(S.ROUTES):
        errs.append(f"openapi has {len(paths)} operations, ROUTES has {len(S.ROUTES)}")
    # AI fixtures obey the number rule
    facts = json.loads((FIX / "ai/facts.myel018-L1-d273.json").read_text())
    for rel in ("scans/myel018-L1-d273/summary.json", "scans/myel018-L1-d273/explanation.json"):
        g = json.loads((FIX / rel).read_text())
        nums = numbers_in(g["text"]["headline"] + " " + g["text"]["detail"])
        bad = [n for n in nums if n not in facts_numbers(facts)]
        if bad:
            errs.append(f"{rel}: numbers not in facts: {bad}")
    blocked = re.compile("|".join(LABELS["ai"]["blocked_output_patterns"]), re.I)
    words = re.compile(r"\b(" + "|".join(LABELS["ai"]["number_words"]) + r")\b", re.I)
    for rel in ("scans/myel018-L1-d273/summary.json", "scans/myel018-L1-d273/explanation.json"):
        g = json.loads((FIX / rel).read_text())
        t = g["text"]["headline"] + " " + g["text"]["detail"]
        for rx, what in ((blocked, "blocked wording"), (words, "number word")):
            m = rx.search(t)
            if m:
                errs.append(f"{rel}: {what}: {m.group(0)!r}")
        if len(t) > LABELS["ai"]["max_chars"]:
            errs.append(f"{rel}: over max_chars")
    return errs


def render() -> dict[str, str]:
    """Every generated contract file, keyed by path relative to the repo root."""
    fixtures: dict[str, str] = {}
    idx = build(fixtures)
    files = {f"contracts/fixtures/{rel}": text for rel, text in fixtures.items()}
    files["contracts/fixtures/index.json"] = json.dumps(
        {"contract_version": S.CONTRACT_VERSION, "fixtures": idx}, indent=2) + "\n"
    files["contracts/openapi.json"] = json.dumps(contract_app().openapi(), indent=2, sort_keys=False) + "\n"
    return files


def stale_files(files: dict[str, str] | None = None) -> list[str]:
    """Generated files whose bytes on disk differ from a fresh render, plus fixtures nobody generates."""
    files = files if files is not None else render()
    stale = [rel for rel, text in sorted(files.items())
             if not (REPO / rel).is_file() or (REPO / rel).read_text(encoding="utf-8") != text]
    on_disk = {str(p.relative_to(REPO)) for p in FIX.rglob("*.json")}
    stale += sorted(f"{rel} (not generated by build_contracts.py)" for rel in on_disk - set(files))
    return stale


def main() -> int:
    only_check = "--check" in sys.argv[1:]
    files = render()
    if not only_check:
        for rel, text in files.items():
            p = REPO / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    errs = [f"stale: {rel}" for rel in stale_files(files)]
    idx = json.loads((FIX / "index.json").read_text(encoding="utf-8"))["fixtures"]
    errs += check(idx)
    spec_now = contract_app().openapi()
    for e in errs:
        print("FAIL", e)
    print(f"{len(idx)} fixtures, {len(S.ROUTES)} routes, {len(spec_now['components']['schemas'])} schemas: "
          + ("OK" if not errs else f"{len(errs)} errors"))
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
