"""The output contract and the checker (C4, contracts/ai-interface.md "Output and the checker").

Every wording list comes from contracts/labels.json (`ai`, `copilot`); this module reads them and never
copies them. check() rejects, in this order:
  1. a `length` finish, or headline + detail over ai.max_chars;
  2. a number that is not in the facts;
  3. a spelled-out quantity (ai.number_words, whole words);
  4. blocked wording (ai.blocked_output_patterns).
The footer is added by code after the check and is not checked.
"""
from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

# A number is a token that does not follow a letter or digit, so L1, T11, cm3 and M5 are not numbers (C4),
# while a number written against a unit (15mm, 2x) still counts and must be in the facts.
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9.])[-−]?\d+(?:\.\d+)?")

# Output schema sent to every model (response_json_schema / strict json_schema), validated again locally.
OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "detail": {"type": "string"},
        "numbers_used": {"type": "array", "items": {"type": "number"}},
    },
    "required": ["headline", "detail", "numbers_used"],
    "additionalProperties": False,
}

READING_GRADE_FORMULA = "flesch_kincaid_grade/1"
TASKS: tuple[str, ...] = ("care_team_summary", "patient_explanation")


# ----------------------------------------------------------------------------------------- labels
def contracts_dir() -> Path:
    """The repo's contracts/ folder: the first ancestor of this file that holds contracts/labels.json."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts"
        if (candidate / "labels.json").is_file():
            return candidate
    raise FileNotFoundError("contracts/labels.json not found in any parent of api/app/ai/")


@functools.lru_cache(maxsize=1)
def labels() -> dict:
    """contracts/labels.json (C6), loaded once."""
    with open(contracts_dir() / "labels.json", encoding="utf-8") as f:
        return json.load(f)


def ai_labels() -> dict:
    return labels()["ai"]


@functools.lru_cache(maxsize=1)
def _blocked() -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in ai_labels()["blocked_output_patterns"])


@functools.lru_cache(maxsize=1)
def _number_words() -> re.Pattern:
    words = sorted(ai_labels()["number_words"], key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)


# ----------------------------------------------------------------------------------------- numbers
def _as_float(token: str) -> float:
    return float(token.replace("−", "-"))


def numbers_in(text: str) -> list[float]:
    """Distinct numeric values in `text`, in order of first appearance."""
    seen: list[float] = []
    for m in NUMBER_RE.finditer(text):
        v = _as_float(m.group(0))
        if v not in seen:
            seen.append(v)
    return seen


def _facts_dict(facts: Any) -> Mapping[str, Any]:
    if isinstance(facts, Mapping):
        return facts
    return facts.model_dump()


def fact_numbers(facts: Any) -> set[float]:
    """Every numeric fact value (bools and nulls are not numbers)."""
    out: set[float] = set()
    for v in _facts_dict(facts).values():
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            out.add(float(v))
    return out


# ----------------------------------------------------------------------------------------- checker
@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: Optional[str]
    numbers_matched: int
    numbers_total: int


def _text_of(output: Any) -> tuple[str, str]:
    if isinstance(output, Mapping):
        return str(output.get("headline") or ""), str(output.get("detail") or "")
    return output.headline, output.detail


def check(output: Any, facts: Any, finish_reason: Optional[str] = None) -> Verdict:
    """Verdict on one generated text (`GeneratedText` or a dict with headline/detail) against the facts."""
    headline, detail = _text_of(output)
    body = f"{headline}\n{detail}"
    found = numbers_in(body)
    allowed = fact_numbers(facts)
    matched = [v for v in found if v in allowed]

    def verdict(reason: Optional[str]) -> Verdict:
        return Verdict(ok=reason is None, reason=reason, numbers_matched=len(matched), numbers_total=len(found))

    max_chars = int(ai_labels()["max_chars"])
    if finish_reason == "length":
        return verdict("cut off at the token limit")
    if len(headline) + len(detail) > max_chars:
        return verdict(f"over {max_chars} characters")
    for m in NUMBER_RE.finditer(body):
        if _as_float(m.group(0)) not in allowed:
            return verdict(f"number not in facts: {m.group(0)}")
    word = _number_words().search(body)
    if word:
        return verdict(f"spelled-out quantity: {word.group(0)!r}")
    for pattern in _blocked():
        hit = pattern.search(body)
        if hit:
            return verdict(f"blocked wording: {hit.group(0)!r}")
    return verdict(None)


def blocked_wording(text: str) -> list[str]:
    """Every blocked-wording match in `text` (for tests and the review tooling)."""
    return [m.group(0) for p in _blocked() for m in p.finditer(text)]


# ----------------------------------------------------------------------------------------- reading grade
_SENTENCE_SPLIT = re.compile(r"[.!?]+(?=\s|$)")
_WORD = re.compile(r"[A-Za-z0-9]+(?:[.'’][A-Za-z0-9]+)*")
_VOWEL_GROUP = re.compile(r"[aeiouy]+")


def _syllables(word: str) -> int:
    w = word.lower()
    count = len(_VOWEL_GROUP.findall(w))
    if count > 1 and w.endswith("e") and not w.endswith("le"):
        count -= 1
    return max(1, count)


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(text) if not any(ch.isdigit() for ch in w)]


def reading_grade(text: str) -> float:
    """flesch_kincaid_grade/1 (C4): 0.39 x words/sentences + 11.8 x syllables/words - 15.59, to 0.1.

    Sentences split on [.!?]+ before whitespace or the end; words exclude tokens that contain a digit;
    syllables are vowel groups, minus a final silent e (not after l) when above 1, at least 1.
    """
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    words = _words(text)
    if not sentences or not words:
        return 0.0
    syllables = sum(_syllables(w) for w in words)
    grade = 0.39 * (len(words) / len(sentences)) + 11.8 * (syllables / len(words)) - 15.59
    return round(grade, 1)


def joined(parts: Iterable[str]) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


# ----------------------------------------------------------------------------------------- prompts
# Draft wording; Galit reviews it before the chain goes live in M6. Until then every model provider is a
# stub, so these messages are never sent.
PROMPT_VERSION = "draft/0"
_SYSTEM_PROMPTS = {
    "care_team_summary": (
        "You restate measurements for a care team. The JSON holds every number our software computed for "
        "one vertebra on two scans. Write a one-sentence headline and a one- or two-sentence detail that use "
        "only those numbers, written as digits exactly as given, with their units. Do not interpret the "
        "numbers, give advice or name a condition. Answer with JSON: headline, detail, numbers_used."
    ),
    "patient_explanation": (
        "You explain measurements to a patient in plain words a 12-year-old can read. The JSON holds every "
        "number our software computed for one bone in the spine on two scans. Write a one-sentence headline "
        "and a short detail that use only those numbers, written as digits exactly as given, with their "
        "units. Do not interpret the numbers, give advice or name a condition. Answer with JSON: headline, "
        "detail, numbers_used."
    ),
}


def messages_for(task: str, facts_json_text: str) -> list[dict]:
    """Chat messages for one task: the system rules and the facts JSON, nothing else."""
    return [{"role": "system", "content": _SYSTEM_PROMPTS[task]},
            {"role": "user", "content": facts_json_text}]
