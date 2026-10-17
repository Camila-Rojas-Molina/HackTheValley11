"""Export (S3): strain volumes, .mz3, .vtp, .vti, .stl and metrics.json named per contracts/formats.md.
C7 signature only until G1."""
from __future__ import annotations

from pathlib import Path

from .. import Crop, RunResult, not_before_g1

__all__ = ["export"]


def export(result: RunResult, crop: Crop, out_dir: Path) -> dict[str, Path]:
    """Writes the formats.md files for one run; STL normals outward, watertight."""
    raise not_before_g1("export")
