"""Registration and the labelled synthetic series (S2). C7 signatures only until G1."""
from __future__ import annotations

from typing import Literal

from .. import Crop, Transform, not_before_g1

__all__ = ["register", "synth_series"]


def register(moving: Crop, fixed: Crop) -> Transform:
    """Rigid registration driven by the vertebra masks."""
    raise not_before_g1("register")


def synth_series(scan_a: Crop, scan_b: Crop, days: list[int], volume_curve: Literal["linear"] = "linear",
                 seed: int = 0, generator_version: str = "insilico/1") -> list[Crop]:
    """Every output is provenance synthetic_insilico; no projection rows."""
    raise not_before_g1("synth_series")
