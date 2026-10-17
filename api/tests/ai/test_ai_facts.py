"""Facts (C4 "Facts"): derived values computed by code, display precision, the canonical hash."""
from __future__ import annotations

import pytest

from app.ai.facts import build_facts, facts_sha256
from tests.ai.conftest import FACTS_FIXTURE, SUMMARY_FIXTURE, load_fixture

BASE = dict(scan_id="myel018-L1-d273", level="L1", day_a=0, day_b=273, lesion_volume_a_cm3=0.0,
            lesion_volume_b_cm3=3.2449, lesion_grid="original", vertebra_volume_b_cm3=68.5213,
            mask_source="expert", provenance="real_scan")


def test_build_facts_reproduces_the_fixture():
    built = build_facts(**BASE)
    assert built.model_dump() == load_fixture(FACTS_FIXTURE)


def test_facts_sha256_matches_the_golden_summary(facts):
    assert facts_sha256(facts) == load_fixture(SUMMARY_FIXTURE)["facts_sha256"]
    assert facts_sha256(build_facts(**BASE)) == facts_sha256(facts)


def test_derived_values_come_from_rounded_inputs():
    f = build_facts(**{**BASE, "day_a": 10, "day_b": 104, "lesion_volume_a_cm3": 1.006,
                       "lesion_volume_b_cm3": 2.004})
    assert f.interval_days == 94
    assert (f.lesion_volume_a_cm3, f.lesion_volume_b_cm3, f.lesion_volume_change_cm3) == (1.01, 2.0, 0.99)


def test_simulated_follows_provenance():
    assert build_facts(**BASE).simulated is False
    assert build_facts(**{**BASE, "provenance": "synthetic_metric_interp"}).simulated is True


@pytest.mark.parametrize("bad", [{"day_b": -1}, {"vertebra_volume_b_cm3": 0.0}, {"level": "C6"},
                                 {"provenance": "projection"}])
def test_bad_inputs_are_refused(bad):
    with pytest.raises(ValueError):
        build_facts(**{**BASE, **bad})
