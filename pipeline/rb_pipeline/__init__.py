"""RainbowBone pipeline (C7, contracts/pipeline.md): the shared types every stage passes around.

No web, database or AI imports here or in any subpackage (tests/repo/test_boundaries.py). Until Galit's gate
(G1) every stage function is a typed stub that raises NotImplementedError: no segmentation, FE or physics
compute exists in the repo yet. Large inputs come from RB_DATA_DIR, outside the repo; outputs follow
contracts/formats.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

__all__ = [
    "Level", "ScanFiles", "Calibration", "Masks", "Crop", "Transform", "PhysicsParams", "RunResult",
    "CalibrationMissingError", "ImplantRegionError", "TooLargeError", "not_before_g1",
]

Level = Literal["T11", "T12", "L1", "L2", "L3", "L4", "L5"]


@dataclass(frozen=True)
class ScanFiles:
    """One CT acquisition on disk (RB_DATA_DIR)."""
    acquisition_id: str
    ct_path: Path
    seg_vertebrae_path: Path | None
    seg_lesions_path: Path | None
    series_instance_uid: str | None
    spacing_mm: tuple[float, float, float]
    kvp: float | None
    sha256: dict[str, str]


@dataclass(frozen=True)
class Calibration:
    """The calibration block of contracts/formats.md, fitted on the full scan."""
    method: Literal["phantomless_fat_air", "phantomless_air_fat_muscle"]
    slope: float
    intercept: float
    slope_sd: float
    peaks_hu: dict[str, float | None]
    reference: dict[str, float]
    reference_unit: str
    roi: str
    fit_r2: float | None
    label: str


@dataclass(frozen=True)
class Masks:
    vertebra: Path
    body: Path | None
    source: Literal["expert", "ai"]
    body_split_method: Literal["vertebrae_body", "geometric_cut"] | None


@dataclass(frozen=True)
class Crop:
    """One level, 1 mm isotropic, 10 mm margin."""
    scan_id: str
    level: Level
    ct: Path
    masks: Masks
    lesion: Path | None
    calibration: Calibration | None
    affine: tuple[tuple[float, ...], ...]
    shape: tuple[int, int, int]


@dataclass(frozen=True)
class Transform:
    """Rigid, moving -> fixed, world mm."""
    matrix: tuple[tuple[float, ...], ...]
    fixed_scan_id: str
    moving_scan_id: str
    metric: float | None


@dataclass(frozen=True)
class PhysicsParams:
    load_n: float = 800.0
    voxel_mm: float = 1.0
    lesion_models: tuple[Literal["weak_bone", "void"], ...] = ("weak_bone", "void")
    statistic: Literal["central80_mean", "weakest_5mm"] = "central80_mean"
    exclusion_band_mm: float = 3.0
    max_dof: int = 1_200_000


@dataclass(frozen=True)
class RunResult:
    metrics: list[dict]
    volumes: dict[str, Path]
    surfaces: dict[str, Path]
    calibration: Calibration
    dof: int
    seconds: float


class CalibrationMissingError(ValueError):
    """run_physics refuses a crop without a calibration block."""


class ImplantRegionError(ValueError):
    """Myel_018 T1-T6: metal implant from C6 to T6; never run FE there."""


class TooLargeError(ValueError):
    """The model would exceed PhysicsParams.max_dof."""


def not_before_g1(function: str) -> NotImplementedError:
    """The error every C7 stub raises until its stage lands after Galit's gate."""
    return NotImplementedError(f"{function}: lands after G1 (M5)")
