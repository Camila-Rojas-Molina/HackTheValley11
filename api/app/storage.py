"""Asset files: where they live, which names are allowed, and the upload size cap.

An asset of scan S named N (contracts/formats.md) is read from <RB_STORAGE_DIR>/S/N (analysis outputs) or
from <repo>/demo/S/N (the committed demo bundle), in that order. Names and scan ids must match the C1
patterns, and the resolved path must stay inside its root, so `../` or a symlink out of the tree is a 404.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import AsyncIterator, Optional

from .schemas import ASSET_NAME_PATTERN, SCAN_ID_PATTERN

_NAME = re.compile(ASSET_NAME_PATTERN)
_SCAN = re.compile(SCAN_ID_PATTERN)

MEDIA_TYPES = (  # contracts/formats.md "Media types"
    (".nii.gz", "application/gzip"),
    (".mz3", "application/octet-stream"),
    (".vtp", "application/xml"),
    (".vti", "application/xml"),
    (".stl", "model/stl"),
    (".json", "application/json"),
)


class TooLarge(Exception):
    """The request body is over RB_MAX_UPLOAD_MB."""


def valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name)) and ".." not in name and not name.endswith(".vtu")


def valid_scan_id(scan_id: str) -> bool:
    return bool(_SCAN.fullmatch(scan_id))


def media_type(name: str) -> Optional[str]:
    for ext, mt in MEDIA_TYPES:
        if name.endswith(ext):
            return mt
    return None


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def asset_path(roots: list[Path], scan_id: str, name: str) -> Optional[Path]:
    """The first existing file for (scan_id, name) under the roots, or None. Never a path outside a root."""
    if not (valid_scan_id(scan_id) and valid_name(name) and media_type(name)):
        return None
    for root in roots:
        p = root / scan_id / name
        if _inside(root, p) and p.is_file():
            return p.resolve()
    return None


async def read_capped(chunks: AsyncIterator[bytes], cap_bytes: int) -> int:
    """Count a streamed body, raising TooLarge as soon as it passes cap_bytes. Returns the byte count."""
    n = 0
    async for chunk in chunks:
        n += len(chunk)
        if n > cap_bytes:
            raise TooLarge()
    return n
