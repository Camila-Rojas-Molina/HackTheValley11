"""The Bone Finder (S2): TotalSegmentator vertebra and vertebral-body masks. C7 signature only until G1."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from .. import Level, Masks, not_before_g1

__all__ = ["segment"]


def segment(ct: Path, levels: list[Level], device: Literal["cpu", "mps", "cuda"] = "cpu") -> dict[Level, Masks]:
    """TotalSegmentator `total --roi_subset vertebrae_*` + `vertebrae_body` at 1.5 mm. Finds bone, never lesions."""
    raise not_before_g1("segment")
