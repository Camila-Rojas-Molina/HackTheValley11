"""Read models: C1 payloads built from the database (patients, scans, scan_metrics, alert_feedback).

The manifest of a scan is its `scans` row plus its `scan_metrics` rows (contracts/db.md). Which metric tiles
a manifest shows, in which order, and the milestone each pending one lands in, is the fixed catalogue
SLOTS below: a slot with a stored value is served with it; a slot without one is `pending` with
`available_in`. Volumes are stored in mm3 and served in cm3 (/ 1000, 2 decimals).

The care-team flag (labels.json `flag`, PLAN 6.6) is computed here by code from the two latest real scans of
the patient's focus level; simulated scans never flag.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Optional

from .contract_fixtures import contracts_dir, labels_version
from .schemas import API_PREFIX

LEVELS = ("T11", "T12", "L1", "L2", "L3", "L4", "L5")
SITES = LEVELS + ("left_femur", "right_femur")
DAY0 = datetime(2026, 1, 17, tzinfo=timezone.utc)   # demo calendar (contracts/db.md): time = Day 0 + day
CALIBRATION_AVAILABLE_IN = "M5"

ASSET_KIND_ORDER = ("ct", "mask_vertebra", "mask_lesion", "mask_body", "strain_volume", "strain_surface",
                    "context_mesh", "surface_vtp", "volume_vti", "stl", "metrics_json")
VARIANT_ORDER = {None: 0, "expert": 1, "ai": 2, "mock": 3}
STRAIN_FIELDS = ("strain_min_principal_ue", "strain_max_principal_ue", "vonmises_mpa", "rigidity_ratio")


@lru_cache(maxsize=2)
def _labels(root: str) -> dict:
    return json.loads((contracts_dir() / "labels.json").read_text(encoding="utf-8"))


def labels() -> dict:
    return _labels(str(contracts_dir()))


def time_of(day: int) -> datetime:
    return DAY0 + timedelta(days=int(day))


def day_of(t: datetime) -> int:
    return (t.astimezone(timezone.utc) - DAY0).days


def cm3(mm3: Optional[float]) -> Optional[float]:
    return None if mm3 is None else round(float(mm3) / 1000.0, 2)


# ------------------------------------------------------------------------------------------ assets
def describe_asset(name: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(kind, variant, field) from a file name (contracts/formats.md "File names"); kind None if unknown."""
    if name == "ct.nii.gz":
        return "ct", None, None
    if name == "metrics.json":
        return "metrics_json", None, None
    if name.startswith("context_") and name.endswith(".mz3"):
        return "context_mesh", None, None
    parts = name.split(".")
    head = parts[0]
    variant = parts[1] if len(parts) > 2 else None
    if head in ("mask_vertebra", "mask_body", "mask_lesion") and name.endswith(".nii.gz"):
        return head, variant, None
    if head in STRAIN_FIELDS:
        if name.endswith(".nii.gz"):
            return "strain_volume", variant, head
        if name.endswith(".mz3"):
            return "strain_surface", variant, head
    if head == "surface" and name.endswith(".vtp"):
        return "surface_vtp", variant, None
    if head == "fields" and name.endswith(".vti"):
        return "volume_vti", variant, None
    if head == "vertebra" and name.endswith(".stl"):
        return "stl", variant, None
    return None, None, None


def assets_of(scan: dict) -> list[dict]:
    from .storage import media_type
    out = []
    for name, f in (scan.get("files") or {}).items():
        kind, variant, field = describe_asset(name)
        if kind is None:
            continue
        status = f.get("status", "pending")
        out.append({
            "name": name, "kind": kind, "url": f"{API_PREFIX}/scans/{scan['scan_id']}/assets/{name}",
            "media_type": media_type(name), "variant": variant, "field": field,
            "basis": f.get("basis") or ("measured" if status == "ready" else "pending"),
            "provenance": scan["provenance"], "status": status,
            "available_in": None if status == "ready" else f.get("available_in"),
            "bytes": f.get("bytes") if status == "ready" else None,
            "sha256": f.get("sha256") if status == "ready" else None,
        })
    out.sort(key=lambda a: (ASSET_KIND_ORDER.index(a["kind"]), VARIANT_ORDER.get(a["variant"], 9), a["name"]))
    return out


