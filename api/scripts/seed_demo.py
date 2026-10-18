"""Seed the demo patient (Myel_018 as "Demo patient A") and the synthetic phantom. Safe to run again.

    RB_DATABASE_URL=postgresql://... .venv/bin/python api/scripts/seed_demo.py     (from the repo root)

The API also runs it at start-up when RB_DEMO_SEED=1 (the default).

Every number below was measured by our own code on public data, never typed from a paper or a guess:
- Data: TCIA Spinal-Multiple-Myeloma-SEG (Nohel et al. 2026, DOI 10.7937/k4qv-hh78, CC BY 4.0), case Myel_018.
  Scan A (Day 0) = series Myel_018_a_konv, scan B (Day 273) = Myel_018_b_konv; the 273 days are the interval
  recorded in the de-identified data, shown on the demo calendar (Day 0 = 2026-01-17).
- Original-grid volumes (T11-L5, both scans): per_vertebra.json of the data spike, expert (radiologist-corrected)
  vertebra and lesion masks from the dataset's DICOM-SEG; sha256 in PER_VERTEBRA_SHA256.
- Scan metadata (shape, spacing, kVp, series UIDs): summary.json of the same spike (SUMMARY_SHA256).
- 1 mm crop of L1 on Day 273 (grid, affine, voxel counts, file sha256s): the data spike's crop of scan B,
  files L1_ct_1mm_20160906.nii.gz, L1_mask_1mm_20160906.nii.gz, L1_lesion_1mm_20160906.nii.gz in RB_DATA_DIR,
  served as ct.nii.gz, mask_vertebra.expert.nii.gz and mask_lesion.expert.nii.gz (contracts/formats.md).
- Phantom: synthetic, made by our own generator; its strain field is a mock (labelled so everywhere).
Everything not computed yet is left out, so the API serves it as `pending` with the milestone it lands in.

Idempotent, and safe at every API start: when nothing in this file changed (seed_sha256 in each scan's last
`seed_demo` audit entry) it writes nothing. When it did change, rows are upserted without undoing later work:
an asset the pipeline made ready stays ready, and a calibration, registration, grid or mask source a run
recorded is kept. The seeded scan_metrics rows (solver_version SEED_VERSION) are replaced; rows a run wrote are
never touched (every seed version is named seed_demo/<n>). A scan's audit chain gets a `seed_demo` entry only when what was seeded for it changed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

API = Path(__file__).resolve().parents[1]
if str(API) not in sys.path:
    sys.path.insert(0, str(API))

from app import audit, tiger  # noqa: E402

SEED_VERSION = "seed_demo/1"
DAY0 = datetime(2026, 1, 17, tzinfo=timezone.utc)
PATIENT = "demo-a"

DATASET = "TCIA Spinal-Multiple-Myeloma-SEG"
SOURCE_TCIA = ("TCIA Spinal-Multiple-Myeloma-SEG, Nohel et al. 2026, DOI 10.7937/k4qv-hh78, CC BY 4.0; "
               "case Myel_018")
PER_VERTEBRA = "rb-data/tcia/myel018/nifti/per_vertebra.json"
PER_VERTEBRA_SHA256 = "83f7760cc49485f9a32b8fc32a2bdf348f2ecd558535e31b8304f1f272025657"
SUMMARY = "rb-data/tcia/myel018/nifti/summary.json"
SUMMARY_SHA256 = "ecf95e219beced7e0a4c9cf40d860f4f1869dc72f8d69ecefc3b48b6e56b8cd5"

# per_vertebra.json, expert masks, original CT grid, cm3 as the spike wrote them (2 decimals):
# level: (lesion Day 0, vertebra Day 0, lesion Day 273, vertebra Day 273). Vertebra volume includes the lesion.
LADDER_CM3 = {
    "T11": (0.53, 62.18, 1.44, 61.12), "T12": (0.42, 64.00, 1.06, 64.52), "L1": (0.00, 69.24, 3.24, 68.52),
    "L2": (0.80, 71.76, 0.22, 71.60), "L3": (0.63, 78.69, 2.24, 78.66), "L4": (0.10, 77.88, 3.32, 77.31),
    "L5": (1.09, 79.58, 2.34, 79.21),
}
# The Day-273 L1 crop at 1 mm: 3,217 lesion voxels and 67,790 vertebra voxels of 1 mm3 each.
L1_D273_CROP_MM3 = {"lesion": 3217.0, "vertebra": 67790.0}
# The Day-0 L1 crop at 1 mm (spike R0, same crop.py; demo/PROVENANCE.json): 0 lesion voxels, 69,376 vertebra voxels.
L1_D0_CROP_MM3 = {"lesion": 0.0, "vertebra": 69376.0}

CT_SCALES = [
    {"layer": "ct", "colormap": "gray", "cal_min": -500.0, "cal_max": 1500.0, "unit": None, "display": "window",
     "opacity": 1.0, "scope": "patient_level", "basis": "measured"},
    {"layer": "mask_vertebra", "colormap": "green", "cal_min": 0.0, "cal_max": 1.0, "unit": None,
     "display": "label", "opacity": 0.15, "scope": "patient_level", "basis": "measured"},
    {"layer": "mask_lesion", "colormap": "red", "cal_min": 0.0, "cal_max": 1.0, "unit": None, "display": "label",
     "opacity": 0.6, "scope": "patient_level", "basis": "measured"},
]
NOT_REGISTERED = {"status": "not_registered", "fixed_scan_id": None, "method": None, "available_in": "M5"}


def _labels() -> dict:
    root = Path(os.environ.get("RB_CONTRACTS_DIR") or API.parent / "contracts")
    return json.loads((root / "labels.json").read_text(encoding="utf-8"))


def _ready(nbytes: int, sha: str, basis: str = "measured") -> dict:
    return {"status": "ready", "bytes": nbytes, "sha256": sha, "basis": basis, "available_in": None}


def _pending(milestone: str, basis: str = "pending") -> dict:
    return {"status": "pending", "bytes": None, "sha256": None, "basis": basis, "available_in": milestone}


def _computed_files(with_context: bool) -> dict:
    """Assets the pipeline computes (body mask, strain field, meshes): pending until M5."""
    files = {"mask_body.expert.nii.gz": _pending("M5"),
             "strain_min_principal_ue.expert.nii.gz": _pending("M5"),
             "strain_min_principal_ue.expert.mz3": _pending("M5"),
             "vertebra.expert.stl": _pending("M5")}
    if with_context:
        files["context_T11-L5.mz3"] = _pending("M5")
    return files


def scans() -> list[dict]:
    lab = _labels()
    masks = {"vertebra": "expert", "body": None, "lesion": "expert", "body_split_method": None,
             "caption": lab["viewer"]["lesion_outline_caption"]}
    d273_files = {
        # L1_ct_1mm_20160906.nii.gz / L1_mask_1mm_20160906.nii.gz / L1_lesion_1mm_20160906.nii.gz (RB_DATA_DIR)
        "ct.nii.gz": _ready(1111145, "4ca90d8bb7dc061418def973dea8d04933c70dfd39ac01207f708a13a98468f1"),
        "mask_vertebra.expert.nii.gz": _ready(8367, "b22e99cd5dda3939f74779429f9206e73c241057a6b3ba23b3a8f9f327633f1d"),
        "mask_lesion.expert.nii.gz": _ready(2375, "163103d9f49ec3af765b94adf563a83c258ecfd46aab34512534fe1b343dae38"),
        **_computed_files(with_context=True),
    }
    d0_files = {  # spike R0's Day-0 crop, bundled by S2 in demo/myel018-L1-d0 (demo/PROVENANCE.json)
        "ct.nii.gz": _ready(1120634, "544fe5a2ba2413c8439bf9d8665f9ce9de26314b3b9622bddf54fd5afa1851e4"),
        "mask_vertebra.expert.nii.gz": _ready(8563, "748d247cce07218c847014d4062efae451459f52f3bc67b8a06d9d939d5cab4c"),
        "mask_lesion.expert.nii.gz": _ready(943, "18bab9992d887d882e900fe197473f6402babd1e269771c21e27bd107207174f"),
        **_computed_files(with_context=False),
    }
    common = {"patient_id": PATIENT, "level": "L1", "provenance": "real_scan", "generator_version": None,
              "anchor_scan_ids": [], "seed": None, "kvp": 120.0, "calibration": None,
              "registration": NOT_REGISTERED, "mask_sources": masks, "color_scales": CT_SCALES}
    return [
        {**common, "scan_id": "myel018-L1-d0", "acquisition_id": "myel018-a", "day": 0, "label": "Day 0",
         "series_instance_uid": "1.3.6.1.4.1.14519.5.2.1.163416561557440954759359029287186276821",
         "series_description": "Myel_018_a_konv", "source_shape": [512, 512, 1021],
         "source_spacing_mm": [0.883, 0.883, 0.6],
         "grid": {"shape": [95, 110, 80], "spacing_mm": [1.0, 1.0, 1.0],
                  "affine": [[-1.0, 0.0, 0.0, 50.186], [0.0, -1.0, 0.0, -131.024], [0.0, 0.0, 1.0, -641.44],
                             [0.0, 0.0, 0.0, 1.0]], "margin_mm": 10.0, "dtype": "int16"},
         "files": d0_files,
         "source": "demo/myel018-L1-d0"},
        {**common, "scan_id": "myel018-L1-d273", "acquisition_id": "myel018-b", "day": 273, "label": "Day 273",
         "series_instance_uid": "1.3.6.1.4.1.14519.5.2.1.214314532294190421148158394502115174580",
         "series_description": "Myel_018_b_konv", "source_shape": [512, 512, 1035],
         "source_spacing_mm": [0.977, 0.977, 0.6],
         "grid": {"shape": [95, 109, 80], "spacing_mm": [1.0, 1.0, 1.0],
                  "affine": [[-1.0, 0.0, 0.0, 62.875], [0.0, -1.0, 0.0, -122.744], [0.0, 0.0, 1.0, -568.673],
                             [0.0, 0.0, 0.0, 1.0]], "margin_mm": 10.0, "dtype": "int16"},
         "files": d273_files, "source": "demo/myel018-L1-d273"},
        {"scan_id": "phantom", "patient_id": None, "acquisition_id": "phantom", "level": None, "day": None,
         "label": "Synthetic phantom", "provenance": "synthetic_insilico", "generator_version": "phantom-femur-shaft/1",
         "anchor_scan_ids": [], "seed": 0, "series_instance_uid": None,
         "series_description": "Synthetic femur-shaft phantom with a lytic defect", "kvp": None,
         "source_shape": None, "source_spacing_mm": None,
         "grid": {"shape": [96, 96, 128], "spacing_mm": [1.0, 1.0, 1.0],
                  "affine": [[1.0, 0.0, 0.0, -48.0], [0.0, 1.0, 0.0, -48.0], [0.0, 0.0, 1.0, -64.0],
                             [0.0, 0.0, 0.0, 1.0]], "margin_mm": None, "dtype": "int16"},
         "files": {  # demo/phantom (demo/PROVENANCE.json): synthetic CT and a mock strain field
             "ct.nii.gz": _ready(1321186, "9c035f24c27be8c04dfb1acc5056c69ff688314ed7074506fd20e79c0652aea5", "mock"),
             "strain_min_principal_ue.mock.nii.gz":
                 _ready(48721, "52e928239f966ffd90d8112a68527436a1dbdcc3ded23198e8bd86d452bed177", "mock"),
             "strain_min_principal_ue.mock.mz3":
                 _ready(227072, "8d6d3c3daec21545915afcd0e3507ce6b5951d63a3916345817ea7d6b09e8d09", "mock")},
         "calibration": None,
         "registration": {"status": "not_registered", "fixed_scan_id": None, "method": None, "available_in": None},
         "mask_sources": {"vertebra": None, "body": None, "lesion": None, "body_split_method": None,
                          "caption": lab["viewer"]["phantom_label"]},
         "color_scales": [{**CT_SCALES[0], "scope": "scan", "basis": "mock"},
                          {"layer": "strain_min_principal_ue", "colormap": "turbo", "cal_min": 0.0, "cal_max": 1500.0,
                           "unit": "ue", "display": "magnitude", "opacity": 0.8, "scope": "scan", "basis": "mock"}],
         "source": "demo/phantom"},
    ]


def metric_rows() -> list[dict]:
    rows = []
    for level, (les0, vert0, les273, vert273) in LADDER_CM3.items():
        for day, les, vert in ((0, les0, vert0), (273, les273, vert273)):
            rows.append({"day": day, "site": level, "scan_id": f"myel018-L1-d{day}" if level == "L1" else None,
                         "lesion_grid": "original", "lesion_volume_mm3": round(les * 1000.0, 3),
                         "vertebra_volume_mm3": round(vert * 1000.0, 3)})
    rows.append({"day": 273, "site": "L1", "scan_id": "myel018-L1-d273", "lesion_grid": "crop_1mm",
                 "lesion_volume_mm3": L1_D273_CROP_MM3["lesion"], "vertebra_volume_mm3": L1_D273_CROP_MM3["vertebra"]})
    rows.append({"day": 0, "site": "L1", "scan_id": "myel018-L1-d0", "lesion_grid": "crop_1mm",
                 "lesion_volume_mm3": L1_D0_CROP_MM3["lesion"], "vertebra_volume_mm3": L1_D0_CROP_MM3["vertebra"]})
    return rows


def seed_sha256() -> str:
    """Hash of everything this seed writes: unchanged hash, nothing to write."""
    data = {"version": SEED_VERSION, "scans": scans(), "metrics": metric_rows(), "patient": PATIENT}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def seed_detail(scan: dict) -> dict:
    """What the audit chain records for one seeded scan (C1 AuditEntry.detail)."""
    detail = {"assets": {n: f["sha256"] for n, f in scan["files"].items() if f["status"] == "ready"},
              "source": scan["source"], "provenance": scan["provenance"], "seed_sha256": seed_sha256()}
    if scan["provenance"] == "real_scan":
        detail["measured_from"] = (f"{SOURCE_TCIA}; volumes {PER_VERTEBRA} sha256 {PER_VERTEBRA_SHA256}; "
                                   f"metadata {SUMMARY} sha256 {SUMMARY_SHA256}; seed {SEED_VERSION}")
    else:
        detail["measured_from"] = "synthetic phantom from our own generator; mock strain field"
    return detail


def seed(conninfo: str) -> dict:
    """Upsert the demo rows. Returns {"scans": n, "metric_rows": n, "audit_appended": [scan_id...]}."""
    appended: list[str] = []
    digest = seed_sha256()
    with psycopg.connect(conninfo, autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('rb_seed_demo', 0))")
            done = {r["scan_id"] for r in conn.execute(
                """SELECT DISTINCT ON (chain_key) scan_id, detail ->> 'seed_sha256' AS sha FROM audit_log
                    WHERE action = 'seed_demo' ORDER BY chain_key, id DESC""").fetchall() if r["sha"] == digest}
            if done >= {s["scan_id"] for s in scans()}:
                return {"scans": 0, "metric_rows": 0, "audit_appended": [], "unchanged": True}
            conn.execute(
                """INSERT INTO patients (patient_id, label, dataset, dataset_case, cohort, focus_level)
                   VALUES (%s, 'Demo patient A', %s, 'Myel_018', 'demo', 'L1')
                   ON CONFLICT (patient_id) DO UPDATE SET label = EXCLUDED.label, dataset = EXCLUDED.dataset,
                       dataset_case = EXCLUDED.dataset_case, cohort = EXCLUDED.cohort,
                       focus_level = EXCLUDED.focus_level""", (PATIENT, DATASET))
            for s in scans():
                t = DAY0 + timedelta(days=s["day"]) if s["day"] is not None else None
                conn.execute(
                    """INSERT INTO scans (scan_id, patient_id, acquisition_id, level, day, time, label, provenance,
                                          generator_version, anchor_scan_ids, seed, series_instance_uid,
                                          series_description, kvp, source_shape, source_spacing_mm, grid, files,
                                          calibration, registration, mask_sources, color_scales)
                       VALUES (%(scan_id)s, %(patient_id)s, %(acquisition_id)s, %(level)s, %(day)s, %(time)s,
                               %(label)s, %(provenance)s, %(generator_version)s, %(anchor_scan_ids)s, %(seed)s,
                               %(series_instance_uid)s, %(series_description)s, %(kvp)s, %(source_shape)s,
                               %(source_spacing_mm)s, %(grid)s::jsonb, %(files)s::jsonb, %(calibration)s::jsonb,
                               %(registration)s::jsonb, %(mask_sources)s::jsonb, %(color_scales)s::jsonb)
                       ON CONFLICT (scan_id) DO UPDATE SET
                         patient_id = EXCLUDED.patient_id, acquisition_id = EXCLUDED.acquisition_id,
                         level = EXCLUDED.level, day = EXCLUDED.day, time = EXCLUDED.time, label = EXCLUDED.label,
                         provenance = EXCLUDED.provenance, generator_version = EXCLUDED.generator_version,
                         anchor_scan_ids = EXCLUDED.anchor_scan_ids, seed = EXCLUDED.seed,
                         series_instance_uid = EXCLUDED.series_instance_uid,
                         series_description = EXCLUDED.series_description, kvp = EXCLUDED.kvp,
                         source_shape = EXCLUDED.source_shape, source_spacing_mm = EXCLUDED.source_spacing_mm,
                         grid = coalesce(EXCLUDED.grid, scans.grid),
                         files = EXCLUDED.files || coalesce((SELECT jsonb_object_agg(k, v) FROM jsonb_each(scans.files)
                                                              AS e(k, v) WHERE v ->> 'status' = 'ready'), '{}'::jsonb),
                         calibration = coalesce(scans.calibration, EXCLUDED.calibration),
                         registration = CASE WHEN scans.registration ->> 'status' = 'registered'
                                             THEN scans.registration ELSE EXCLUDED.registration END,
                         mask_sources = CASE WHEN scans.mask_sources ->> 'body' IS NOT NULL
                                             THEN scans.mask_sources ELSE EXCLUDED.mask_sources END,
                         color_scales = EXCLUDED.color_scales""",
                    {**s, "time": t, "anchor_scan_ids": s["anchor_scan_ids"],
                     "grid": json.dumps(s["grid"]) if s["grid"] is not None else None,
                     "files": json.dumps(s["files"]),
                     "calibration": json.dumps(s["calibration"]) if s["calibration"] is not None else None,
                     "registration": json.dumps(s["registration"]), "mask_sources": json.dumps(s["mask_sources"]),
                     "color_scales": json.dumps(s["color_scales"])})
            conn.execute("DELETE FROM scan_metrics WHERE patient_id = %s AND solver_version LIKE 'seed_demo/%%'",
                         (PATIENT,))
            rows = metric_rows()
            for r in rows:
                conn.execute(
                    """INSERT INTO scan_metrics (time, patient_id, scan_id, site, provenance, mask_source,
                                                 lesion_grid, lesion_volume_mm3, vertebra_volume_mm3, solver_version)
                       VALUES (%s, %s, %s, %s, 'real_scan', 'expert', %s, %s, %s, %s)""",
                    (DAY0 + timedelta(days=r["day"]), PATIENT, r["scan_id"], r["site"], r["lesion_grid"],
                     r["lesion_volume_mm3"], r["vertebra_volume_mm3"], SEED_VERSION))
            for s in scans():
                detail = seed_detail(s)
                last = conn.execute(
                    """SELECT detail FROM audit_log WHERE chain_key = %s AND action = 'seed_demo'
                        ORDER BY id DESC LIMIT 1""", (audit.chain_key(s["scan_id"]),)).fetchone()
                if last is not None and last["detail"] == json.loads(json.dumps(detail)):
                    continue
                _append_sync(conn, s, detail)
                appended.append(s["scan_id"])
        _refresh_aggregates(conn, rows)   # after the commit: the refresh procedure cannot run in a transaction
    return {"scans": len(scans()), "metric_rows": len(rows), "audit_appended": appended, "unchanged": False}


def _refresh_aggregates(conn, rows: list[dict]) -> list[str]:
    """Materialize the seeded bucket range (contracts/db.md: refresh a scan's bucket range after writing it).

    Without it a later refresh of a newer range moves the aggregate's watermark past the seeded Day-0 rows and
    they drop out of scan_metrics_monthly, although scan_metrics still holds them. TimescaleDB only.
    """
    ext = conn.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') AS ok").fetchone()
    if not ext["ok"]:
        return []
    ht = conn.execute("SELECT EXISTS (SELECT 1 FROM timescaledb_information.hypertables "
                      "WHERE hypertable_name = 'scan_metrics') AS ok").fetchone()
    if not ht["ok"]:
        return []
    days = [r["day"] for r in rows]
    start, end = DAY0 + timedelta(days=min(days)), DAY0 + timedelta(days=max(days))
    done = []
    for name in tiger.CAGGS:
        lo, hi = tiger.bucket_range(name, start, end)
        conn.execute("CALL refresh_continuous_aggregate(%s::regclass, %s::timestamptz, %s::timestamptz)",
                     (name, lo, hi))
        done.append(name)
    return done


def _append_sync(conn, scan: dict, detail: dict) -> str:
    """audit.append for a synchronous connection (same payload and hash)."""
    key = audit.chain_key(scan["scan_id"])
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
    row = conn.execute("SELECT hash FROM audit_log WHERE chain_key = %s ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    prev = row["hash"] if row else audit.GENESIS
    at = conn.execute("SELECT clock_timestamp() AS at").fetchone()["at"]
    payload = audit.entry_payload(chain_key=key, at=at, actor="seed", action="seed_demo",
                                  patient_id=scan["patient_id"], scan_id=scan["scan_id"], run_id=None, detail=detail)
    h = audit.compute_hash(prev, payload)
    conn.execute(
        """INSERT INTO audit_log (chain_key, patient_id, scan_id, run_id, at, actor, action, detail, prev_hash, hash)
           VALUES (%s, %s, %s, NULL, %s, 'seed', 'seed_demo', %s::jsonb, %s, %s)""",
        (key, scan["patient_id"], scan["scan_id"], at, json.dumps(payload["detail"]), prev, h))
    return h


def main() -> int:
    url = os.environ.get("RB_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        print("RB_DATABASE_URL is not set", file=sys.stderr)
        return 2
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.config import schema_writes_allowed
    if not schema_writes_allowed(url):
        print("refusing to seed a database that is not on this machine (Tiger Cloud only after G1, "
              "with RB_ALLOW_REMOTE_SCHEMA=1)", file=sys.stderr)
        return 2
    print(json.dumps(seed(url)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
