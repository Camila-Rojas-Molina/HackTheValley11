"""The copilot's five read-only tools (C4 "Copilot"; live in M8).

Each tool is fixed parameterised SQL over the patient bound by the server from the UI session; the model
never chooses the patient, and every argument is validated against an enum before any query runs.
Until M8 the specs below are the contract and every call raises NotImplementedError.
"""
from __future__ import annotations

from typing import Any, get_args

from app.schemas import Level, SeriesMetric, Site

AVAILABLE_IN = "M8"

# name -> argument enums (None = free text checked elsewhere, e.g. a scan id from list_scans()).
TOOL_ARGS: dict[str, dict[str, Any]] = {
    "list_scans": {},
    "get_metric_series": {"site": get_args(Site), "metric": get_args(SeriesMetric)},
    "compare_scans": {"scan_a": None, "scan_b": None},
    "get_monthly_trend": {"site": get_args(Site), "metric": get_args(SeriesMetric)},
    "define_term": {"term": "copilot.terms"},   # the enum lives in contracts/labels.json
}
TOOL_NAMES: tuple[str, ...] = tuple(TOOL_ARGS)
LEVELS: tuple[str, ...] = get_args(Level)


async def call_tool(name: str, args: dict[str, Any], *, patient_id: str) -> list[dict]:
    """Run one tool for the session's patient and return its rows (M8)."""
    raise NotImplementedError(f"copilot tools land in {AVAILABLE_IN}")