# ------------------------------------------------------------------------------------------ metric slots
@dataclass(frozen=True)
class Slot:
    key: str
    unit: str
    roles: str                      # "B" baseline, "F" follow-up, "BF" both
    pending_in: str
    grid: Optional[str] = None
    mask_source: Optional[str] = "expert"
    lesion_model: Optional[str] = None
    statistic: Optional[str] = None
    compared: bool = False
    basis: str = "measured"         # basis once computed: measured (our code on real voxels) or simulation (FE)


SLOTS: tuple[Slot, ...] = (
    Slot("lesion_volume_cm3", "cm3", "BF", "M3", grid="original"),
    Slot("lesion_volume_cm3", "cm3", "BF", "M3", grid="crop_1mm"),
    Slot("vertebra_volume_cm3", "cm3", "BF", "M3", grid="original"),
    Slot("vertebra_volume_cm3", "cm3", "BF", "M3", grid="crop_1mm"),
    Slot("lesion_pct_of_vertebra", "pct", "BF", "M3", grid="original"),
    Slot("body_volume_cm3", "cm3", "BF", "M5", grid="crop_1mm"),
    Slot("lesion_pct_of_body", "pct", "F", "M5", grid="crop_1mm"),
    Slot("stiffness_change_axial_pct", "pct", "F", "M5", statistic="central80_mean", compared=True),
    Slot("lesion_share_pts", "pts", "F", "M5", statistic="central80_mean", compared=True),
    Slot("background_change_pct", "pct", "F", "M5", statistic="central80_mean", compared=True),
    Slot("stiffness_change_axial_pct", "pct", "F", "M5", mask_source="ai", statistic="central80_mean",
         compared=True),
    Slot("strain_ratio", "ratio", "F", "M5", lesion_model="weak_bone", compared=True, basis="simulation"),
    Slot("modulus_lesion_gpa", "gpa", "F", "M5", lesion_model="weak_bone"),
    Slot("modulus_lesion_gpa", "gpa", "F", "M5", lesion_model="void"),
    Slot("modulus_ref_gpa", "gpa", "F", "M5"),
    Slot("vonmises_p99_mpa", "mpa", "F", "M5", lesion_model="weak_bone", basis="simulation"),
    Slot("strain_p99_lesion_ue", "ue", "B", "M5", lesion_model="weak_bone", basis="simulation"),
    Slot("dice_ai_vs_expert", "dice", "BF", "M5", mask_source=None),
)

# Series metric -> the slot whose value one point carries (whatever the scan's role).
SERIES_SLOTS: dict[str, Slot] = {
    "lesion_volume_cm3": SLOTS[0], "vertebra_volume_cm3": SLOTS[2], "body_volume_cm3": SLOTS[5],
    "stiffness_change_axial_pct": SLOTS[7],
    "stiffness_change_bending_pct": Slot("stiffness_change_bending_pct", "pct", "F", "M5",
                                         statistic="central80_mean", compared=True),
    "lesion_share_pts": SLOTS[8], "background_change_pct": SLOTS[9], "strain_ratio": SLOTS[11],
    "modulus_lesion_gpa": SLOTS[12],
}
_VOLUME_COLUMN = {"lesion_volume_cm3": "lesion_volume_mm3", "vertebra_volume_cm3": "vertebra_volume_mm3",
                  "body_volume_cm3": "body_volume_mm3"}


def _matches(slot: Slot, row: dict) -> bool:
    if slot.mask_source is not None and row["mask_source"] != slot.mask_source:
        return False
    if row["lesion_model"] != slot.lesion_model or row["statistic"] != slot.statistic:
        return False
    if slot.grid is not None and row["lesion_grid"] != slot.grid:
        return False
    return True


