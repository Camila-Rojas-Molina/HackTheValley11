"""Senior-mode wording: never a diagnosis, never a named condition as a conclusion, no forward-looking
claim (no '48-72 h early warning'), no skin temperature, and none of the BUILD_PLAN §7.3 stems.

Checked twice: every string the care code can emit (cards for many seeds, script notes, constants),
and a grep over the care source files, comments and docstrings included. test_care_db.py runs
the same check on the live HTTP responses.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from app import care

BANNED = ("spik", "intoxicat", "unconscious", "diagnos", "detect", "bradycard", "tachycard", "tamper", "evidence")
CARE_BLOCKLIST = (
    # named conditions as conclusions
    r"hypotension", r"orthostatic", r"\bpots\b", r"dehydrat", r"\buti\b", r"urinary", r"sepsis", r"septic",
    r"infection", r"delirium", r"dementia", r"parkinson", r"stroke", r"syncope", r"frail", r"disease",
    r"disorder", r"sedat", r"hydration", r"pneumonia", r"syndrome", r"illness",
    # forward-looking claims
    r"predict", r"forecast", r"prevent", r"will fall", r"\bfalls?\b", r"\bfall(?:ing|en)\b", r"\bfell\b",
    r"\brisk", r"early warning", r"48\s*[-–—]\s*72", r"\b72\s*h",
    r"\bwithin\s+\d+\s*(?:h|hrs?|hours?|days?|weeks?)\b", r"\b(?:in|over)\s+the\s+(?:next|coming)\s+(?:few\s+)?(?:\d+\s+)?(?:hours?|days?|weeks?)\b",
    r"\btomorrow\b",
    # no skin-temperature sensing
    r"skin", r"temperature", r"fever",
)
PATTERN = re.compile("|".join(BANNED + CARE_BLOCKLIST), re.IGNORECASE)

BACKEND = Path(__file__).resolve().parent.parent
CARE_SOURCES = [BACKEND / "app" / "care.py", BACKEND / "app" / "routes" / "care.py",
                *sorted((BACKEND / "migrations").glob("*care*.sql"))]


def hits(text: str) -> list[str]:
    return sorted({m.group(0) for m in PATTERN.finditer(text)})


def test_the_check_itself_catches_the_outline_wording():
    for bad in ("48-72 hrs early warning", "48–72 h", "orthostatic HR spike", "early UTI/sepsis delirium",
                "occipital skin temp +0.8 C", "fall predicted", "POTS", "dehydration check", "diagnosis"):
        assert hits(bad), bad
    assert not hits("Standing sway over the last 48 h: 0.114 vs 0.079 m/s² in their own 48 h baseline")


@pytest.mark.parametrize("seed", [care.DEFAULT_SEED, 0, 1, 2, 3, 19, 91, 153])
def test_every_card_string_is_clean(seed):
    cohort = care.generate_cohort(seed, date(2026, 10, 16))
    for card in care.score_cohort(cohort, date(2026, 10, 16)):
        text = json.dumps(card, ensure_ascii=False)
        assert not hits(text), (card["resident_id"], hits(text))


def test_constants_and_script_notes_are_clean():
    strings = [care.NOTICE, care.ASSUMPTION, care.METHOD, *care.LABELS.values(), *care.AIDE_CHECKS.values(),
               *care.THRESHOLDS.values(), *care.LEVELS, *care.RULES, *care.EXPERIMENTAL,
               *care.EXPERIMENTAL_NOTES.values()]
    for script in care.SCRIPTS.values():
        strings += [script.name, script.note]
    for s in strings:
        assert not hits(s), (s, hits(s))


def test_not_enough_data_and_minimum_change_texts_are_clean():
    from dataclasses import replace

    from tests.test_care_rules import END, flat_days
    sparse = [d for d in flat_days() if d.day != END]
    small = flat_days(stand_hr_n=1)                                    # too few stands with heart rate
    one_night = flat_days()
    one_night[-1] = replace(one_night[-1], night_restless_min=90.0)
    for days in (sparse, small, one_night):
        text = json.dumps(care.score_resident("X", "Synthetic resident X", days, END), ensure_ascii=False)
        assert not hits(text), hits(text)


@pytest.mark.parametrize("path", CARE_SOURCES, ids=lambda p: p.name)
def test_care_sources_are_clean(path):
    found = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if hits(line):
            found.append(f"{path.name}:{i}: {line.strip()} {hits(line)}")
    assert not found, "\n".join(found)


def test_every_card_says_synthetic_and_untested():
    cohort = care.generate_cohort(care.DEFAULT_SEED, date(2026, 10, 16))
    for card in care.score_cohort(cohort, date(2026, 10, 16)):
        assert card["synthetic"] is True and "Synthetic resident" in card["display_name"]
        assert "not a medical device" in card["notice"] and "not clinically validated" in card["notice"]
        assert "not a medical assessment" in card["briefing"]
        assert all(r["threshold_status"] == "untested assumption" for r in card["rules"])
