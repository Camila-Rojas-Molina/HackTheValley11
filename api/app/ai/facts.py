"""Facts: the only input any model sees (C4, contracts/ai-interface.md "Facts").

Numbers our code computed, rounded to display precision (2 decimals), plus enums. No names, DICOM tags,
dates or free text. Derived values (interval_days, lesion_volume_change_cm3, lesion_pct_of_vertebra_b)
are computed here, from the rounded inputs, so every number a text can show agrees with the others:
a model given two days and no interval wrote "over four months" for 94 days (research ai.md §14).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from app.schemas import Facts

DISPLAY_DECIMALS = 2


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(float(v), DISPLAY_DECIMALS)


def build_facts(*, scan_id: str, level: str, day_a: int, day_b: int,
                lesion_volume_a_cm3: float, lesion_volume_b_cm3: float, lesion_grid: str,
                vertebra_volume_b_cm3: float, mask_source: str, provenance: str,
                lesion_source: str = "expert",
                stiffness_change_axial_pct: Optional[float] = None,
                lesion_share_pts: Optional[float] = None,
                background_change_pct: Optional[float] = None,
                calib_low_pct: Optional[float] = None,
                calib_high_pct: Optional[float] = None,
                strain_ratio: Optional[float] = None) -> Facts:
    """Facts for one scan pair (baseline Day day_a, follow-up Day day_b). Stiffness values stay null until
    the physics computes them (M5); the template then reads them as pending."""
    if day_b < day_a:
        raise ValueError("day_b must not be before day_a")
    vol_a, vol_b, vert_b = _r(lesion_volume_a_cm3), _r(lesion_volume_b_cm3), _r(vertebra_volume_b_cm3)
    if vert_b is None or vert_b <= 0:
        raise ValueError("vertebra_volume_b_cm3 must be positive")
    return Facts(
        scan_id=scan_id,
        level=level,
        day_a=int(day_a),
        day_b=int(day_b),
        interval_days=int(day_b) - int(day_a),
        lesion_volume_a_cm3=vol_a,
        lesion_volume_b_cm3=vol_b,
        lesion_volume_change_cm3=_r(vol_b - vol_a),
        lesion_grid=lesion_grid,
        vertebra_volume_b_cm3=vert_b,
        lesion_pct_of_vertebra_b=_r(100.0 * vol_b / vert_b),
        stiffness_change_axial_pct=_r(stiffness_change_axial_pct),
        lesion_share_pts=_r(lesion_share_pts),
        background_change_pct=_r(background_change_pct),
        calib_low_pct=_r(calib_low_pct),
        calib_high_pct=_r(calib_high_pct),
        strain_ratio=_r(strain_ratio),
        mask_source=mask_source,
        lesion_source=lesion_source,
        provenance=provenance,
        simulated=provenance != "real_scan",
    )


def facts_json(facts: Facts | dict[str, Any]) -> str:
    """Canonical JSON of the facts: what a model receives and what facts_sha256 hashes."""
    data = facts if isinstance(facts, dict) else facts.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def facts_sha256(facts: Facts | dict[str, Any]) -> str:
    return hashlib.sha256(facts_json(facts).encode("utf-8")).hexdigest()