def slot_value(slot: Slot, rows: list[dict]) -> tuple[Optional[float], Optional[dict]]:
    """(value served, the row it came from) for the newest matching row that has the value."""
    for row in sorted(rows, key=lambda r: (r["time"], r["created_at"]), reverse=True):
        if not _matches(slot, row):
            continue
        if slot.key in _VOLUME_COLUMN:
            v = cm3(row[_VOLUME_COLUMN[slot.key]])
        elif slot.key == "lesion_pct_of_vertebra":
            le, ve = row["lesion_volume_mm3"], row["vertebra_volume_mm3"]
            v = None if le is None or not ve else round(100.0 * le / ve, 2)
        elif slot.key == "lesion_pct_of_body":
            le, bo = row["lesion_volume_mm3"], row["body_volume_mm3"]
            v = None if le is None or not bo else round(100.0 * le / bo, 2)
        else:
            v = row.get(slot.key)
            v = None if v is None else float(v)
        if v is not None:
            return v, row
    return None, None


def metric(slot: Slot, value: Optional[float], row: Optional[dict], provenance: str,
           compared_to: Optional[str], note: Optional[str] = None) -> dict:
    rng = None
    if value is not None and slot.key == "stiffness_change_axial_pct" and row is not None \
            and row.get("calib_low_pct") is not None and row.get("calib_high_pct") is not None:
        rng = {"low": float(row["calib_low_pct"]), "high": float(row["calib_high_pct"]), "kind": "calibration"}
    return {
        "key": slot.key, "value": value, "unit": slot.unit,
        "basis": slot.basis if value is not None else "pending",
        "provenance": provenance, "available_in": None if value is not None else slot.pending_in,
        "range": rng, "grid": slot.grid, "mask_source": slot.mask_source, "lesion_model": slot.lesion_model,
        "statistic": slot.statistic, "compared_to_scan_id": compared_to if slot.compared else None,
        "note": note,
    }


# ------------------------------------------------------------------------------------------ queries
SCAN_COLUMNS = """s.scan_id, s.patient_id, s.acquisition_id, s.level, s.day, s.time, s.label, s.provenance,
       s.generator_version, s.anchor_scan_ids, s.seed, s.series_instance_uid, s.series_description,
       s.kvp::text::float8 AS kvp, s.source_shape, s.source_spacing_mm::text::float8[] AS source_spacing_mm,
       s.grid, s.files, s.calibration, s.registration, s.mask_sources, s.color_scales, p.dataset_case"""


async def scan_row(conn, scan_id: str) -> Optional[dict]:
    row = await (await conn.execute(
        f"SELECT {SCAN_COLUMNS} FROM scans s LEFT JOIN patients p USING (patient_id) WHERE s.scan_id = %s",
        (scan_id,))).fetchone()
    return dict(row) if row else None


async def patient_scans(conn, patient_id: str, level: Optional[str] = None) -> list[dict]:
    q = f"SELECT {SCAN_COLUMNS} FROM scans s LEFT JOIN patients p USING (patient_id) WHERE s.patient_id = %s"
    params: list[Any] = [patient_id]
    if level is not None:
        q += " AND s.level = %s"
        params.append(level)
    q += " ORDER BY s.day NULLS LAST, s.scan_id"
    return [dict(r) for r in await (await conn.execute(q, params)).fetchall()]


async def metric_rows(conn, *, scan_id: Optional[str] = None, patient_id: Optional[str] = None,
                      site: Optional[str] = None) -> list[dict]:
    if scan_id is not None:
        cur = await conn.execute("SELECT * FROM scan_metrics WHERE scan_id = %s", (scan_id,))
    else:
        cur = await conn.execute("SELECT * FROM scan_metrics WHERE patient_id = %s AND site = %s",
                                 (patient_id, site))
    return [dict(r) for r in await cur.fetchall()]


def _rows_for_scan(scan: dict, rows: list[dict]) -> list[dict]:
    """A scan's metric rows: tagged with its scan_id, or (for ladder rows) same patient, site and time."""
    return [r for r in rows if r["scan_id"] == scan["scan_id"]
            or (r["scan_id"] is None and r["site"] == scan["level"] and scan["time"] is not None
                and r["time"] == scan["time"])]


