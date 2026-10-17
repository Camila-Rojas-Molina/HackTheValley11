"""C6: contracts/labels.json, the single source of visible wording, the flag rule and the AI word lists."""
from __future__ import annotations

import json
import re
import string
from pathlib import Path

import pytest

import build_contracts as bc
from app import schemas as S

REPO = Path(__file__).resolve().parents[3]
LABELS = json.loads((REPO / "contracts" / "labels.json").read_text(encoding="utf-8"))
FIX = REPO / "contracts" / "fixtures"
FACTS = json.loads((FIX / "ai" / "facts.myel018-L1-d273.json").read_text(encoding="utf-8"))
STATUS_LINE = "Research prototype on public, de-identified data. Not a medical device. Not for clinical use or diagnosis."


def fields(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def test_status_line_and_attribution_are_the_goal_wording():
    assert LABELS["status_line"] == STATUS_LINE
    short = LABELS["attribution"]["short"]
    for part in ("Nohel et al. 2026", "Spinal-Multiple-Myeloma-SEG", "TCIA", "DOI 10.7937/k4qv-hh78", "CC BY 4.0"):
        assert part in short


def test_every_regex_list_compiles():
    for p in LABELS["wording"]["banned_claims"] + LABELS["ai"]["blocked_output_patterns"]:
        re.compile(p)


def test_units_cover_every_schema_unit():
    assert set(LABELS["units"]) == set(S.Unit.__args__)
    assert set(LABELS["basis_badges"]) == set(S.Basis.__args__)
    assert set(LABELS["provenance_badges"]) == set(S.Provenance.__args__) | {"filled"}


def test_every_synthetic_provenance_renders_simulated():
    for p, badge in LABELS["provenance_badges"].items():
        assert (badge == "SIMULATED") is (p != "real_scan"), p


def test_tiles_name_only_schema_metrics():
    keys = set(S.MetricKey.__args__)
    for t in LABELS["tiles"]:
        assert set(t["metrics"]) <= keys, t["key"]


def test_flag_cannot_fire_while_thresholds_are_unset():
    rule = LABELS["flag"]["rule"]
    assert rule["simulated_points_never_flag"] is True
    if rule["growth_pct_min"] is None or rule["lesion_share_pts_min"] is None:
        flag = json.loads((FIX / "patients.json").read_text(encoding="utf-8"))["patients"][0]["flag"]
        assert flag["status"] == "pending" and flag["alert_id"] is None


def test_flag_templates_render():
    t = LABELS["flag"]["templates"]
    assert fields(t["flagged"]) == {"change", "level", "vol_a", "vol_b", "share"}
    assert fields(t["pending"]) == {"change", "level", "vol_a", "vol_b", "available_in"}


@pytest.mark.parametrize("name", sorted(LABELS["ai"]["templates"]))
def test_ai_templates_pass_the_checker_rules_on_the_facts_fixture(name):
    tmpl = LABELS["ai"]["templates"][name]
    fmt = bc.facts_fmt(FACTS)
    text = tmpl["headline"].format(**fmt) + " " + tmpl["detail"].format(**fmt)
    assert set(bc.numbers_in(text)) <= bc.facts_numbers(FACTS), text
    blocked = re.compile("|".join(LABELS["ai"]["blocked_output_patterns"]), re.I)
    assert not blocked.search(text), blocked.search(text)
    words = re.compile(r"\b(" + "|".join(LABELS["ai"]["number_words"]) + r")\b", re.I)
    assert not words.search(text), words.search(text)
    assert len(text) <= LABELS["ai"]["max_chars"]


def test_number_rule_ignores_level_and_unit_tokens():
    assert bc.numbers_in("L1 and T11 in cm3 by M5: 3.24 cm³ on Day 273, −0.5") == [-0.5, 3.24, 273.0]


def test_reading_grade_is_reproducible_and_within_target():
    expl = json.loads((FIX / "scans" / "myel018-L1-d273" / "explanation.json").read_text(encoding="utf-8"))
    grade = bc.fk_grade(expl["text"]["headline"] + " " + expl["text"]["detail"])
    assert grade == expl["reading_grade"]
    assert grade <= LABELS["ai"]["reading_grade"]["target_max"] == expl["target_grade_max"]


def test_ai_footer_never_calls_the_data_synthetic():
    assert "synthetic" not in LABELS["ai"]["footer"].lower()
    assert "de-identified" in LABELS["ai"]["footer"]


def test_copilot_refusal_contains_no_number_and_no_advice():
    r = LABELS["copilot"]["refusal"]
    assert not bc.numbers_in(r)
    assert "should" not in r.lower()


def test_fixtures_and_labels_agree_on_the_labels_version():
    for rel in ("scans/myel018-L1-d273.json", "health.json"):
        assert json.loads((FIX / rel).read_text(encoding="utf-8"))["labels_version"] == LABELS["version"]
