"""tests/repo/contracts imports the contract builder (scripts/), the schemas (api/) and the pipeline types."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for p in (REPO / "scripts", REPO / "pipeline", REPO / "api"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