def baseline_and_followup(scans: list[dict]) -> tuple[Optional[dict], Optional[dict]]:
    real = [s for s in scans if s["provenance"] == "real_scan" and s["day"] is not None]
    if not real:
        return None, None
    return real[0], real[-1]


# ------------------------------------------------------------------------------------------ manifest
async def manifest(conn, scan_id: str) -> Optional[dict]:
    scan = await scan_row(conn, scan_id)
    if scan is None:
        return None
    metrics: list[dict] = []
    if scan["patient_id"] and scan["level"]:
        siblings = await patient_scans(conn, scan["patient_id"], scan["level"])
        base, follow = baseline_and_followup(siblings)
        role = "B" if base is None or base["scan_id"] == scan["scan_id"] or scan["day"] is None \
            or scan["day"] <= base["day"] else "F"
        rows = _rows_for_scan(scan, await metric_rows(conn, patient_id=scan["patient_id"], site=scan["level"]))
        compared_to = base["scan_id"] if (base and role == "F") else None
        for slot in SLOTS:
            if role not in slot.roles:
                continue
            value, row = slot_value(slot, rows)
            note = None
            if slot.key == "lesion_volume_cm3" and slot.grid == "crop_1mm" and value is not None:
                note = f"{round(row['lesion_volume_mm3']):,} voxels at 1 mm"
            if slot.key == "strain_p99_lesion_ue" and follow is not None and follow["scan_id"] != scan["scan_id"]:
                note = f"lesion region of {follow['label']}, mapped onto {scan['label']} after registration"
            metrics.append(metric(slot, value, row, scan["provenance"], compared_to, note))
    real = scan["provenance"] == "real_scan"
    return {
        "schema": "rb.manifest/1", "scan_id": scan["scan_id"], "patient_id": scan["patient_id"],
        "label": scan["label"], "level": scan["level"], "day": scan["day"], "time": scan["time"],
        "provenance": scan["provenance"], "simulated": not real, "generator_version": scan["generator_version"],
        "anchor_scan_ids": list(scan["anchor_scan_ids"] or []), "seed": scan["seed"],
        "acquisition": {
            "acquisition_id": scan["acquisition_id"], "dataset_case": scan["dataset_case"],
            "series_instance_uid": scan["series_instance_uid"], "series_description": scan["series_description"],
            "modality": "CT", "kvp": scan["kvp"], "source_shape": scan["source_shape"],
            "source_spacing_mm": scan["source_spacing_mm"],
        },
        "grid": scan["grid"],
        "registration": scan["registration"],
        "calibration": scan["calibration"],
        "calibration_available_in": CALIBRATION_AVAILABLE_IN if (real and scan["calibration"] is None) else None,
        "mask_sources": scan["mask_sources"],
        "assets": assets_of(scan),
        "color_scales": scan["color_scales"],
        "metrics": metrics,
        "labels_version": labels_version(),
    }


# ------------------------------------------------------------------------------------------ patients and flag
def scan_ref(scan: dict) -> dict:
    files = scan.get("files") or {}
    ready = (files.get("ct.nii.gz") or {}).get("status") == "ready"
    has_strain = any(describe_asset(n)[0] in ("strain_volume", "strain_surface") and f.get("status") == "ready"
                     for n, f in files.items())
    return {"scan_id": scan["scan_id"], "patient_id": scan["patient_id"], "level": scan["level"],
            "day": scan["day"], "time": scan["time"], "label": scan["label"], "provenance": scan["provenance"],
            "simulated": scan["provenance"] != "real_scan", "status": "ready" if ready else "pending",
            "has_strain": has_strain}


def _f2(v: float) -> str:
    return f"{v:.2f}"


