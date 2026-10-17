"""Wording rules (BUILD_PLAN §7.3): no user-facing string may contain a banned stem.

Two checks: every rendered template, and a grep over every shipped source,
page, template, migration, script and doc (tests are excluded because they
hold the list itself).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import messages, rules, states

BANNED = ("spik", "intoxicat", "unconscious", "diagnos", "detect", "bradycard", "tachycard", "tamper", "evidence")
PATTERN = re.compile("|".join(BANNED), re.IGNORECASE)

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
SCANNED_SUFFIXES = {".py", ".html", ".sql", ".md", ".txt", ".example", ".sh", ".js", ".css", ".json", ".yml", ".yaml"}


def shipped_files():
    roots = [BACKEND / "app", BACKEND / "migrations", BACKEND / "scripts"]
    files = [p for r in roots if r.exists() for p in r.rglob("*")
             if p.is_file() and (p.suffix in SCANNED_SUFFIXES or p.name.endswith(".env.example"))]
    for extra in (REPO / "README.md", BACKEND / "README.md", REPO / ".env.example", BACKEND / ".env.example"):
        if extra.exists():
            files.append(extra)
    docs = REPO / "docs"
    if docs.exists():
        files += [p for p in docs.rglob("*") if p.is_file() and p.suffix in SCANNED_SUFFIXES]
    return [f for f in files if "__pycache__" not in f.parts]


SAMPLE = {"name": "Maya", "time": "21:17", "t1": "21:14", "t2": "21:15", "n": 3, "late": True,
          "contact": "Sam", "head": "ab" * 32,
          "location": {"as_of": "21:02", "age_min": 12, "link": "https://example.test/l/x", "simulated": True}}


@pytest.mark.parametrize("template", messages.TEMPLATES)
def test_rendered_templates_are_clean(template):
    text = messages.render(template, SAMPLE)
    assert not PATTERN.search(text), (template, text)


def test_constants_are_clean():
    strings = [v for v in vars(messages).values() if isinstance(v, str)]
    strings += list(messages.CONTACT_PAGE_TITLES.values()) + list(messages.CALLBACK_ANSWERS.values())
    strings += list(rules.REASON_LABELS.values()) + [states.FINAL_BANNER]
    for s in strings:
        assert not PATTERN.search(s), s


def test_no_banned_stem_anywhere_in_shipped_files():
    hits = []
    for f in shipped_files():
        for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if PATTERN.search(line):
                hits.append(f"{f.relative_to(REPO)}:{i}: {line.strip()}")
    assert not hits, "banned wording:\n" + "\n".join(hits)
    assert shipped_files(), "scanner found no files"


def test_alert_texts_carry_no_health_data():
    for t in ("alert_no_response", "alert_duress", "alert_final", "lost_contact"):
        text = messages.render(t, SAMPLE).lower()
        for word in ("bpm", "heart", "pulse", "hr "):
            assert word not in text, (t, text)
