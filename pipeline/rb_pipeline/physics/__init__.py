"""Physics (S3): calibration, CTRA, lesion refill, voxel FE. Not AI. C7 signatures only until G1."""
from __future__ import annotations

from pathlib import Path

from .. import Calibration, CalibrationMissingError, Crop, PhysicsParams, RunResult, not_before_g1

__all__ = ["calibrate", "refill_lesion", "run_physics"]


def calibrate(full_ct: Path, body_region: Path | None = None) -> Calibration:
    """Phantomless calibration on the full scan, never the crop."""
    raise not_before_g1("calibrate")


def refill_lesion(crop_b: Crop, crop_a_registered: Crop) -> Crop:
    """Lesion voxels of B get the registered A HU; the counterfactual behind the lesion share."""
    raise not_before_g1("refill_lesion")


def run_physics(crop: Crop, baseline: Crop | None = None, params: PhysicsParams = PhysicsParams()) -> RunResult:
    """Refuses a crop without a calibration block (contract C7), then the FE run itself lands after G1."""
    if crop.calibration is None:
        raise CalibrationMissingError("run_physics: crop has no calibration block")
    raise not_before_g1("run_physics")
