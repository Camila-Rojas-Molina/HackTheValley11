"""Ingest and crop (S2). C7 signatures only: typed stubs until G1 (contracts/pipeline.md)."""
from __future__ import annotations

from pathlib import Path

from .. import Calibration, Crop, Level, Masks, ScanFiles, not_before_g1

__all__ = ["ingest", "crop"]


def ingest(series_uids: list[str], out_dir: Path) -> ScanFiles:
    """IDC via idc-index; DICOM -> NIfTI with SimpleITK; SEG -> label maps (highdicom, combine_segments=True)."""
    raise not_before_g1("ingest")


def crop(ct: Path, masks: Masks, level: Level, calibration: Calibration | None, lesion: Path | None = None) -> Crop:
    """1 mm, 10 mm margin, masks resampled by physical coordinates. ImplantRegionError for Myel_018 T1-T6."""
    raise not_before_g1("crop")
