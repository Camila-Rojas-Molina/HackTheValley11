"""The deterministic last tier of the chain (C4): labels.json ai.templates rendered from the facts.

Always available, 0 ms, and correct by construction: every number it prints is a fact, formatted at
display precision, so it passes check() for any facts. Values: every Facts field by name (floats with 2
decimals), plus the short names the labels use: level, day_a, day_b, vol_a, vol_b, pct_vert.

While the stiffness numbers are null (until M5) the `<task>_lesion_only` template is used. Facts with a
stiffness number need a `<task>_full` template in labels.json; until it exists, TemplateMissing is raised
rather than printing "pending" next to a computed number.
"""
from __future__ import annotations

from typing import Any, Optional

from app.schemas import Facts, GeneratedText

from .contract import TASKS, ai_labels, numbers_in
from .providers.base import Result

PROMPT_VERSION = "template/1"


class TemplateMissing(LookupError):
    """labels.json has no template for this task and these facts."""


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def template_values(facts: Facts) -> dict[str, str]:
    data = facts.model_dump()
    values = {k: _fmt(v) for k, v in data.items() if v is not None}
    values.update({
        "level": facts.level,
        "day_a": str(facts.day_a),
        "day_b": str(facts.day_b),
        "vol_a": _fmt(facts.lesion_volume_a_cm3),
        "vol_b": _fmt(facts.lesion_volume_b_cm3),
        "pct_vert": _fmt(facts.lesion_pct_of_vertebra_b),
    })
    return values


def template_key(task: str, facts: Facts) -> str:
    if task not in TASKS:
        raise ValueError(f"unknown task {task!r}")
    lesion_only = facts.stiffness_change_axial_pct is None and facts.lesion_share_pts is None
    return f"{task}_lesion_only" if lesion_only else f"{task}_full"


def template(task: str, facts: Facts) -> GeneratedText:
    key = template_key(task, facts)
    tpl = ai_labels()["templates"].get(key)
    if tpl is None:
        raise TemplateMissing(f"labels.json ai.templates has no {key!r}")
    values = template_values(facts)
    headline = tpl["headline"].format(**values)
    detail = tpl["detail"].format(**values)
    return GeneratedText(headline=headline, detail=detail,
                         numbers_used=sorted(numbers_in(f"{headline}\n{detail}")))


class TemplateProvider:
    """The template as a chain tier, for status pages. The chain calls template() directly."""
    name = "template"
    model: Optional[str] = None
    timeout = 0.0

    def status(self) -> dict:
        return {"name": self.name, "model": None, "configured": True, "key": None, "available": True,
                "reason": None}

    async def generate(self, messages: list[dict], schema: Optional[dict] = None,
                       tools: Optional[list[dict]] = None, image: Optional[bytes] = None,
                       timeout: float = 0.0) -> Result:
        raise NotImplementedError("the chain renders the template with template(task, facts)")