def evaluate_flag(level: Optional[str], a: Optional[dict], b: Optional[dict], rows: list[dict]) -> dict:
    """The deterministic flag (labels.json flag.rule) for scans a (earlier) and b (latest) at `level`."""
    lab = labels()["flag"]
    rule, tpl = lab["rule"], lab["templates"]
    G, S = rule["growth_pct_min"], rule["lesion_share_pts_min"]
    vol_slot, share_slot = SLOTS[0], SLOTS[8]
    vol_a = vol_b = share = low = high = None
    if a is not None and b is not None and a["scan_id"] != b["scan_id"]:
        vol_a, _ = slot_value(vol_slot, _rows_for_scan(a, rows))
        vol_b, _ = slot_value(vol_slot, _rows_for_scan(b, rows))
        share, srow = slot_value(share_slot, _rows_for_scan(b, rows))
        if srow is not None:
            low, high = srow.get("calib_low_pct"), srow.get("calib_high_pct")

    change = None
    c1: dict[str, Any] = {"key": "lesion_new_or_grew", "met": None, "value": None, "threshold": None,
                          "unit": "cm3", "basis": "pending", "available_in": vol_slot.pending_in, "detail": None}
    if vol_a is not None and vol_b is not None:
        c1.update(basis="measured", available_in=None)
        if vol_a == 0 and vol_b > 0:
            change = "new"
            c1.update(met=True, value=vol_b,
                      detail=f"new lesion at {level}: {_f2(vol_a)} to {_f2(vol_b)} cm3 (original grid)")
        elif vol_b > vol_a > 0:
            change = "grew"
            growth = round(100.0 * (vol_b - vol_a) / vol_a, 2)
            c1.update(value=growth, unit="pct", threshold=G)
            if G is None:
                c1["detail"] = f"lesion at {level} grew {growth}%; the growth threshold is not set yet"
            else:
                c1["met"] = growth >= G
                c1["detail"] = f"lesion at {level} grew {growth}% (threshold {G}%)"
        else:
            c1.update(met=False, value=vol_b,
                      detail=f"no new or growing lesion at {level} ({_f2(vol_a)} to {_f2(vol_b)} cm3)")

    c2: dict[str, Any] = {"key": "lesion_share_at_least", "met": None, "value": None, "threshold": S,
                          "unit": "pts", "basis": "pending", "available_in": share_slot.pending_in, "detail": None}
    c3: dict[str, Any] = {"key": "share_exceeds_calibration_range", "met": None, "value": None, "threshold": None,
                          "unit": "pts", "basis": "pending", "available_in": share_slot.pending_in, "detail": None}
    if share is not None:
        c2.update(value=share, basis="measured", available_in=None)
        if S is None:
            c2["detail"] = "the share threshold is not set yet"
        else:
            c2["met"] = share >= S
        if low is not None and high is not None:
            width = round(float(high) - float(low), 2)
            c3.update(value=share, threshold=width, basis="measured", available_in=None, met=share > width)

    conds = [c1, c2, c3]
    if all(c["met"] is True for c in conds):
        status = "flagged"
    elif any(c["met"] is False for c in conds):
        status = "not_flagged"
    else:
        status = "pending"

    if status == "flagged":
        line = tpl["flagged"].format(change=tpl["change_words"][change], level=level, vol_a=_f2(vol_a),
                                     vol_b=_f2(vol_b), share=_f2(share))
    elif status == "not_flagged":
        reason = next(c["detail"] or c["key"] for c in conds if c["met"] is False)
        line = tpl["not_flagged"].format(reason=reason)
    elif change is not None:
        pending_in = next((c["available_in"] for c in conds if c["available_in"]), None) or "M5"
        line = tpl["pending"].format(change=tpl["change_words"][change], level=level, vol_a=_f2(vol_a),
                                     vol_b=_f2(vol_b), available_in=pending_in)
    else:
        line = f"Not yet evaluated: {level or 'this patient'} needs two real scans with lesion volumes."

    rng = None
    if low is not None and high is not None:
        rng = {"low": float(low), "high": float(high), "kind": "calibration"}
    return {
        "status": status, "rule_version": rule["version"], "level": level,
        "scan_id": b["scan_id"] if b else None,
        "alert_id": f"al-{b['scan_id']}" if (status == "flagged" and b) else None,
        "conditions": conds, "line": line, "protocol_line": lab["protocol_line"],
        "data_quality": {"mask_source": vol_slot.mask_source if vol_b is not None else None,
                         "lesion_source": "expert" if vol_b is not None else None,
                         "calibration_range": rng, "provenance": b["provenance"] if b else "real_scan",
                         "simulated": bool(b and b["provenance"] != "real_scan")},
    }


