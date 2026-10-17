"""The Care-Team Copilot (C4 "Copilot"; live in M8). POST /api/v1/copilot answers 501 until then.

M8 runs our own loop over tools.py: at most copilot.max_rounds rounds and copilot.max_output_tokens,
every number must appear in this turn's tool results, citing a simulated or filled point requires the
word "simulated", out-of-scope questions get copilot.refusal with no tools, and with every provider down
the copilot.suggested_questions map to the same tools plus copilot.template_answers.
"""
from __future__ import annotations

from app.schemas import CopilotAnswer, CopilotRequest

from .chain import Chain

AVAILABLE_IN = "M8"


async def answer(request: CopilotRequest, *, chain: Chain) -> CopilotAnswer:
    """Answer one question for the session's patient (request.patient_id is bound by the server)."""
    raise NotImplementedError(f"the copilot lands in {AVAILABLE_IN}")