async def patient_item(conn, p: dict) -> dict:
    scans = await patient_scans(conn, p["patient_id"])
    latest = max(scans, key=lambda s: (s["day"] if s["day"] is not None else -1,
                                       s["provenance"] == "real_scan")) if scans else None
    focus = [s for s in scans if s["level"] == p["focus_level"]]
    a = b = None
    real = [s for s in focus if s["provenance"] == "real_scan" and s["day"] is not None]
    if len(real) >= 2:
        a, b = real[-2], real[-1]
    elif real:
        b = real[-1]
    rows = await metric_rows(conn, patient_id=p["patient_id"], site=p["focus_level"]) if p["focus_level"] else []
    return {
        "patient_id": p["patient_id"], "label": p["label"], "dataset": p["dataset"],
        "dataset_case": p["dataset_case"], "latest_scan_id": latest["scan_id"] if latest else None,
        "latest_day": latest["day"] if latest else None, "scan_count": len(scans),
        "flag": evaluate_flag(p["focus_level"], a, b, rows),
    }


async def patient_list(conn) -> dict:
    ps = await (await conn.execute(
        "SELECT * FROM patients WHERE cohort = 'demo' ORDER BY patient_id")).fetchall()
    return {"patients": [await patient_item(conn, dict(p)) for p in ps]}


async def patient(conn, patient_id: str) -> Optional[dict]:
    """A demo patient's detail; the synthetic cohort is never shown as a patient."""
    p = await (await conn.execute(
        "SELECT * FROM patients WHERE patient_id = %s AND cohort = 'demo'", (patient_id,))).fetchone()
    return dict(p) if p else None


async def patient_detail(conn, p: dict) -> dict:
    item = await patient_item(conn, p)
    scans = await patient_scans(conn, p["patient_id"])
    present = {r["site"] for r in await (await conn.execute(
        "SELECT DISTINCT site FROM scan_metrics WHERE patient_id = %s", (p["patient_id"],))).fetchall()}
    present |= {s["level"] for s in scans if s["level"]}
    return {**item, "scans": [scan_ref(s) for s in scans], "sites": [s for s in SITES if s in present]}


# ------------------------------------------------------------------------------------------ ladder and series
async def ladder(conn, patient_id: str) -> dict:
    from . import tiger
    rows, raw_ms = await tiger.timed(
        conn, """SELECT * FROM scan_metrics WHERE patient_id = %s AND provenance = 'real_scan'
                  ORDER BY site, time""", (patient_id,))
    timing = await tiger.query_timing(conn, patient_id)
    days = sorted({day_of(r["time"]) for r in rows})
    base_day, follow_day = (days[-2], days[-1]) if len(days) >= 2 else ((days[0], days[0]) if days else (0, 0))
    scans = await patient_scans(conn, patient_id)
    levels = []
    for level in LEVELS:
        lrows = [r for r in rows if r["site"] == level]
        if not lrows:
            continue
        b_rows = [r for r in lrows if day_of(r["time"]) == base_day]
        f_rows = [r for r in lrows if day_of(r["time"]) == follow_day]
        base_scan = next((s for s in scans if s["level"] == level and s["day"] == base_day
                          and s["provenance"] == "real_scan"), None)
        compared_to = base_scan["scan_id"] if base_scan else None
        vols = (SLOTS[0], SLOTS[2])
        levels.append({
            "level": level,
            "baseline": [metric(s, slot_value(s, b_rows)[0], None, "real_scan", None) for s in vols],
            "followup": [metric(s, slot_value(s, f_rows)[0], None, "real_scan", None) for s in vols],
            "change": [metric(s, *slot_value(s, f_rows), "real_scan", compared_to) for s in SLOTS[7:10]],
            "excluded": False, "exclusion_reason": None,
        })
    return {"patient_id": patient_id, "baseline_scan_day": base_day, "followup_scan_day": follow_day,
            "statistic": "central80_mean", "levels": levels, "source": "raw",
            "query_ms": {"raw_ms": raw_ms, "aggregate_ms": timing["aggregate_ms"]}}


async def series(conn, patient_id: str, site: str, metric_key: str) -> Optional[dict]:
    from . import tiger
    slot = SERIES_SLOTS[metric_key]
    rows, ms = await tiger.timed(conn, "SELECT * FROM scan_metrics WHERE patient_id = %s AND site = %s",
                                 (patient_id, site))
    scans = [s for s in await patient_scans(conn, patient_id, site) if s["day"] is not None]
    points = []
    if scans:
        for s in scans:
            value, _ = slot_value(slot, _rows_for_scan(s, rows))
            points.append({"time": s["time"], "day": s["day"], "value": value,
                           "basis": slot.basis if value is not None else "pending", "provenance": s["provenance"],
                           "filled": False, "scan_id": s["scan_id"], "mask_source": slot.mask_source,
                           "lesion_model": slot.lesion_model,
                           "available_in": None if value is not None else slot.pending_in})
    else:
        for t in sorted({r["time"] for r in rows}):
            at = [r for r in rows if r["time"] == t]
            value, row = slot_value(slot, at)
            if value is None:
                continue
            points.append({"time": t, "day": day_of(t), "value": value, "basis": slot.basis,
                           "provenance": row["provenance"], "filled": False, "scan_id": None,
                           "mask_source": slot.mask_source, "lesion_model": slot.lesion_model,
                           "available_in": None})
    if not points:
        return None
    return {"patient_id": patient_id, "site": site, "metric": metric_key, "unit": slot.unit, "points": points,
            "contains_synthetic": any(p["provenance"] != "real_scan" or p["filled"] for p in points),
            "source": "raw", "query_ms": ms}


# ------------------------------------------------------------------------------------------ AI facts
async def facts_for(conn, scan_id: str):
    """Facts (C4) for a follow-up scan against its patient's baseline at the same level, or None."""
    from .ai.facts import build_facts
    scan = await scan_row(conn, scan_id)
    if scan is None or not scan["patient_id"] or scan["level"] not in LEVELS or scan["day"] is None:
        return None
    siblings = [s for s in await patient_scans(conn, scan["patient_id"], scan["level"])
                if s["provenance"] == "real_scan" and s["day"] is not None and s["day"] < scan["day"]]
    if not siblings:
        return None
    base = siblings[0]
    rows = await metric_rows(conn, patient_id=scan["patient_id"], site=scan["level"])
    vol_a, _ = slot_value(SLOTS[0], _rows_for_scan(base, rows))
    b_rows = _rows_for_scan(scan, rows)
    vol_b, _ = slot_value(SLOTS[0], b_rows)
    vert_b, _ = slot_value(SLOTS[2], b_rows)
    if vol_a is None or vol_b is None or not vert_b:
        return None
    x, xrow = slot_value(SLOTS[7], b_rows)
    return build_facts(
        scan_id=scan["scan_id"], level=scan["level"], day_a=base["day"], day_b=scan["day"],
        lesion_volume_a_cm3=vol_a, lesion_volume_b_cm3=vol_b, lesion_grid="original",
        vertebra_volume_b_cm3=vert_b, mask_source="expert", provenance=scan["provenance"],
        stiffness_change_axial_pct=x, lesion_share_pts=slot_value(SLOTS[8], b_rows)[0],
        background_change_pct=slot_value(SLOTS[9], b_rows)[0],
        calib_low_pct=xrow.get("calib_low_pct") if xrow else None,
        calib_high_pct=xrow.get("calib_high_pct") if xrow else None,
        strain_ratio=slot_value(SLOTS[11], b_rows)[0],
    )
